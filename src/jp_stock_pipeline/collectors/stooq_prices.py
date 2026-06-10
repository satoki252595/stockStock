"""stooq 日足CSVコレクター (DESIGN.md §4 トラックB / §2.1)。

- ライセンス: stooq は商用許諾が不明確 → personal-only 固定（§2.1。公開・商用導線禁止）
- 役割: yfinance 障害時の「実在ソースの実データ」フォールバック（§3-2。ダミー生成は絶対禁止）
- 原本: 取得した生CSVバイト列を無加工で保存（§5.1: ダウンロード1回 = 1原本）
- DataFrame 化は型変換のみ（Date→date型、数値列→float）。値は一切変更しない（§5.2）

エンドポイント: ``GET https://stooq.com/q/d/l/?s={code4桁}.jp&i=d``
CSV列: Date,Open,High,Low,Close,Volume

注記: stooq は 2026年時点でブラウザ検証（SHA-256 proof-of-work チャレンジ）を
返すことがある。チャレンジは計算問題（ハッシュ探索）であり、解いて再取得する。
これはデータの捏造ではなく取得手段の自動化である（§3 真実性ポリシーに抵触しない）。
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from typing import TYPE_CHECKING

import pandas as pd
import requests

from .. import http
from ..licensing import source_license
from ..models import RawArtifact, Source
from ..rawstore import save_raw

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

STOOQ_DAILY_URL = "https://stooq.com/q/d/l/?s={code}.jp&i=d"
STOOQ_VERIFY_URL = "https://stooq.com/__verify"

# CSV の期待列 (§4 トラックB)
EXPECTED_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]
_NUMERIC_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# ブラウザ検証ページの proof-of-work パラメータ抽出
# 例: const c="AAAA...",d=4,t="0".repeat(d)
_CHALLENGE_RE = re.compile(r'const c="([^"]+)",d=(\d+)')

# PoW 探索の上限（d=4 なら期待値 ~65,536 回。暴走防止のガード）
_POW_MAX_ITER = 5_000_000


def _solve_challenge(html: str) -> tuple[str, int] | None:
    """ブラウザ検証ページから (c, n) を求める。

    sha256(c + str(n)) の16進表現が "0"*d で始まる最小の n を探索する
    （ページ内 JavaScript と同一の計算）。チャレンジでなければ None。
    """
    m = _CHALLENGE_RE.search(html)
    if not m:
        return None
    c, d = m.group(1), int(m.group(2))
    prefix = "0" * d
    for n in range(_POW_MAX_ITER):
        if hashlib.sha256(f"{c}{n}".encode()).hexdigest().startswith(prefix):
            return c, n
    logger.warning("stooq: proof-of-work が上限 %d 回で未解決", _POW_MAX_ITER)
    return None


def _fetch_csv_bytes(code: str, *, session: requests.Session | None = None) -> tuple[bytes, str]:
    """stooq から日足CSVの生バイト列を取得する（チャレンジ対応込み）。

    返り値: (生バイト列, 取得URL)。内容の妥当性検証は parse_daily_csv で行う。
    """
    sess = session or requests.Session()
    url = STOOQ_DAILY_URL.format(code=code)
    content = http.fetch(url, session=sess).content
    head = content[:4096].decode("utf-8", errors="replace")
    if "__verify" in head:
        solved = _solve_challenge(head)
        if solved is not None:
            c, n = solved
            logger.info("stooq: ブラウザ検証チャレンジを解決 (n=%d)", n)
            sess.post(
                STOOQ_VERIFY_URL,
                data={"c": c, "n": str(n)},
                headers={"User-Agent": http.USER_AGENT},
                timeout=http.DEFAULT_TIMEOUT,
            )
            content = http.fetch(url, session=sess).content
    return content, url


def parse_daily_csv(content: bytes) -> pd.DataFrame:
    """生CSVバイト列を DataFrame に変換する（値不変 §5.2: 型変換のみ）。

    - Date 列は datetime.date 型へ、数値列は float へ変換する
    - 空レスポンス / "No data" / CSVでない内容は FetchError
      （取得失敗 = 欠損として扱う §3-2。ダミー生成はしない）
    """
    text = content.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise http.FetchError("stooq: 空レスポンス（欠損として記録する §3-2）")
    if text.startswith("No data"):
        raise http.FetchError("stooq: 'No data' レスポンス（欠損として記録する §3-2）")
    if text.startswith("<"):
        raise http.FetchError(
            "stooq: CSVでないレスポンス（ブラウザ検証ページ等）。欠損として記録する §3-2"
        )
    df = pd.read_csv(io.StringIO(text))
    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise http.FetchError(f"stooq: 期待列が欠落 {missing} (列={list(df.columns)})")
    # 型変換のみ（値不変 §5.2）。日付が解釈不能なら例外（黙殺・補完はしない §3-1）
    df["Date"] = pd.to_datetime(df["Date"], format="%Y-%m-%d", errors="raise").dt.date
    for col in _NUMERIC_COLUMNS:
        df[col] = df[col].astype("float64")
    return df


def fetch_daily(
    settings: "Settings", code: str, *, session: requests.Session | None = None
) -> tuple[RawArtifact, pd.DataFrame]:
    """銘柄 1 つの日足全履歴を取得する (§8.1 step 1-2)。

    - 原本 = 取得した生CSVバイト列そのまま
      (datatype="daily_prices", scope=銘柄コード, license=personal-only §2.1)
    - データ基準日 = CSV 中の最終取引日
    - 取得失敗・無効レスポンスは FetchError（呼び出し側は欠損として記録 §3-2）
    """
    content, url = _fetch_csv_bytes(code, session=session)
    df = parse_daily_csv(content)  # 無効レスポンスはここで FetchError
    data_date = df["Date"].max() if len(df) else None
    artifact = save_raw(
        content,
        source=Source.STOOQ,
        datatype="daily_prices",
        scope=code,
        data_date=data_date,
        url=url,
        ext="csv",
        license_tag=source_license(Source.STOOQ),
        base_dir=settings.raw_data_dir,
    )
    return artifact, df
