"""R2 への書き込み (S3 互換 API)。

**REST API は使わない**。R2 の Cloudflare REST API は 1,200 req/5分 の制限があり、
日次 4,445 PUT には足りない (docs/CF-CANONICAL-DESIGN.md §2.4)。

**delete_object を実装しない**。既存 10 週の信用残 (2026-06-12〜。うち
07-03・07-10 は恒久欠測) を物理的に消せなくするための設計判断であり、
「実装し忘れ」ではない。消す必要が出たら、まずユーザーに判断を仰ぐこと。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from ..config import CloudStoreSettings
from .guards import GuardError, check_no_regression

logger = logging.getLogger(__name__)


class R2Error(RuntimeError):
    """R2 への読み書きに失敗した（取得単位の失敗として記録する）。"""


def _client(settings: CloudStoreSettings):
    try:
        import boto3  # noqa: PLC0415 - 任意依存。未設定環境では import しない
        from botocore.config import Config  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 依存が入っていない環境
        raise R2Error(f"boto3 が無い: {exc}") from exc
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


class R2Store:
    """1 バケットぶんの読み書き。バケットは用途ごとに分ける (§3.1)。

    バケット分離＝R2 API トークンのスコープ分離。R2 のトークンはバケット単位で
    しかスコープできず、プレフィックス単位ではできないため、公開 Worker が
    バインドしている vwap-data と、personal-only の需給を同居させない。
    """

    def __init__(self, settings: CloudStoreSettings, bucket: str, *, writer: str) -> None:
        self.settings = settings
        self.bucket = bucket
        self.writer = writer
        self._s3 = None
        # botocore のクライアントは概ねスレッドセーフとされるが、接続プールの
        # 競合を避けるためワーカースレッドごとに1つ持つ。テストが差し込んだ
        # self._s3 は全スレッドで共有される（差し替えを壊さないため）。
        self._local = threading.local()

    @property
    def s3(self):
        if self._s3 is not None:
            return self._s3
        client = getattr(self._local, "client", None)
        if client is None:
            client = _client(self.settings)
            self._local.client = client
        return client

    def get_json(self, key: str) -> tuple[Any | None, bool]:
        """(payload, found) を返す。404 は (None, False)。

        404 以外のエラーは R2Error。「取れなかった」を「無かった」に潰すと
        後退禁止ガードが無力化するため、呼び出し側は必ず例外を伝播させる。
        """
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - botocore の例外型に依存しない
            if _is_not_found(exc):
                return None, False
            raise R2Error(f"R2 GET 失敗 {self.bucket}/{key}: {exc}") from exc
        try:
            return json.loads(resp["Body"].read()), True
        except ValueError as exc:
            raise R2Error(f"R2 の既存オブジェクトが JSON でない {self.bucket}/{key}") from exc

    def put_bytes(self, key: str, body: bytes, *, content_type: str) -> None:
        """immutable キー用の素の PUT（原本など。ガードは適用しない）。"""
        try:
            self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=body, ContentType=content_type
            )
        except Exception as exc:  # noqa: BLE001
            raise R2Error(f"R2 PUT 失敗 {self.bucket}/{key}: {exc}") from exc
        logger.info("R2 PUT %s/%s (%d bytes)", self.bucket, key, len(body))

    def put_json_guarded(
        self,
        key: str,
        payload: Any,
        *,
        contract: dict[str, tuple[str, ...]] | None = None,
        add_writer: bool = True,
    ) -> None:
        """mutable JSON の全置換 PUT。後退禁止ガードを必ず通す (§2.1)。

        ガードに落ちたら **PUT しない**。GuardError は呼び出し側で取得単位の
        失敗として記録する（隠して成功にしない §3-2）。

        payload は dict と list の両方を受ける。**配列そのものが契約の
        オブジェクトがあるため**（`margin/weeks.json` は kabulab-cf の
        `/api/margin` が `JSON.parse(...).slice(-n)` で読むので、オブジェクトに
        変えると読み手が壊れる）。

        add_writer=False のときは writer キーを足さない。既存の公開面と
        1バイトも変えてはいけないオブジェクト（互換シム）に使う。
        """
        if add_writer:
            if not isinstance(payload, dict):
                raise ValueError(
                    f"add_writer=True は dict のみ対応（{type(payload).__name__} が渡された）: {key}"
                )
            payload = dict(payload)
            payload.setdefault("writer", self.writer)
        old, _found = self.get_json(key)
        check_no_regression(
            old, payload, writer=self.writer, contract=contract,
            require_writer=add_writer,
        )
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.put_bytes(key, body, content_type="application/json")

    def exists(self, key: str) -> bool:
        """immutable キーの冪等スキップ判定。"""
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return False
            raise R2Error(f"R2 HEAD 失敗 {self.bucket}/{key}: {exc}") from exc
        return True


def _is_not_found(exc: Exception) -> bool:
    """botocore の 404 系を型に依存せず判定する。"""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        if str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"}:
            return True
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if status == 404:
            return True
    return exc.__class__.__name__ in {"NoSuchKey", "404"}


__all__ = ["GuardError", "R2Error", "R2Store"]
