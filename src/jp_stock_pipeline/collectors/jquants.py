"""J-Quants API（無料/個人版）コレクター (DESIGN.md §4 トラックB, §12)。

- ライセンス: 規約上、登録者本人の私的利用に限定 → personal-only 厳守
  （§2.1。公開ページ・商用デリバラブルには一切含めない）
- レート制限 (§4): 5コール/分 → 全APIコールを 12 秒間隔でスロットル、
  429 は指数バックオフで再試行
- 無料版データは 12 週遅延（確定値の突合・バックフィル用 §3-5, §8.2 jquants_weekly）
- 認証 (§12): POST /token/auth_user (mail+password) → refreshToken →
  POST /token/auth_refresh → idToken（24時間有効。メモリ保持のみ。永続化しない）

原本の定義 (§5.1, §5.2):
    pagination_key で複数レスポンスに分かれる取得も「1取得単位」とし、
    各ページの生レスポンスJSONバイト列を改行区切りで連結したもの
    （JSON Lines: 1行=1ページの無加工レスポンス）を原本とする。
DataFrame 化は列名そのまま・型変換のみ（値不変 §5.2）。
"""

from __future__ import annotations

import io
import json
import logging
import time
from datetime import date
from typing import TYPE_CHECKING, Callable

import pandas as pd
import requests

from ..config import ConfigError
from ..http import USER_AGENT, FetchError
from ..licensing import source_license
from ..models import RawArtifact, Source
from ..rawstore import save_raw

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

JQUANTS_API_BASE = "https://api.jquants.com/v1"

MIN_CALL_INTERVAL = 12.0  # §4: 5コール/分 → 12秒間隔
ID_TOKEN_TTL = 23 * 3600  # idToken は24時間有効 → 余裕をみて23時間で再認証
MAX_RETRIES_429 = 5
DEFAULT_TIMEOUT = 60

# 原本ページ間のセパレータ（JSON Lines）
_PAGE_SEPARATOR = b"\n"


def throttle_wait_seconds(last_call: float, now: float, min_interval: float = MIN_CALL_INTERVAL) -> float:
    """次のAPIコールまでに必要な待機秒数 (§4: 12秒間隔スロットル)。"""
    return max(0.0, last_call + min_interval - now)


def backoff_seconds(attempt: int) -> float:
    """429 時の指数バックオフ秒数（attempt は 0 始まり、上限 120 秒）。"""
    return min(2.0 ** (attempt + 1), 120.0)


