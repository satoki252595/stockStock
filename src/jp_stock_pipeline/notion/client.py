"""スロットル付き Notion API クライアント (DESIGN.md §6.1, §12-4)。

- 全リクエストを設定値 (既定 2.5req/s) でスロットル
- 429 / 5xx は Retry-After を尊重した指数バックオフで再試行
- dry-run (§3-6): 本番DBへ一切書き込まない。書き込み操作は self.ops に記録し
  合成ID (`dry-run-*`) を返す。読み取りはトークンがあれば実行、無ければ空を返す
- 標準エンドポイントは notion-client、File Upload 等の未対応エンドポイントは
  raw_api() (requests) を使う
"""

from __future__ import annotations

import itertools
import logging
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


class NotionRequestError(RuntimeError):
    """リトライ後も失敗した Notion API 呼び出し。"""


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

    def _call(self, fn, /, **kwargs) -> Any:
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            self._throttle.wait()
            try:
                return fn(**kwargs)
            except APIResponseError as exc:
                status = getattr(exc, "status", None) or 0
                if exc.code == "rate_limited" or status >= 500:
                    delay = _retry_after_seconds(exc, attempt)
                    logger.warning("Notion %s (attempt %d) — %.1fs 待機", exc.code, attempt, delay)
                    time.sleep(delay)
                    last_exc = exc
                    continue
                raise
            except (HTTPResponseError, _RetryableRawError) as exc:
                delay = _retry_after_seconds(exc, attempt)
                logger.warning("Notion HTTP error (attempt %d) — %.1fs 待機: %s", attempt, delay, exc)
                time.sleep(delay)
                last_exc = exc
                continue
            except (requests.RequestException, RequestTimeoutError, httpx.HTTPError) as exc:
                time.sleep(min(2.0**attempt, 60.0))
                last_exc = exc
                continue
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

        return self._call(_do)

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
    ) -> list[dict]:
        """全ページをページネーションして返す。トークン無し dry-run では空。"""
        if self._client is None:
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
            if not resp.get("has_more") or (max_pages and pages >= max_pages):
                return results
            cursor = resp.get("next_cursor")

    def get_page(self, page_id: str) -> dict:
        if self._client is None:
            return {}
        return self._call(self._client.pages.retrieve, page_id=page_id)

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
        return self._call(self._client.pages.create, **payload)

    def update_page(self, page_id: str, properties: dict) -> dict:
        if self.dry_run:
            return self._record("update_page", {"page_id": page_id, "properties": properties})
        return self._call(self._client.pages.update, page_id=page_id, properties=properties)

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
        return self._call(self._client.databases.create, **payload)

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
            self._client.blocks.children.append, block_id=block_id, children=children
        )
