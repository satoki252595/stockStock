"""notion/upsert.py のテスト (§8.1-6)。

dry-run の NotionClient を使い実 Notion へは接続しない。
- Record → payload 構築 (None 除外 §3-1 / Provenance 必須 §3-3 / select 値が enum と一致)
- 複合キー filter の構造
- 冪等 upsert の create 経路 (dry-run では query が空 → create)
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from jp_stock_pipeline.config import load_settings
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import (
    DataQuality,
    DisclosureRecord,
    FinancialSummaryRecord,
    JST,
    PriceTechnicalRecord,
    Provenance,
    Source,
    StockMasterRecord,
)
from jp_stock_pipeline.notion import schema as S
from jp_stock_pipeline.notion import upsert

ENV = {
    "NOTION_DB_IDS_FILE": "/nonexistent/db_ids.json",
    "NOTION_DB_STOCK_MASTER": "db-master",
    "NOTION_DB_PRICES": "db-prices",
    "NOTION_DB_FINANCIALS": "db-fin",
    "NOTION_DB_DISCLOSURES": "db-disc",
    "NOTION_DB_RAW_FILES": "db-raw",
    "NOTION_DB_EXPORTS": "db-exp",
    "NOTION_DB_JOB_LOG": "db-job",
}

FETCHED_AT = datetime(2026, 6, 10, 19, 30, tzinfo=JST)


def make_settings():
    return load_settings(dry_run=True, env=ENV)


def prov(**overrides) -> Provenance:
    kwargs = dict(
        source=Source.EDINET,
        license_tag=LicenseTag.COMMERCIAL_OK,
        data_date=date(2026, 6, 10),
        fetched_at=FETCHED_AT,
        raw_page_id="raw-page-id-123",
        quality=DataQuality.OK,
    )
    kwargs.update(overrides)
    return Provenance(**kwargs)


class TestProvenanceProperties:
    def test_select_values_match_enums(self):
        props = upsert.provenance_properties(prov(source=Source.YFINANCE,
                                                  license_tag=LicenseTag.PERSONAL_ONLY))
        assert props[S.PROP_SOURCE]["select"]["name"] == Source.YFINANCE.value
        assert props[S.PROP_LICENSE_TAG]["select"]["name"] == LicenseTag.PERSONAL_ONLY.value
        assert props[S.PROP_QUALITY]["select"]["name"] == DataQuality.OK.value

    def test_data_date_none_is_explicit_clear(self):
        """欠損は明示的な空値で送る (update 時の前回値残存防止 §3-1/§2.2)。"""
        props = upsert.provenance_properties(prov(data_date=None))
        assert props[S.PROP_DATA_DATE] == {"date": None}

    def test_raw_relation_set_for_real_page_id(self):
        props = upsert.provenance_properties(prov(raw_page_id="abc123"))
        assert props[S.PROP_RAW_RELATION] == {"relation": [{"id": "abc123"}]}

    def test_raw_relation_skipped_for_dry_run_id(self):
        """dry-run 合成IDは relation に書かない。"""
        props = upsert.provenance_properties(prov(raw_page_id="dry-run-7"))
        assert S.PROP_RAW_RELATION not in props

    def test_raw_relation_skipped_when_none(self):
        props = upsert.provenance_properties(prov(raw_page_id=None))
        assert S.PROP_RAW_RELATION not in props

    def test_fetched_at_always_present(self):
        props = upsert.provenance_properties(prov())
        assert props[S.PROP_FETCHED_AT]["date"]["start"] == FETCHED_AT.isoformat()

    def test_raw_files_subset_mode(self):
        """⑤向け: raw relation / 品質を含めない (§6.3)。"""
        props = upsert.provenance_properties(
            prov(), include_raw_relation=False, include_quality=False
        )
        assert S.PROP_RAW_RELATION not in props
        assert S.PROP_QUALITY not in props
        assert S.PROP_SOURCE in props


class TestStockMasterPayload:
    def test_none_fields_explicit_clear(self):
        """None は明示クリア: update 時に前回値が残らない (§3-1)。"""
        rec = StockMasterRecord(code="7203", name="トヨタ自動車", provenance=prov())
        props = upsert.stock_master_properties(rec)
        assert props[S.MASTER_PROP_MARKET] == {"select": None}
        assert props[S.MASTER_PROP_SECTOR33] == {"select": None}
        assert props[S.MASTER_PROP_EDINET_CODE] == {"rich_text": []}

    def test_full_payload(self):
        rec = StockMasterRecord(
            code="7203",
            name="トヨタ自動車",
            provenance=prov(),
            market="プライム",
            sector33="輸送用機器",
            sector17="自動車・輸送機",
            edinet_code="E02144",
            listed=True,
        )
        props = upsert.stock_master_properties(rec)
        assert props[S.MASTER_PROP_NAME]["title"][0]["text"]["content"] == "トヨタ自動車"
        assert props[S.MASTER_PROP_CODE]["rich_text"][0]["text"]["content"] == "7203"
        assert props[S.MASTER_PROP_MARKET]["select"]["name"] == "プライム"
        assert props[S.MASTER_PROP_LISTED]["checkbox"] is True
        # Provenance 必須 (§3-3)
        assert props[S.PROP_SOURCE]["select"]["name"] == "EDINET"
        assert props[S.PROP_RAW_RELATION]["relation"] == [{"id": "raw-page-id-123"}]


class TestPriceTechnicalPayload:
    def test_missing_indicators_explicit_clear(self):
        """取得できなかった指標は {"number": None} で明示クリア (§3-1)。

        例: stooq フォールバック行に前回 yfinance の PER が残存しない。"""
        rec = PriceTechnicalRecord(
            code="7203",
            provenance=prov(source=Source.YFINANCE, license_tag=LicenseTag.PERSONAL_ONLY),
            close=3120.0,
        )
        props = upsert.price_technical_properties(rec)
        assert props[S.PRICE_PROP_CLOSE] == {"number": 3120.0}
        assert props[S.PRICE_PROP_RSI14] == {"number": None}
        assert props[S.PRICE_PROP_SMA200] == {"number": None}
        assert props[S.PRICE_PROP_PER] == {"number": None}

    def test_title_is_code(self):
        rec = PriceTechnicalRecord(code="7203", provenance=prov())
        props = upsert.price_technical_properties(rec)
        assert props[S.PRICE_PROP_CODE]["title"][0]["text"]["content"] == "7203"

    def test_master_relation(self):
        rec = PriceTechnicalRecord(code="7203", provenance=prov())
        props = upsert.price_technical_properties(rec, master_page_id="master-1")
        assert props[S.PROP_MASTER_RELATION]["relation"] == [{"id": "master-1"}]

    def test_master_relation_skipped_for_dry_run_id(self):
        rec = PriceTechnicalRecord(code="7203", provenance=prov())
        props = upsert.price_technical_properties(rec, master_page_id="dry-run-1")
        assert S.PROP_MASTER_RELATION not in props


class TestFinancialSummaryPayload:
    def make_record(self, **overrides) -> FinancialSummaryRecord:
        kwargs = dict(
            code="7203",
            fiscal_period_end=date(2026, 3, 31),
            disclosure_type="本決算",
            provenance=prov(),
        )
        kwargs.update(overrides)
        return FinancialSummaryRecord(**kwargs)

    def test_title_format(self):
        props = upsert.financial_summary_properties(self.make_record())
        assert (
            props[S.FIN_PROP_TITLE]["title"][0]["text"]["content"] == "7203 2026/03期 本決算"
        )

    def test_compound_key_properties_always_present(self):
        props = upsert.financial_summary_properties(self.make_record())
        assert props[S.FIN_PROP_CODE]["rich_text"][0]["text"]["content"] == "7203"
        assert props[S.FIN_PROP_PERIOD_END]["date"]["start"] == "2026-03-31"
        assert props[S.FIN_PROP_DISCLOSURE_TYPE]["select"]["name"] == "本決算"

    def test_none_numbers_explicit_clear(self):
        """§2.2 汚染防止: 別ソース更新時に前ソースの値 (例 J-Quants の予想値) が
        commercial-ok 行に残存しないことの基盤 = 全マップ対象列の明示クリア。"""
        props = upsert.financial_summary_properties(self.make_record(net_sales=1000.0))
        assert props[S.FIN_PROP_NET_SALES] == {"number": 1000.0}
        assert props[S.FIN_PROP_ROE] == {"number": None}
        assert props[S.FIN_PROP_CF_OPERATING] == {"number": None}
        assert props[S.FIN_PROP_FC_NET_INCOME] == {"number": None}


class TestFinancialSummaryFilter:
    def test_compound_and_filter_structure(self):
        flt = upsert.financial_summary_filter("7203", date(2026, 3, 31), "本決算")
        assert flt == {
            "and": [
                {"property": S.FIN_PROP_CODE, "rich_text": {"equals": "7203"}},
                {"property": S.FIN_PROP_PERIOD_END, "date": {"equals": "2026-03-31"}},
                {"property": S.FIN_PROP_DISCLOSURE_TYPE, "select": {"equals": "本決算"}},
            ]
        }


class TestKeyFilters:
    def test_stock_master_filter_is_rich_text_equals(self):
        assert upsert.stock_master_filter("7203") == {
            "property": S.MASTER_PROP_CODE,
            "rich_text": {"equals": "7203"},
        }

    def test_price_filter_is_title_equals(self):
        assert upsert.price_technical_filter("7203") == {
            "property": S.PRICE_PROP_CODE,
            "title": {"equals": "7203"},
        }

    def test_disclosure_filter_is_doc_id(self):
        assert upsert.disclosure_filter("S100XXXX") == {
            "property": S.DISC_PROP_DOC_ID,
            "rich_text": {"equals": "S100XXXX"},
        }


class TestDisclosurePayload:
    def test_payload(self):
        rec = DisclosureRecord(
            doc_id="S100ABCD",
            title="2026年3月期 決算短信",
            disclosed_at=datetime(2026, 5, 8, 15, 0, tzinfo=JST),
            provenance=prov(source=Source.TDNET, license_tag=LicenseTag.FACTUAL_CITE),
            code="7203",
            doc_type="短信",
            source_url="https://www.release.tdnet.info/example.pdf",
            has_xbrl=True,
        )
        props = upsert.disclosure_properties(rec)
        assert props[S.DISC_PROP_DOC_ID]["rich_text"][0]["text"]["content"] == "S100ABCD"
        assert props[S.DISC_PROP_DOC_TYPE]["select"]["name"] == "短信"
        assert props[S.DISC_PROP_HAS_XBRL]["checkbox"] is True
        assert props[S.PROP_LICENSE_TAG]["select"]["name"] == "factual-cite"

    def test_code_none_explicit_clear(self):
        rec = DisclosureRecord(
            doc_id="S100ABCD",
            title="全市場一括",
            disclosed_at=datetime(2026, 5, 8, tzinfo=JST),
            provenance=prov(),
        )
        props = upsert.disclosure_properties(rec)
        assert props[S.DISC_PROP_CODE] == {"rich_text": []}
        assert props[S.DISC_PROP_URL] == {"url": None}


class TestUpsertDryRunCreatePath:
    """dry-run では query が空 → create_page が記録される (冪等 upsert の create 経路)。"""

    def test_upsert_stock_master_creates(self, dry_client):
        settings = make_settings()
        rec = StockMasterRecord(code="7203", name="トヨタ自動車", provenance=prov())
        page_id = upsert.upsert_stock_master(dry_client, settings, rec)
        assert page_id.startswith("dry-run-")
        (op,) = [o for o in dry_client.ops if o.op == "create_page"]
        assert op.payload["parent"] == {"database_id": "db-master"}
        assert S.MASTER_PROP_NAME in op.payload["properties"]

    def test_upsert_price_technical_creates(self, dry_client):
        settings = make_settings()
        rec = PriceTechnicalRecord(code="7203", provenance=prov())
        upsert.upsert_price_technical(dry_client, settings, rec)
        (op,) = [o for o in dry_client.ops if o.op == "create_page"]
        assert op.payload["parent"] == {"database_id": "db-prices"}

    def test_upsert_financial_summary_creates(self, dry_client):
        settings = make_settings()
        rec = FinancialSummaryRecord(
            code="7203",
            fiscal_period_end=date(2026, 3, 31),
            disclosure_type="本決算",
            provenance=prov(),
        )
        upsert.upsert_financial_summary(dry_client, settings, rec)
        (op,) = [o for o in dry_client.ops if o.op == "create_page"]
        assert op.payload["parent"] == {"database_id": "db-fin"}

    def test_upsert_disclosure_creates(self, dry_client):
        settings = make_settings()
        rec = DisclosureRecord(
            doc_id="S100ABCD",
            title="決算短信",
            disclosed_at=datetime(2026, 5, 8, tzinfo=JST),
            provenance=prov(),
        )
        upsert.upsert_disclosure(dry_client, settings, rec)
        (op,) = [o for o in dry_client.ops if o.op == "create_page"]
        assert op.payload["parent"] == {"database_id": "db-disc"}

    def test_find_stock_master_returns_none_in_dry_run(self, dry_client):
        settings = make_settings()
        assert upsert.find_stock_master_page(dry_client, settings, "7203") is None


class TestUpsertUpdatePath:
    def test_existing_page_is_updated_not_created(self, dry_client, monkeypatch):
        """キー一致の既存行があれば update (冪等 §8.1-6)。"""
        settings = make_settings()
        monkeypatch.setattr(
            dry_client, "query_database", lambda *a, **kw: [{"id": "page-existing"}]
        )
        rec = StockMasterRecord(code="7203", name="トヨタ自動車", provenance=prov())
        page_id = upsert.upsert_stock_master(dry_client, settings, rec)
        assert page_id == "page-existing"
        ops = [o.op for o in dry_client.ops]
        assert "update_page" in ops
        assert "create_page" not in ops


class TestJobLog:
    def test_write_job_log(self, dry_client):
        settings = make_settings()
        upsert.write_job_log(
            dry_client,
            settings,
            "prices_daily",
            "一部失敗",
            processed=3800,
            failed=12,
            failed_codes=["7203", "6758"],
            run_url="https://github.com/x/y/actions/runs/1",
            duration_secs=3120.5,
        )
        (op,) = [o for o in dry_client.ops if o.op == "create_page"]
        props = op.payload["properties"]
        assert op.payload["parent"] == {"database_id": "db-job"}
        assert props[S.JOB_PROP_STATUS]["select"]["name"] == "一部失敗"
        assert props[S.JOB_PROP_PROCESSED] == {"number": 3800}
        assert props[S.JOB_PROP_FAILED_CODES]["rich_text"][0]["text"]["content"] == "7203, 6758"
        assert props[S.JOB_PROP_DURATION] == {"number": 3120.5}

    def test_invalid_status_raises(self, dry_client):
        settings = make_settings()
        with pytest.raises(ValueError):
            upsert.write_job_log(dry_client, settings, "x", "完了", 1, 0)

    def test_no_failed_codes_explicit_clear(self, dry_client):
        settings = make_settings()
        upsert.write_job_log(dry_client, settings, "master_sync", "成功", 3900, 0)
        (op,) = [o for o in dry_client.ops if o.op == "create_page"]
        assert op.payload["properties"][S.JOB_PROP_FAILED_CODES] == {"rich_text": []}
        assert op.payload["properties"][S.JOB_PROP_RUN_URL] == {"url": None}


class TestExportRow:
    def test_create_export_row(self, dry_client):
        settings = make_settings()
        upsert.create_export_row(
            dry_client,
            settings,
            "全銘柄株価日足 (personal-only)",
            period="2000-01-01〜2026-06-05",
            row_count=25_000_000,
            schema_desc="date, code, open, high, low, close, volume, adj_close",
            provenance=prov(source=Source.STOOQ, license_tag=LicenseTag.PERSONAL_ONLY),
            file_uploads=[("fu-1", "prices_all.parquet")],
        )
        (op,) = [o for o in dry_client.ops if o.op == "create_page"]
        props = op.payload["properties"]
        assert op.payload["parent"] == {"database_id": "db-exp"}
        assert (
            props[S.EXPORT_PROP_NAME]["title"][0]["text"]["content"]
            == "全銘柄株価日足 (personal-only)"
        )
        assert props[S.EXPORT_PROP_ROW_COUNT] == {"number": 25_000_000}
        assert props[S.EXPORT_PROP_FILES]["files"][0]["file_upload"] == {"id": "fu-1"}
        assert props[S.PROP_LICENSE_TAG]["select"]["name"] == "personal-only"

    def test_export_filter_is_title_equals(self):
        assert upsert.export_filter("dataset-x") == {
            "property": S.EXPORT_PROP_NAME,
            "title": {"equals": "dataset-x"},
        }