class JQuantsClient:
    """スロットル+429バックオフ付き J-Quants API クライアント (§4, §12)。

    idToken は 24 時間有効（メモリ保持のみ）。期限が近づいたら自動で再認証する。
    """

    def __init__(
        self,
        settings: "Settings",
        *,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not settings.jquants_mail_address or not settings.jquants_password:
            raise ConfigError(
                "JQUANTS_MAIL_ADDRESS / JQUANTS_PASSWORD が未設定。"
                "J-Quants API には必須 (§12)。personal-only データのため本人の認証情報のみ使用"
            )
        self._mail = settings.jquants_mail_address
        self._password = settings.jquants_password
        self._session = session or requests.Session()
        self._sleeper = sleeper
        self._clock = clock
        self._last_call = float("-inf")
        self._id_token: str | None = None
        self._id_token_at = float("-inf")

    # ------------------------------------------------------------------
    # スロットル・低レベルHTTP
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        wait = throttle_wait_seconds(self._last_call, self._clock())
        if wait > 0:
            self._sleeper(wait)
        self._last_call = self._clock()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """スロットル+429指数バックオフ付きリクエスト。失敗は FetchError (§3-2)。"""
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
        for attempt in range(MAX_RETRIES_429 + 1):
            self._throttle()
            try:
                resp = self._session.request(method, url, headers=headers, **kwargs)
            except requests.RequestException as exc:
                raise FetchError(f"J-Quants 接続失敗: {url}: {exc}") from exc
            if resp.status_code == 429 and attempt < MAX_RETRIES_429:
                delay = backoff_seconds(attempt)
                logger.warning("J-Quants 429 (attempt %d) — %.0f秒バックオフ", attempt, delay)
                self._sleeper(delay)
                continue
            if resp.status_code >= 400:
                raise FetchError(
                    f"J-Quants 取得失敗: {url}: HTTP {resp.status_code}: {resp.text[:300]}"
                )
            return resp
        raise FetchError(f"J-Quants 429 リトライ枯渇: {url}")

    # ------------------------------------------------------------------
    # 認証 (§12: auth_user → refreshToken → auth_refresh → idToken)
    # ------------------------------------------------------------------

    def _authenticate(self) -> str:
        resp = self._request(
            "POST",
            f"{JQUANTS_API_BASE}/token/auth_user",
            json={"mailaddress": self._mail, "password": self._password},
        )
        refresh_token = resp.json().get("refreshToken")
        if not refresh_token:
            raise FetchError("J-Quants auth_user 応答に refreshToken が無い")
        resp = self._request(
            "POST",
            f"{JQUANTS_API_BASE}/token/auth_refresh",
            params={"refreshtoken": refresh_token},
        )
        id_token = resp.json().get("idToken")
        if not id_token:
            raise FetchError("J-Quants auth_refresh 応答に idToken が無い")
        return id_token

    def _ensure_token(self) -> str:
        if self._id_token is None or self._clock() - self._id_token_at > ID_TOKEN_TTL:
            self._id_token = self._authenticate()
            self._id_token_at = self._clock()
        return self._id_token

    # ------------------------------------------------------------------
    # 取得
    # ------------------------------------------------------------------

    def get_raw(self, path: str, params: dict | None = None) -> tuple[bytes, dict]:
        """認証付き GET。(生レスポンスバイト列, パース済みJSON) を返す。"""
        token = self._ensure_token()
        resp = self._request(
            "GET",
            f"{JQUANTS_API_BASE}/{path.lstrip('/')}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            payload = json.loads(resp.content)
        except ValueError as exc:
            raise FetchError(f"J-Quants 応答が JSON でない: {path}") from exc
        return resp.content, payload


def fetch_paginated(client: JQuantsClient, path: str, params: dict, data_key: str) -> tuple[bytes, list[dict]]:
    """pagination_key を辿って全ページ取得する (§12)。

    返り値: (原本バイト列 = 各ページ生JSONの改行連結, 全ページの data_key 行リスト)。
    行の値は無加工（連結のみ §5.2）。
    """
    pages: list[bytes] = []
    rows: list[dict] = []
    query = dict(params)
    while True:
        raw, payload = client.get_raw(path, query)
        pages.append(raw)
        page_rows = payload.get(data_key)
        if not isinstance(page_rows, list):
            raise FetchError(f"J-Quants 応答に {data_key!r} が無い: {path} keys={sorted(payload)}")
        rows.extend(page_rows)
        pagination_key = payload.get("pagination_key")
        if not pagination_key:
            break
        query = {**params, "pagination_key": pagination_key}
    return _PAGE_SEPARATOR.join(pages), rows


def rows_to_dataframe(rows: list[dict], *, date_columns: tuple[str, ...] = ()) -> pd.DataFrame:
    """API 行リストを DataFrame 化する（列名そのまま・型変換のみ・値不変 §5.2）。

    date_columns に指定した列のみ ISO 日付文字列 → datetime.date へ型変換する。
    欠損は欠損のまま（補完禁止 §3-1）。
    """
    df = pd.DataFrame(rows)
    for col in date_columns:
        if col in df.columns:
            # 型変換のみ。解釈不能値があれば例外（黙殺・補完はしない §3-1）
            df[col] = pd.to_datetime(df[col], format="ISO8601", errors="raise").dt.date
    return df


def _save_artifact(
    settings: "Settings", content: bytes, *, datatype: str, scope: str,
    data_date: date | None, url: str,
) -> RawArtifact:
    return save_raw(
        content,
        source=Source.JQUANTS,
        datatype=datatype,
        scope=scope,
        data_date=data_date,
        url=url,  # 認証情報は含めない
        ext="jsonl",
        license_tag=source_license(Source.JQUANTS),  # personal-only 厳守 (§2.1)
        base_dir=settings.raw_data_dir,
    )


# ---------------------------------------------------------------------------
# エンドポイント別の取得関数
# ---------------------------------------------------------------------------


def daily_quotes(
    settings: "Settings", *, target_date: date, client: JQuantsClient | None = None
) -> tuple[RawArtifact, pd.DataFrame]:
    """日付指定の全銘柄調整済株価 (§4: GET /v1/prices/daily_quotes?date=YYYYMMDD)。

    無料版は12週遅延の確定値（②③との突合・バックフィル用 §3-5, §8.2）。
    原本 = 全ページ生JSONの改行連結 (モジュール docstring の定義)。
    """
    client = client or JQuantsClient(settings)
    datestr = target_date.strftime("%Y%m%d")
    content, rows = fetch_paginated(
        client, "prices/daily_quotes", {"date": datestr}, "daily_quotes"
    )
    artifact = _save_artifact(
        settings,
        content,
        datatype="daily_quotes",
        scope="ALL",
        data_date=target_date,
        url=f"{JQUANTS_API_BASE}/prices/daily_quotes?date={datestr}",
    )
    return artifact, rows_to_dataframe(rows, date_columns=("Date",))


def statements(
    settings: "Settings",
    *,
    code: str | None = None,
    target_date: date | None = None,
    client: JQuantsClient | None = None,
) -> tuple[RawArtifact, pd.DataFrame]:
    """財務サマリ (§4: GET /v1/fins/statements。code か date のどちらか必須)。"""
    if (code is None) == (target_date is None):
        raise ValueError("statements は code または date のどちらか一方を指定する")
    client = client or JQuantsClient(settings)
    params: dict[str, str] = {}
    if code is not None:
        params["code"] = code
    if target_date is not None:
        params["date"] = target_date.strftime("%Y%m%d")
    content, rows = fetch_paginated(client, "fins/statements", params, "statements")
    query = "&".join(f"{k}={v}" for k, v in params.items())
    artifact = _save_artifact(
        settings,
        content,
        datatype="statements",
        scope=code or "ALL",
        data_date=target_date,
        url=f"{JQUANTS_API_BASE}/fins/statements?{query}",
    )
    # 値は文字列のまま保持（数値化は transform 側 §5.2）。DisclosedDate のみ日付型へ
    return artifact, rows_to_dataframe(rows, date_columns=("DisclosedDate",))


def announcement(
    settings: "Settings", *, client: JQuantsClient | None = None
) -> tuple[RawArtifact, pd.DataFrame]:
    """翌営業日の決算発表予定 (§4: GET /v1/fins/announcement)。"""
    client = client or JQuantsClient(settings)
    content, rows = fetch_paginated(client, "fins/announcement", {}, "announcement")
    artifact = _save_artifact(
        settings,
        content,
        datatype="announcement",
        scope="ALL",
        data_date=None,  # 発表予定は単一基準日を持たない（推定しない §3-1）
        url=f"{JQUANTS_API_BASE}/fins/announcement",
    )
    return artifact, rows_to_dataframe(rows, date_columns=("Date",))


def parse_raw_pages(content: bytes) -> list[dict]:
    """原本（改行連結 JSON Lines）をページ毎の dict に戻す（テスト・再処理用）。"""
    return [json.loads(line) for line in io.BytesIO(content).read().split(_PAGE_SEPARATOR) if line]
