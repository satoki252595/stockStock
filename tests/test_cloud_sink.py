"""CloudSink と JobContext の Cloudflare 配線 (docs/CF-CANONICAL-DESIGN.md)。

実通信はしない。検証点:
(a) 書込順序が R2 → D1 であること（逆だと索引が嘘をつく）
(b) 同一内容の再取得で PUT を繰り返さないこと（immutable キー）
(c) Cloudflare の失敗で収集が止まらないこと（移行中の第3系統）
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from _doubles import FakeR2, RecordingD1

from jp_stock_pipeline.cloud_store.sink import CloudSink
from jp_stock_pipeline.config import CloudStoreSettings
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import ConvertStatus, RawArtifact, Source

JST = timezone.utc


def _settings(**kw) -> CloudStoreSettings:
    base = dict(
        cf_account_id="acct",
        r2_access_key_id="k",
        r2_secret_access_key="s",
        cf_api_token="tok",
        d1_database_id="db",
    )
    base.update(kw)
    return CloudStoreSettings(**base)


def _artifact(tmp_path, *, doc_id="S100XU9L", converted=False) -> RawArtifact:
    path = tmp_path / "edinet_csv_7203_20260630.zip"
    path.write_bytes(b"raw-bytes")
    converted_paths = []
    if converted:
        conv = tmp_path / "edinet_csv_7203_20260630_converted.csv"
        conv.write_bytes(b"code,doc_id,element\n")
        converted_paths.append(conv)
    return RawArtifact(
        source=Source.EDINET, datatype="csv", scope="7203",
        data_date=date(2026, 6, 30), fetched_at=datetime(2026, 6, 30, 21, 0, tzinfo=JST),
        url="https://example/doc", local_path=path, sha256="c" * 64,
        size_bytes=9, license_tag=LicenseTag.COMMERCIAL_OK,
        converted_paths=converted_paths, convert_status=ConvertStatus.DONE,
        doc_id=doc_id,
    )


def _FakeR2(existing: set[str] | None = None, *, fail_on_put: bool = False) -> FakeR2:
    return FakeR2(existing, fail_on_put=fail_on_put)


class _FakeD1(RecordingD1):
    """発行文の形だけ見る。`calls` は旧名の別名（呼び出し側はそのまま）。"""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(fail_on_upsert=fail)

    @property
    def calls(self) -> list[tuple]:
        return self.upserts


def _sink(settings=None, *, r2=None, d1=None) -> CloudSink:
    sink = CloudSink(settings or _settings(), writer="edinet_daily")
    sink._raw_bucket = r2 if r2 is not None else _FakeR2()  # noqa: SLF001
    sink._d1 = d1 if d1 is not None else _FakeD1()  # noqa: SLF001
    return sink


class TestRawArtifact:
    def test_puts_to_r2_then_indexes_in_d1(self, tmp_path):
        r2, d1 = _FakeR2(), _FakeD1()
        assert _sink(r2=r2, d1=d1).upsert_raw_artifact(_artifact(tmp_path)) is True
        assert r2.puts == [
            "raw/edinet/csv/2026/2026-06-30/7203/S100XU9L/cccccccccccccccc.zip"
        ]
        table, columns, rows, conflict, keep = d1.calls[0]
        assert table == "jss_raw_files"
        assert conflict == ["sha256"]
        assert rows[0][columns.index("r2_key")] == r2.puts[0]
        assert rows[0][columns.index("doc_id")] == "S100XU9L"
        # first_fetched_at は再取得のたびに excluded.* で上書きしない (overlaps_refactor)。
        assert keep == ["first_fetched_at"]

    def test_d1_is_not_touched_when_r2_fails(self, tmp_path):
        """索引が嘘をつく状態を作らない（R2 → D1 の順序）。"""
        r2, d1 = _FakeR2(fail_on_put=True), _FakeD1()
        assert _sink(r2=r2, d1=d1).upsert_raw_artifact(_artifact(tmp_path)) is False
        assert d1.calls == []

    def test_existing_immutable_key_is_not_reuploaded(self, tmp_path):
        key = "raw/edinet/csv/2026/2026-06-30/7203/S100XU9L/cccccccccccccccc.zip"
        r2 = _FakeR2({key})
        assert _sink(r2=r2).upsert_raw_artifact(_artifact(tmp_path)) is True
        assert r2.puts == []  # 同一内容は PUT しない

    def test_legacy_key_is_used_when_it_already_exists(self, tmp_path):
        """(b)-(i): 新キーが無く旧キー(doc_id 無し)が実在すれば PUT せず旧キーを索引する。"""
        new_key = "raw/edinet/csv/2026/2026-06-30/7203/S100XU9L/cccccccccccccccc.zip"
        legacy = "raw/edinet/csv/2026/2026-06-30/7203/_/cccccccccccccccc.zip"
        r2, d1 = _FakeR2({legacy}), _FakeD1()
        assert _sink(r2=r2, d1=d1).upsert_raw_artifact(_artifact(tmp_path)) is True
        assert r2.puts == []  # 新キーへは PUT しない（R2 を増やさない）
        # HEAD は新キー→旧キーの2回だけ（PUT が無いことに加えて、無駄な
        # HEAD が追加で発行されないことも固定する）。
        assert r2.heads == [new_key, legacy]
        _table, columns, rows, _c, _keep = d1.calls[0]
        assert rows[0][columns.index("r2_key")] == legacy  # 索引の r2_key が正
        assert rows[0][columns.index("doc_id")] == "S100XU9L"  # doc_id は埋まる

    def test_legacy_derived_key_is_used_when_it_already_exists(self, tmp_path):
        """(b)-(ii): 派生は derived_key(key) で計算するので、旧キー救済時は派生も旧位置。"""
        new_key = "raw/edinet/csv/2026/2026-06-30/7203/S100XU9L/cccccccccccccccc.zip"
        legacy = "raw/edinet/csv/2026/2026-06-30/7203/_/cccccccccccccccc.zip"
        legacy_derived = "derived/edinet/csv/2026/2026-06-30/7203/_/cccccccccccccccc.csv"
        r2, d1 = _FakeR2({legacy, legacy_derived}), _FakeD1()
        _sink(r2=r2, d1=d1).upsert_raw_artifact(_artifact(tmp_path, converted=True))
        assert r2.puts == []  # 原本も派生も PUT しない
        assert r2.heads == [new_key, legacy, legacy_derived]
        _table, columns, rows, _c, _keep = d1.calls[0]
        assert rows[0][columns.index("derived_key")] == legacy_derived

    def test_neither_key_exists_puts_to_the_new_doc_id_key(self, tmp_path):
        """(b)-(iii): 新旧どちらにも無ければ新キーへ PUT する（doc_id 付き）。"""
        new_key = "raw/edinet/csv/2026/2026-06-30/7203/S100XU9L/cccccccccccccccc.zip"
        legacy = "raw/edinet/csv/2026/2026-06-30/7203/_/cccccccccccccccc.zip"
        r2, d1 = _FakeR2(), _FakeD1()
        assert _sink(r2=r2, d1=d1).upsert_raw_artifact(_artifact(tmp_path)) is True
        assert r2.puts == [new_key]
        # 旧キーも存在確認はする（無ければ諦めて新キーへ PUT）。
        assert r2.heads == [new_key, legacy]
        _table, columns, rows, _c, _keep = d1.calls[0]
        assert rows[0][columns.index("r2_key")] == new_key

    def test_legacy_key_is_not_checked_past_the_switchover_date(self, tmp_path):
        """LEGACY_KEY_UNTIL より新しい data_date では旧キーが実在しても無視する。"""
        from jp_stock_pipeline.cloud_store import keys
        from jp_stock_pipeline.cloud_store.sink import LEGACY_KEY_UNTIL

        # 定数を将来伸ばす運用（left_undone）と衝突しないよう、固定の月日では
        # なく相対計算で「必ず未来」の日付を作る。
        future = LEGACY_KEY_UNTIL + timedelta(days=1)
        artifact = _artifact(tmp_path)
        artifact.data_date = future
        legacy = keys.raw_key(
            source="EDINET", datatype="csv", scope="7203", data_date=future,
            sha256="c" * 64, ext="zip", doc_id=None,
        )
        new_key = keys.raw_key(
            source="EDINET", datatype="csv", scope="7203", data_date=future,
            sha256="c" * 64, ext="zip", doc_id="S100XU9L",
        )
        r2, d1 = _FakeR2({legacy}), _FakeD1()
        assert _sink(r2=r2, d1=d1).upsert_raw_artifact(artifact) is True
        assert r2.puts == [new_key]  # 旧キーが実在しても切替日後は使わない
        # 切替日を過ぎたら旧キーは HEAD すらしない（追加コストが本当に
        # ゼロであることを固定する。exists() 呼び出し回数を数えない実装
        # だと、採用条件だけ日付で絞っていても本テストは気づけない）。
        assert r2.heads == [new_key]

    def test_converted_file_goes_to_derived_and_is_indexed(self, tmp_path):
        r2, d1 = _FakeR2(), _FakeD1()
        _sink(r2=r2, d1=d1).upsert_raw_artifact(_artifact(tmp_path, converted=True))
        assert any(k.startswith("derived/") for k in r2.puts)
        _table, columns, rows, _c, _keep = d1.calls[0]
        assert rows[0][columns.index("derived_ext")] == "csv"

    def test_r2_success_without_d1_config_is_still_success(self, tmp_path):
        """R2 に原本が残ればトレーサビリティは保たれる。索引は後から作れる。"""
        settings = _settings(cf_api_token=None, d1_database_id=None)
        r2 = _FakeR2()
        assert _sink(settings, r2=r2).upsert_raw_artifact(_artifact(tmp_path)) is True
        assert r2.puts

    def test_d1_failure_is_reported_but_raw_is_kept(self, tmp_path):
        r2, d1 = _FakeR2(), _FakeD1(fail=True)
        assert _sink(r2=r2, d1=d1).upsert_raw_artifact(_artifact(tmp_path)) is False
        assert r2.puts  # 原本自体は残っている

    def test_disabled_when_no_credentials(self, tmp_path):
        sink = CloudSink(CloudStoreSettings(), writer="w")
        assert sink.upsert_raw_artifact(_artifact(tmp_path)) is None

    def test_dry_run_never_writes(self, tmp_path):
        sink = CloudSink(_settings(), writer="w", dry_run=True)
        assert sink.upsert_raw_artifact(_artifact(tmp_path)) is None


class TestJobContextWiring:
    def _ctx(self, cloud):
        import argparse

        from jp_stock_pipeline.config import load_settings
        from jp_stock_pipeline.jobs.runner import JobContext
        from jp_stock_pipeline.notion.client import NotionClient

        settings = load_settings(env={"RAW_DATA_DIR": "/tmp"}, dry_run=True)
        ctx = JobContext(
            settings=settings,
            client=NotionClient(None, rps=1000.0, dry_run=True),
            args=argparse.Namespace(),
        )
        ctx.cloud = cloud
        return ctx

    def test_cloud_failure_increments_counter_without_raising(self):
        class _Boom:
            def upsert_raw_artifact(self, artifact):
                raise RuntimeError("network down")

        ctx = self._ctx(_Boom())
        assert ctx._cloud(lambda c: c.upsert_raw_artifact(None), "x") is False  # noqa: SLF001
        assert ctx.cloud_failed == 1

    def test_false_result_is_counted_too(self):
        ctx = self._ctx(object())
        assert ctx._cloud(lambda c: False, "x") is False  # noqa: SLF001
        assert ctx.cloud_failed == 1

    def test_absent_cloud_returns_none_and_counts_nothing(self):
        ctx = self._ctx(None)
        assert ctx._cloud(lambda c: True, "x") is None  # noqa: SLF001
        assert ctx.cloud_failed == 0


class TestFinancialSummary:
    """③財務サマリ (D1 jss_financials)。器だけあって 0 行だった表への writer。"""

    def _record(self, **kw):
        from jp_stock_pipeline.models import (
            DataQuality,
            FinancialSummaryRecord,
            Provenance,
        )

        prov = Provenance(
            source=Source.EDINET,
            license_tag=LicenseTag.COMMERCIAL_OK,
            data_date=date(2026, 6, 25),
            fetched_at=datetime(2026, 6, 25, 21, 0, tzinfo=JST),
            quality=DataQuality.OK,
        )
        base = dict(
            code="7203",
            fiscal_period_end=date(2026, 3, 31),
            disclosure_type="本決算",
            provenance=prov,
            consolidated="連結",
            net_sales=1000.0,
            disclosed_at=datetime(2026, 6, 25, 15, 0, tzinfo=JST),
        )
        base.update(kw)
        return FinancialSummaryRecord(**base)

    def _store(self):
        from test_cloud_financials import FakeStore

        return FakeStore()

    def test_resolves_stock_id_and_writes_one_row(self):
        store = self._store()
        sink = _sink(d1=store)
        assert sink.upsert_financial_summary(
            self._record(), doc_id="S100AAAA", raw_sha256="a" * 64
        ) is True
        rows = store.fin_rows()
        assert len(rows) == 1
        assert rows[0]["stock_id"] == 11
        assert rows[0]["doc_id"] == "S100AAAA"
        assert rows[0]["raw_sha256"] == "a" * 64

    def test_stock_id_lookup_is_cached_across_records(self):
        """同じ銘柄が同日に複数の書類を出すのは普通。SELECT を繰り返さない。"""
        store = self._store()
        sink = _sink(d1=store)
        sink.upsert_financial_summary(self._record(), doc_id="S1", raw_sha256="a" * 64)
        sink.upsert_financial_summary(
            self._record(disclosure_type="1Q"), doc_id="S2", raw_sha256="b" * 64
        )
        from jp_stock_pipeline.cloud_store import financials

        assert store.sql_log.count(financials.STOCK_ID_SQL) == 1

    def test_a_failing_stock_id_lookup_is_reported_as_false_not_raised(self):
        """SELECT を try の外に出すと True/False/None の契約が破れる。"""
        from jp_stock_pipeline.cloud_store.d1 import D1Error

        class _Boom:
            def query(self, sql, params=None, *, idempotent=True):
                raise D1Error("no such table: core_stocks")

            def rows_per_request(self, n):
                return 3

        assert _sink(d1=_Boom()).upsert_financial_summary(
            self._record(), doc_id="S1", raw_sha256="a" * 64
        ) is False

    def test_conflict_target_without_a_matching_unique_fails_loudly(self):
        """PK 移行前の本番はこの経路。黙って後勝ちさせず失敗させる。"""
        import sqlite3

        from jp_stock_pipeline.cloud_store import schema as S
        from test_cloud_financials import FakeStore

        store = FakeStore()
        store.con.execute("DROP TABLE jss_financials")
        ddl = next(s for s in S.SCHEMA_STATEMENTS if "jss_financials" in s)
        store.con.execute(
            ddl.replace(
                "PRIMARY KEY (code, fiscal_period_end, disclosure_type, consolidated)",
                "PRIMARY KEY (code, fiscal_period_end, disclosure_type)",
            ).strip()
        )

        original = store.query

        def query(sql, params=None, *, idempotent=True):
            from jp_stock_pipeline.cloud_store.d1 import D1Error

            try:
                return original(sql, params, idempotent=idempotent)
            except sqlite3.OperationalError as exc:  # D1 は success=false で返す
                raise D1Error(str(exc)) from exc

        store.query = query
        assert _sink(d1=store).upsert_financial_summary(
            self._record(), doc_id="S1", raw_sha256="a" * 64
        ) is False

    def test_dry_run_writes_nothing(self):
        store = self._store()
        sink = CloudSink(_settings(), writer="edinet_daily", dry_run=True)
        sink._d1 = store  # noqa: SLF001
        assert sink.upsert_financial_summary(
            self._record(), doc_id="S1", raw_sha256="a" * 64
        ) is None
        assert store.sql_log == []

    def test_disabled_without_d1_config(self):
        store = self._store()
        sink = _sink(_settings(cf_api_token=None, d1_database_id=None), d1=store)
        assert sink.upsert_financial_summary(
            self._record(), doc_id="S1", raw_sha256="a" * 64
        ) is None
        assert store.sql_log == []


class TestJobContextFinancialWiring:
    def test_none_record_is_a_programming_error_not_a_silent_noop(self):
        """`fin is None` のガードを呼び出し側から外したら気付けるようにする。"""
        import argparse

        import pytest

        from jp_stock_pipeline.config import load_settings
        from jp_stock_pipeline.jobs.runner import JobContext
        from jp_stock_pipeline.notion.client import NotionClient

        ctx = JobContext(
            settings=load_settings(env={"RAW_DATA_DIR": "/tmp"}, dry_run=True),
            client=NotionClient(None, rps=1000.0, dry_run=True),
            args=argparse.Namespace(),
        )
        with pytest.raises(ValueError):
            ctx.cloud_financial_summary(None, doc_id="S1", raw_sha256=None)
