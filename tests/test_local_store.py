"""local_store のマッパー純粋関数 (record→SQL/params) と sink 分岐のテスト。

実 PostgreSQL は不要。SQL 文・パラメータの正しさ（PK・完全置換・ライフサイクル
出し分け・SQL インジェクション安全な %()s 形式）と、sink の data_date None 省略を検証。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.local_store import mappers
from jp_stock_pipeline.local_store.sink import LocalStore
from jp_stock_pipeline.models import (
    JST,
    ConvertStatus,
    DataQuality,
    DisclosureRecord,
    FinancialSummaryRecord,
    PriceTechnicalRecord,
    Provenance,
    RawArtifact,
    Source,
    StockMasterRecord,
)


def _prov(**kw) -> Provenance:
    base = dict(
        source=Source.EDINET,
        license_tag=LicenseTag.COMMERCIAL_OK,
        data_date=date(2026, 6, 10),
        fetched_at=datetime(2026, 6, 10, 12, tzinfo=JST),
    )
    base.update(kw)
    return Provenance(**base)


class TestStockMasterUpsert:
    def test_include_lifecycle_false_omits_lifecycle_cols(self):
        rec = StockMasterRecord(
            code="7203", name="トヨタ", status="上場",
            listing_date=date(2020, 1, 1), provenance=_prov(),
        )
        sql, params = mappers.stock_master_upsert(rec, include_lifecycle=False)
        # codelist 同期は status/上場日/上場廃止日 を一切送らない (§ Phase3 二重所有回避)
        assert "status" not in params
        assert "listing_date" not in params
        assert "delisting_date" not in params
        assert "status" not in sql
        assert "ON CONFLICT (code)" in sql
        assert "EXCLUDED" in sql
        # codelist 所有列は常に送る
        assert params["code"] == "7203"
        assert params["name"] == "トヨタ"
        assert params["listed"] is True

    def test_include_lifecycle_true_sends_lifecycle(self):
        rec = StockMasterRecord(
            code="7203", name="トヨタ", status="上場廃止",
            delisting_date=date(2026, 8, 31), provenance=_prov(),
        )
        sql, params = mappers.stock_master_upsert(rec, include_lifecycle=True)
        assert params["status"] == "上場廃止"
        assert params["delisting_date"] == date(2026, 8, 31)
        assert "status = EXCLUDED.status" in sql

    def test_provenance_expanded_to_columns(self):
        rec = StockMasterRecord(
            code="7203", name="t", provenance=_prov(quality=DataQuality.NEEDS_REVIEW),
        )
        _, params = mappers.stock_master_upsert(rec)
        assert params["source"] == "EDINET"
        assert params["license_tag"] == "commercial-ok"
        assert params["quality"] == "要確認"
        assert params["data_date"] == date(2026, 6, 10)

    def test_uses_named_placeholders_only(self):
        rec = StockMasterRecord(code="7203", name="x", provenance=_prov())
        sql, _ = mappers.stock_master_upsert(rec)
        # 値はプレースホルダ経由（文字列連結しない=SQLi 安全）
        assert "%(code)s" in sql
        assert "7203" not in sql


class TestPriceUpsert:
    def test_pk_is_code_and_data_date(self):
        rec = PriceTechnicalRecord(
            code="7203", close=2500.0, rsi14=55.5, provenance=_prov(data_date=date(2026, 6, 10)),
        )
        sql, params = mappers.price_upsert(rec)
        assert "ON CONFLICT (code, data_date)" in sql
        assert params["data_date"] == date(2026, 6, 10)
        assert params["close"] == 2500.0
        assert params["rsi14"] == 55.5
        # PK 列は UPDATE 対象に含めない
        assert "data_date = EXCLUDED.data_date" not in sql
        assert "code = EXCLUDED.code" not in sql
        # 非PK列は EXCLUDED で完全置換
        assert "close = EXCLUDED.close" in sql


class TestFinancialUpsert:
    def test_composite_pk_and_values(self):
        rec = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
            net_sales=1.0e12, eps=250.0, provenance=_prov(),
        )
        sql, params = mappers.financial_upsert(rec)
        assert "ON CONFLICT (code, fiscal_period_end, disclosure_type)" in sql
        assert params["net_sales"] == 1.0e12
        assert params["eps"] == 250.0


class TestDisclosureUpsert:
    def test_doc_id_pk_and_corporate_action_attrs(self):
        rec = DisclosureRecord(
            doc_id="d1", title="株式分割", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="株式分割", split_ratio="1:3", split_factor=3.0,
            effective_date=date(2026, 7, 1), provenance=_prov(),
        )
        sql, params = mappers.disclosure_upsert(rec)
        assert "ON CONFLICT (doc_id)" in sql
        assert params["doc_id"] == "d1"
        assert params["split_ratio"] == "1:3"
        assert params["split_factor"] == 3.0
        assert params["effective_date"] == date(2026, 7, 1)


class TestRawFileUpsert:
    def test_sha256_pk(self):
        art = RawArtifact(
            source=Source.EDINET, datatype="codelist", scope="ALL",
            data_date=date(2026, 6, 10), fetched_at=datetime(2026, 6, 10, tzinfo=JST),
            url="https://x/y.zip", local_path=Path("data/raw/y.zip"), sha256="abc123",
            size_bytes=999, license_tag=LicenseTag.COMMERCIAL_OK,
            convert_status=ConvertStatus.DONE,
        )
        sql, params = mappers.raw_file_upsert(art)
        assert "ON CONFLICT (sha256)" in sql
        assert params["sha256"] == "abc123"
        assert params["filename"] == "y.zip"
        assert params["convert_status"] == "完了"
        assert params["size_bytes"] == 999


class TestJobLogInsert:
    def test_insert_only_no_conflict(self):
        sql, params = mappers.job_log_insert("master_sync", "成功", 10, 0, [], None, 1.5)
        assert sql.startswith("INSERT INTO job_log")
        assert "ON CONFLICT" not in sql
        assert params["processed"] == 10
        assert params["failed_codes"] is None  # 空リストは NULL

    def test_failed_codes_joined(self):
        _, params = mappers.job_log_insert(
            "j", "一部失敗", 5, 2, ["7203", "9999"], "https://run", 2.0
        )
        assert params["failed_codes"] == "7203,9999"
        assert params["run_url"] == "https://run"


class TestLifecycleAndAbsent:
    def test_mark_absent_only_sets_listed_false(self):
        sql, params = mappers.mark_absent_update("7203")
        assert "listed = FALSE" in sql
        assert "status" not in sql  # 状態は一次開示が所有
        assert params == {"code": "7203"}

    def test_lifecycle_delisting_with_date(self):
        rec = DisclosureRecord(
            doc_id="d", title="上場廃止", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="上場廃止", effective_date=date(2026, 8, 31),
            provenance=_prov(),
        )
        sql, params = mappers.lifecycle_update(rec)
        assert "status = %(status)s" in sql
        assert "delisting_date = %(eff_date)s" in sql
        assert params["status"] == "上場廃止"
        assert params["eff_date"] == date(2026, 8, 31)

    def test_lifecycle_delisting_without_date_omits_date(self):
        rec = DisclosureRecord(
            doc_id="d", title="上場廃止後の取り扱い",
            disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="上場廃止", provenance=_prov(),
        )
        sql, params = mappers.lifecycle_update(rec)
        # 効力発生日が無いときは日付キーを送らない（既存値保持 §3-1）
        assert "delisting_date" not in sql
        assert params == {"status": "上場廃止", "code": "7203"}

    def test_lifecycle_new_listing(self):
        rec = DisclosureRecord(
            doc_id="d", title="新規上場", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="300A", doc_type="新規上場", effective_date=date(2026, 4, 1),
            provenance=_prov(),
        )
        sql, params = mappers.lifecycle_update(rec)
        assert params["status"] == "上場"
        assert "listing_date = %(eff_date)s" in sql

    def test_lifecycle_non_lifecycle_type_returns_none(self):
        rec = DisclosureRecord(
            doc_id="d", title="決算短信", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="短信", provenance=_prov(),
        )
        assert mappers.lifecycle_update(rec) is None

    def test_lifecycle_without_code_returns_none(self):
        rec = DisclosureRecord(
            doc_id="d", title="上場廃止", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code=None, doc_type="上場廃止", provenance=_prov(),
        )
        assert mappers.lifecycle_update(rec) is None


# --- sink の分岐 (DB は記録のみのフェイク接続) -----------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


class _FakeConn:
    def __init__(self) -> None:
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur

    def close(self):
        pass


class TestSinkBranches:
    def test_price_skipped_when_data_date_none(self):
        store = LocalStore(_FakeConn())
        rec = PriceTechnicalRecord(code="7203", close=1.0, provenance=_prov(data_date=None))
        store.upsert_price_technical(rec)
        assert store._conn.cur.executed == []  # 主キーを作れないので格納しない

    def test_price_executed_when_data_date_present(self):
        store = LocalStore(_FakeConn())
        rec = PriceTechnicalRecord(code="7203", close=1.0, provenance=_prov(data_date=date(2026, 6, 10)))
        store.upsert_price_technical(rec)
        assert len(store._conn.cur.executed) == 1
        sql, params = store._conn.cur.executed[0]
        assert "INSERT INTO prices" in sql
        assert params["code"] == "7203"

    def test_apply_lifecycle_noop_for_non_lifecycle(self):
        store = LocalStore(_FakeConn())
        rec = DisclosureRecord(
            doc_id="d", title="短信", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="短信", provenance=_prov(),
        )
        store.apply_disclosure_lifecycle(rec)
        assert store._conn.cur.executed == []


# --- JobContext.mirror の型 dispatch / degrade（⑤配線の回帰含む） ---------


class _RecordingLocal:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def upsert_stock_master(self, r, *, include_lifecycle=True):
        self.calls.append(("master", include_lifecycle))

    def upsert_price_technical(self, r):
        self.calls.append(("price",))

    def upsert_financial_summary(self, r):
        self.calls.append(("fin",))

    def upsert_disclosure(self, r):
        self.calls.append(("disc",))

    def upsert_raw_artifact(self, a):
        self.calls.append(("raw",))


def _ctx(local):
    from jp_stock_pipeline.jobs.runner import JobContext

    return JobContext(settings=None, client=None, args=None, local=local)


def _raw(data_date=date(2026, 6, 10)) -> RawArtifact:
    return RawArtifact(
        source=Source.EDINET, datatype="codelist", scope="ALL", data_date=data_date,
        fetched_at=datetime(2026, 6, 10, tzinfo=JST), url="https://x/y.zip",
        local_path=Path("data/raw/y.zip"), sha256="s", size_bytes=1,
        license_tag=LicenseTag.COMMERCIAL_OK,
    )


class TestJobContextMirror:
    def test_raw_artifact_is_dispatched(self):
        # ⑤ 配線の回帰: RawArtifact が upsert_raw_artifact へ dispatch される
        local = _RecordingLocal()
        _ctx(local).mirror(_raw())
        assert local.calls == [("raw",)]

    def test_stock_master_passes_include_lifecycle(self):
        local = _RecordingLocal()
        rec = StockMasterRecord(code="7203", name="t", provenance=_prov())
        _ctx(local).mirror(rec, include_lifecycle=False)
        assert local.calls == [("master", False)]

    def test_price_and_disclosure_dispatch(self):
        local = _RecordingLocal()
        ctx = _ctx(local)
        ctx.mirror(PriceTechnicalRecord(code="7203", provenance=_prov()))
        ctx.mirror(
            DisclosureRecord(
                doc_id="d", title="t", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
                code="7203", doc_type="短信", provenance=_prov(),
            )
        )
        assert local.calls == [("price",), ("disc",)]

    def test_noop_when_local_none(self):
        # local 未接続なら何もしない（例外を出さない）
        _ctx(None).mirror(_raw())  # 例外が出ないこと自体が assert

    def test_failure_degrades_without_raising(self):
        class _Bad:
            def upsert_raw_artifact(self, a):
                raise RuntimeError("db down")

        ctx = _ctx(_Bad())
        ctx.mirror(_raw())  # Notion 正本は別。ミラー失敗で例外を投げない
        assert ctx.mirror_failed == 1
