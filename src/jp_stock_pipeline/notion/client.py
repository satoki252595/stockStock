"""スロットル付き Notion API クライアント (DESIGN.md §6.1, §12-4)。

- 全リクエストを設定値 (既定 2.5req/s) でスロットル
- 429 / 529 は再試行。5xx / 通信断は安全に再送できる読み取り・上書きだけ再試行
- 作成・追記は結果不明なら再送せず失敗を返す（成功後の応答欠落による重複を防ぐ）
- dry-run (§3-6): 本番DBへ一切書き込まない。書き込み操作は self.ops に記録し
  合成ID (`dry-run-*`) を返す。読み取りはトークンがあれば実行、無ければ空を返す
- 標準エンドポイントは notion-client、File Upload 等の未対応エンドポイントは
  raw_api() (requests) を使う
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import requests
from notion_client import Client
from notion_client.errors import APIResponseError, HTTPResponseError, RequestTimeoutError

from ..config import DEFAULT_NOTION_RPS

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_RETRIES = 6


# Notion は 1 クエリ（1 ページネーション系列）あたり 10,000 件で打ち切る。
# 到達すると has_more が false になるため、検知しないと「全件取れた」と誤認する。
# 出典: https://developers.notion.com/reference/query-a-data-source
QUERY_RESULT_LIMIT = 10_000


class QueryTruncatedError(RuntimeError):
    """クエリが 10,000 件上限で打ち切られた。全件前提の処理は続行してはならない。"""


class NotionRequestError(RuntimeError):
    """Notion API の失敗。作成結果不明の場合も、再送せずこの例外を返す。"""


class _RetryableRawError(Exception):
    """raw_api() の 429/5xx (requests.Response 由来)。Retry-After を保持する。"""

    def __init__(self, status: int, headers):
        super().__init__(f"Notion API HTTP {status}")
        self.status = status
        self.headers = headers


@dataclass
class RecordedOp:
    """dry-run 時に記録される書き込み操作。テスト検証にも使う。"""

    op: str  # "create_page" / "update_page" / "create_database" / ...
    payload: dict[str, Any]


class _Throttle:
    def __init__(self, rps: float):
        if rps <= 0:
            raise ValueError("rps must be positive")
        self._min_interval = 1.0 / rps
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = self._last + self._min_interval - now
            if delta > 0:
                time.sleep(delta)
                now = time.monotonic()
            self._last = now


def _retry_after_seconds(exc: Exception, attempt: int) -> float:
    """Retry-After ヘッダを尊重しつつ指数バックオフ。"""
    retry_after = 0.0
    headers = getattr(exc, "headers", None)
    if headers:
        try:
            retry_after = float(headers.get("retry-after") or headers.get("Retry-After") or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
    return max(retry_after, min(2.0**attempt, 120.0))


class NotionClient:
    def __init__(
        self,
        token: str | None,
        *,
        rps: float = DEFAULT_NOTION_RPS,
        dry_run: bool = False,
    ):
        if not token and not dry_run:
            raise ValueError("NOTION_TOKEN が未設定。dry_run=True 以外では必須")
        self.dry_run = dry_run
        self.ops: list[RecordedOp] = []
        self._dry_counter = itertools.count(1)
        self._token = token
        self._throttle = _Throttle(rps)
        self._client = Client(auth=token, notion_version=NOTION_VERSION) if token else None
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # 低レベル: スロットル+バックオフ付き呼び出し
    # ------------------------------------------------------------------

    def _call(self, fn, /, *, _retry_safe: bool = True, **kwargs) -> Any:
        # 公式方針: 429/529 は再試行、5xx は冪等な操作のみ。
        # https://developers.notion.com/reference/request-limits
        # POST query/search は読取だが、PATCH children.append は非冪等なので
        # HTTPメソッドだけでは判定しない。呼び出し側が操作の性質を指定する。
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            self._throttle.wait()
            try:
                return fn(**kwargs)
            except (
                APIResponseError, HTTPResponseError, _RetryableRawError,
                requests.RequestException, RequestTimeoutError, httpx.HTTPError,
            ) as exc:
                status = getattr(exc, "status", None) or 0
                rate_limited = status in (429, 529) or getattr(exc, "code", None) == "rate_limited"
                if isinstance(exc, APIResponseError) and not (rate_limited or status >= 500):
                    raise
                if not _retry_safe and not rate_limited:
                    raise NotionRequestError(
                        "Notion 書き込みの結果不明。重複防止のため自動再送しません。"
                        "対象が既に作成・追記されていないか確認してから再実行してください。"
                        f" ({type(exc).__name__}, HTTP {status or '不明'})"
                    ) from exc
                delay = (
                    _retry_after_seconds(exc, attempt)
                    if isinstance(exc, (HTTPResponseError, _RetryableRawError))
                    else min(2.0**attempt, 60.0)
                )
                logger.warning("Notion %s (attempt %d) — %.1fs 待機", type(exc).__name__, attempt, delay)
                time.sleep(delay)
                last_exc = exc
        raise NotionRequestError(f"Notion API リトライ枯渇: {last_exc}") from last_exc

    def raw_api(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        record_in_dry_run: bool = True,
    ) -> dict:
        """notion-client 未対応エンドポイント (File Upload 等) の直接呼び出し。

        書き込み系メソッドは dry-run では実行せず記録のみ。
        """
        is_write = method.upper() != "GET"
        if self.dry_run and is_write:
            if record_in_dry_run:
                return self._record(f"{method.upper()} {path}", json_body or data or {})
            return {}
        if not self._token:
            return {}

        def _do() -> dict:
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Notion-Version": NOTION_VERSION,
            }
            resp = self._session.request(
                method,
                f"{NOTION_API_BASE}/{path.lstrip('/')}",
                json=json_body,
                data=data,
                files=files,
                headers=headers,
                timeout=120,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise _RetryableRawError(resp.status_code, resp.headers)
            if resp.status_code >= 400:
                raise NotionRequestError(f"Notion API {resp.status_code}: {resp.text[:500]}")
            return resp.json()

        # raw POST の読取だけを列挙する。未知のPOST・File Upload作成/送信/完了は
        # 冪等性を仮定せず、応答不明なら上位の RawUploadError / 部分失敗経路へ返す。
        endpoint = path.strip("/")
        retry_safe = method.upper() == "GET" or (
            method.upper() == "POST" and (
                endpoint == "search"
                or re.fullmatch(r"(?:databases|data_sources)/[^/]+/query", endpoint) is not None
            )
        )
        return self._call(_do, _retry_safe=retry_safe)

    def _record(self, op: str, payload: dict[str, Any]) -> dict:
        self.ops.append(RecordedOp(op=op, payload=payload))
        synthetic_id = f"dry-run-{next(self._dry_counter)}"
        logger.info("[dry-run] %s -> %s", op, synthetic_id)
        return {"object": "dry_run", "id": synthetic_id, "op": op}

    # ------------------------------------------------------------------
    # 読み取り
    # ------------------------------------------------------------------

    def query_database(
        self,
        database_id: str,
        *,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
        max_pages: int | None = None,
        strict: bool = False,
    ) -> list[dict]:
        """全ページをページネーションして返す。トークン無し dry-run では空。

        Notion は **1 クエリあたり 10,000 件で打ち切る**。上限に当たると has_more が
        false になるため、素直に読むと「全件取れた」と誤認する。これを検知して
        必ず WARNING を出し、`strict=True` なら QueryTruncatedError を送出する。
        全件を前提にする処理（⑥エクスポート等）は strict を立てること。

        検知は2系統: (1) レスポンスの request_status.incomplete（2025-09-03 版 API で
        文書化。現行ピン留めの 2022-06-28 版で返るかは未確認）、(2) 取得件数が
        QUERY_RESULT_LIMIT に達した（版に依らず効く保険）。
        """
        if self._client is None or str(database_id).startswith("dry-run-"):
            # dry-run の合成DB ID (runner が補完) への実クエリは行わない (§3-6)
            return []
        results: list[dict] = []
        cursor: str | None = None
        pages = 0
        while True:
            kwargs: dict[str, Any] = {"database_id": database_id, "page_size": page_size}
            if filter is not None:
                kwargs["filter"] = filter
            if sorts is not None:
                kwargs["sorts"] = sorts
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = self._call(self._client.databases.query, **kwargs)
            results.extend(resp.get("results", []))
            pages += 1
            status = resp.get("request_status") or {}
            incomplete = str(status.get("type", "")) == "incomplete"
            if not resp.get("has_more") or (max_pages and pages >= max_pages):
                capped = len(results) >= QUERY_RESULT_LIMIT
                if incomplete or capped:
                    reason = status.get("incomplete_reason") or "件数が上限に到達"
                    message = (
                        f"Notion クエリが打ち切られた可能性: db={database_id} "
                        f"取得={len(results)} 件 上限={QUERY_RESULT_LIMIT} 理由={reason}。"
                        "全件前提の処理はこの結果を使ってはならない"
                    )
                    logger.warning("%s", message)
                    if strict:
                        raise QueryTruncatedError(message)
                return results
            cursor = resp.get("next_cursor")

    def get_page(self, page_id: str) -> dict:
        if self._client is None:
            return {}
        return self._call(self._client.pages.retrieve, page_id=page_id)

    def list_page_property_items(self, page_id: str, property_id: str) -> list[dict]:
        """ページの 1 プロパティを「プロパティ取得 API」で全件読む（読み取りのみ）。

        ページオブジェクトの relation は 25 件を超えると has_more が立ち、残りが載らない。
        全件が要る処理（④重複の集約・バックアップ）はこちらで読み直す。
        戻り値は property_item の配列（relation なら各要素の ["relation"]["id"]）。
        出典: https://developers.notion.com/reference/retrieve-a-page-property
        """
        if self._client is None:
            return []
        results: list[dict] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"page_id": page_id, "property_id": property_id}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = self._call(self._client.pages.properties.retrieve, **kwargs)
            if resp.get("object") != "list":
                # 単一値プロパティ（number 等）は list ではなく property_item を 1 つ返す
                return [resp]
            results.extend(resp.get("results", []))
            if not resp.get("has_more"):
                return results
            cursor = resp.get("next_cursor")

    def retrieve_database(self, database_id: str) -> dict:
        if self._client is None:
            return {}
        return self._call(self._client.databases.retrieve, database_id=database_id)

    def list_child_blocks(self, block_id: str) -> list[dict]:
        if self._client is None:
            return []
        results: list[dict] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"block_id": block_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = self._call(self._client.blocks.children.list, **kwargs)
            results.extend(resp.get("results", []))
            if not resp.get("has_more"):
                return results
            cursor = resp.get("next_cursor")

    # ------------------------------------------------------------------
    # 書き込み (dry-run では記録のみ)
    # ------------------------------------------------------------------

    def create_page(
        self,
        *,
        parent: dict,
        properties: dict,
        children: list[dict] | None = None,
        icon: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"parent": parent, "properties": properties}
        if children:
            payload["children"] = children
        if icon:
            payload["icon"] = icon
        if self.dry_run:
            return self._record("create_page", payload)
        return self._call(self._client.pages.create, _retry_safe=False, **payload)

    def update_page(self, page_id: str, properties: dict) -> dict:
        if self.dry_run:
            return self._record("update_page", {"page_id": page_id, "properties": properties})
        return self._call(self._client.pages.update, page_id=page_id, properties=properties)

    def archive_page(self, page_id: str) -> dict:
        """ページを archive する（Notion のゴミ箱へ移す。復元可能な削除）。

        同じ page_id を何度 archive しても結果は同じなので、update と同様に再試行してよい。
        完全削除（ゴミ箱を空にする）はしない。
        """
        if self.dry_run:
            return self._record("archive_page", {"page_id": page_id})
        return self._call(self._client.pages.update, page_id=page_id, archived=True)

    def create_database(
        self,
        *,
        parent_page_id: str,
        title: str,
        properties: dict,
        icon: dict | None = None,
        description: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties,
        }
        if icon:
            payload["icon"] = icon
        if description:
            payload["description"] = [{"type": "text", "text": {"content": description}}]
        if self.dry_run:
            return self._record("create_database", payload)
        return self._call(self._client.databases.create, _retry_safe=False, **payload)

    def update_database(
        self, database_id: str, *, properties: dict | None = None
    ) -> dict:
        payload: dict[str, Any] = {"database_id": database_id}
        if properties is not None:
            payload["properties"] = properties
        if self.dry_run:
            return self._record("update_database", payload)
        return self._call(self._client.databases.update, **payload)

    def append_block_children(self, block_id: str, children: list[dict]) -> dict:
        if self.dry_run:
            return self._record("append_block_children", {"block_id": block_id, "children": children})
        return self._call(
            self._client.blocks.children.append, _retry_safe=False,
            block_id=block_id, children=children
        )
