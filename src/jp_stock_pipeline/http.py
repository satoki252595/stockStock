"""HTTP取得の共通実装 (DESIGN.md §8.1 step 1: リトライ3回・指数バックオフ)。"""

from __future__ import annotations

import logging

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

USER_AGENT = "jp-stock-data-pipeline/0.1"
DEFAULT_TIMEOUT = 60
MAX_ATTEMPTS = 3


class FetchError(RuntimeError):
    """リトライ後も取得に失敗した。呼び出し側は欠損として記録する (§3-2)。"""


class _RetryableHTTP(Exception):
    """429/5xx などリトライ対象のHTTPエラー。"""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (_RetryableHTTP, requests.ConnectionError, requests.Timeout))


@retry(
    reraise=True,
    stop=stop_after_attempt(MAX_ATTEMPTS),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    retry=retry_if_exception(_is_retryable),
)
def _get(session: requests.Session, url: str, params, headers, timeout) -> requests.Response:
    resp = session.get(url, params=params, headers=headers, timeout=timeout)
    if resp.status_code == 429 or resp.status_code >= 500:
        raise _RetryableHTTP(f"HTTP {resp.status_code} for {url}")
    return resp


def fetch(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> requests.Response:
    """GET 取得（指数バックオフ付き最大3回）。失敗は FetchError。

    4xx (429以外) は即時 FetchError。レスポンス内容の真偽判定は呼び出し側で行う。
    """
    sess = session or requests.Session()
    merged_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    try:
        resp = _get(sess, url, params, merged_headers, timeout)
    except Exception as exc:  # リトライ枯渇・接続不能
        raise FetchError(f"取得失敗: {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise FetchError(f"取得失敗: {url}: HTTP {resp.status_code}")
    return resp
