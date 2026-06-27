"""LocalStore: 端末B(PostgreSQL)への接続・スキーマ初期化・UPSERT 実行。

dual-write のミラー先。Notion を正本とし、ここへの書き込み失敗はジョブを止めず
呼び出し側 (JobContext.mirror) が degrade する設計（§3-2 で warning 記録）。
接続できない/未設定なら connect_local_store() は None を返し、Notion のみで稼働する。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import mappers
from .schema import FTS_STATEMENTS, SCHEMA_STATEMENTS

if TYPE_CHECKING:
    from collections.abc import Iterable

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
    def connect(cls, settings: LocalStoreSettings, target: str = "cloud") -> LocalStore:
        import psycopg

        conn = psycopg.connect(
            **settings.connect_kwargs(target),
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
        self._init_fulltext_index()

    def _init_fulltext_index(self) -> None:
        """PGroonga 日本語全文検索インデックスをベストエフォートで作成する。

        PGroonga 拡張が未導入の端末では CREATE EXTENSION が失敗するが、その場合も
        本体スキーマは使える（API は ILIKE 検索へフォールバックする）。autocommit の
        ため失敗文は単独で abort され、握って警告のみ出す。
        """
        for stmt in FTS_STATEMENTS:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(stmt)
            except Exception as exc:  # noqa: BLE001 - 拡張未導入でも本体は使える
                logger.warning(
                    "PGroonga 全文検索インデックス未作成（ILIKE 検索で代替）: %s", exc
                )
                return

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

    def upsert_xbrl_facts(self, rows: Iterable[dict], artifact: RawArtifact) -> int:
        """⑧ XBRL 全ファクトを doc 単位で bulk upsert する（ローカル専用）。

        格納した行数を返す（空なら 0 で何も実行しない）。executemany で一括投入。
        """
        sql, params_list = mappers.xbrl_facts_insert(rows, artifact)
        if not params_list:
            return 0
        with self._conn.cursor() as cur:
            cur.executemany(sql, params_list)
        return len(params_list)

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


def connect_local_store(
    settings: LocalStoreSettings, target: str = "cloud"
) -> LocalStore | None:
    """接続情報があれば LocalStore を返す。未設定/接続失敗なら None。

    target=cloud(既定) はクラウド経由（LOCAL_DB_HOST + TLS require）、
    target=lan は同一LAN（LOCAL_DB_LAN_HOST + prefer）。接続失敗でジョブを落とさない
    （Notion が正本。ローカルは可用性ベストエフォート）。
    """
    if not settings.enabled(target):
        return None
    try:
        return LocalStore.connect(settings, target)
    except Exception as exc:  # noqa: BLE001 - 到達不能でも Notion 収集は継続する
        logger.error("ローカル PostgreSQL 接続失敗（Notion のみで継続）: %s", exc)
        return None
