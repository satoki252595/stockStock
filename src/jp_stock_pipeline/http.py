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


@retry(
    reraise=True,
    stop=stop_after_attempt(MAX_ATTEMPTS),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    retry=retry_if_exception(_is_retryable),
)
def _post_idempotent(session, url, json_body, headers, timeout) -> requests.Response:
    """冪等な POST のみリトライする内部実装。"""
    resp = session.post(url, json=json_body, headers=headers, timeout=timeout)
    if resp.status_code == 429 or resp.status_code >= 500:
        raise _RetryableHTTP(f"HTTP {resp.status_code} for {url}")
    return resp


def post_json(
    url: str,
    *,
    json_body: dict,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
    idempotent: bool = False,
) -> requests.Response:
    """JSON を POST する。失敗は FetchError。

    ``idempotent`` を呼び出し側が明示したときだけ 5xx/タイムアウトを自動再送する。
    既定が False なのは、作成済みか不明な POST を再送すると二重作成しうるため
    （コミット 1b2910a で Notion の作成 POST に対して確立した方針と同じ）。
    D1 の ``INSERT ... ON CONFLICT DO UPDATE`` や SELECT は冪等なので True でよい。

    429 はサーバが処理せず弾いた応答なので、非冪等でも再送して安全。
    """
    sess = session or requests.Session()
    merged_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    try:
        if idempotent:
            resp = _post_idempotent(sess, url, json_body, merged_headers, timeout)
        else:
            resp = sess.post(url, json=json_body, headers=merged_headers, timeout=timeout)
    except Exception as exc:  # リトライ枯渇・接続不能
        raise FetchError(f"POST 失敗: {url}: {exc}") from exc
    if resp.status_code >= 400:
        # 応答本文には「どの権限が足りないか」が入っていることが多い
        # (Cloudflare は code/message を返す)。ステータスだけだと切り分けられない。
        # 資格情報そのものは本文に含まれないので、そのまま出して安全。
        detail = ""
        try:
            body = resp.text[:400]
        except Exception:  # noqa: BLE001 - 本文が読めなくても本来のエラーを潰さない
            body = ""
        if body:
            detail = f" body={body!r}"
        raise FetchError(f"POST 失敗: {url}: HTTP {resp.status_code}{detail}")
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
