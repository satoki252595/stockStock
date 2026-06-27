"""LocalStore: 端末B(PostgreSQL)への接続・スキーマ初期化・UPSERT 実行。

dual-write のミラー先。Notion を正本とし、ここへの書き込み失敗はジョブを止めず
呼び出し側 (JobContext.mirror) が degrade する設計（§3-2 で warning 記録）。
接続できない/未設定なら connect_local_store() は None を返し、Notion のみで稼働する。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import mappers
from .schema import SCHEMA_STATEMENTS

if TYPE_CHECKING:
    from ..config import LocalStoreSettings
    from ..models import (
        DisclosureRecord,
        FinancialSummaryRecord,
        PriceTechnicalRecord,
        RawArtifact,
        StockMasterRecord,
    )

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECS = 10


class LocalStore:
    """psycopg 接続のラッパ。1接続を autocommit で使う（ミラーはレコード単位）。"""

    def __init__(self, conn) -> None:
        self._conn = conn

    @classmethod
    def connect(cls, settings: LocalStoreSettings) -> LocalStore:
        import psycopg

        conn = psycopg.connect(
            **settings.connect_kwargs(),
            connect_timeout=_CONNECT_TIMEOUT_SECS,
            autocommit=True,
        )
        store = cls(conn)
        store.init_schema()
        return store

    def init_schema(self) -> None:
        """冪等な DDL を流す（テーブル/インデックスが無ければ作る）。"""
        with self._conn.cursor() as cur:
            for stmt in SCHEMA_STATEMENTS:
                cur.execute(stmt)

    def _exec(self, built: tuple[str, dict]) -> None:
        sql, params = built
        with self._conn.cursor() as cur:
            cur.execute(sql, params)

    # --- 書き込み (各 record を Notion と対称にミラー) -----------------------

    def upsert_stock_master(
        self, record: StockMasterRecord, *, include_lifecycle: bool = True
    ) -> None:
        self._exec(mappers.stock_master_upsert(record, include_lifecycle=include_lifecycle))

    def upsert_price_technical(self, record: PriceTechnicalRecord) -> None:
        if record.provenance.data_date is None:
            # (code, data_date) が主キーのため日付不明は格納できない（推定しない §3-1）
            logger.warning("② ローカルミラー省略: data_date 不明 (code=%s)", record.code)
            return
        self._exec(mappers.price_upsert(record))

    def upsert_financial_summary(self, record: FinancialSummaryRecord) -> None:
        self._exec(mappers.financial_upsert(record))

    def upsert_disclosure(self, record: DisclosureRecord) -> None:
        self._exec(mappers.disclosure_upsert(record))

    def upsert_raw_artifact(self, artifact: RawArtifact) -> None:
        self._exec(mappers.raw_file_upsert(artifact))

    def apply_disclosure_lifecycle(self, record: DisclosureRecord) -> None:
        built = mappers.lifecycle_update(record)
        if built is not None:
            self._exec(built)

    def mark_master_absent(self, code: str) -> None:
        self._exec(mappers.mark_absent_update(code))

    def write_job_log(
        self,
        job_name: str,
        status: str,
        processed: int,
        failed: int,
        failed_codes: list[str],
        *,
        run_url: str | None = None,
        duration_secs: float | None = None,
    ) -> None:
        self._exec(
            mappers.job_log_insert(
                job_name, status, processed, failed, failed_codes, run_url, duration_secs
            )
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - クローズ失敗はジョブ結果に影響させない
            logger.debug("LocalStore close 失敗（無視）", exc_info=True)


def connect_local_store(settings: LocalStoreSettings) -> LocalStore | None:
    """接続情報があれば LocalStore を返す。未設定/接続失敗なら None。

    接続失敗でジョブを落とさない（Notion が正本。ローカルは可用性ベストエフォート）。
    """
    if not settings.enabled:
        return None
    try:
        return LocalStore.connect(settings)
    except Exception as exc:  # noqa: BLE001 - 到達不能でも Notion 収集は継続する
        logger.error("ローカル PostgreSQL 接続失敗（Notion のみで継続）: %s", exc)
        return None
