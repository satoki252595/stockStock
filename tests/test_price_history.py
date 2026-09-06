"""①配下の株価テクニカル履歴子DB。dry-run のみ（実 Notion へは書かない）。"""

from __future__ import annotations

from datetime import date, datetime

from jp_stock_pipeline.config import load_settings
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import DataQuality, JST, PriceTechnicalRecord, Provenance, Source
from jp_stock_pipeline.notion import price_history as H
from jp_stock_pipeline.notion import schema as S
from jp_stock_pipeline.notion.client import NotionClient

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


def settings():
    return load_settings(dry_run=True, env=ENV)


def dry_client() -> NotionClient:
    return NotionClient(None, rps=1000.0, dry_run=True)


def record(data_date: date = date(2026, 9, 4)) -> PriceTechnicalRecord:
    return PriceTechnicalRecord(
        code="7203",
        provenance=Provenance(
            source=Source.YFINANCE,
            license_tag=LicenseTag.PERSONAL_ONLY,
            data_date=data_date,
            fetched_at=datetime(2026, 9, 4, 19, 30, tzinfo=JST),
            quality=DataQuality.OK,
        ),
        close=3343.0,
        volume=14_251_500,
        rsi14=55.0,
    )


class TestTitleAndShard:
    def test_first_shard_has_no_suffix(self):
        assert H.history_db_title(1) == S.HISTORY_DB_TITLE

    def test_later_shards_are_numbered(self):
        assert H.history_db_title(2) == f"{S.HISTORY_DB_TITLE}_2"

    def test_parse_title(self):
        assert H.parse_history_db_title(S.HISTORY_DB_TITLE) == 1
        assert H.parse_history_db_title(f"{S.HISTORY_DB_TITLE}_3") == 3
        assert H.parse_history_db_title("別DB") is None

    def test_roll_at_threshold(self):
        assert H.should_roll_shard(S.HISTORY_SHARD_THRESHOLD) is True
        assert H.should_roll_shard(S.HISTORY_SHARD_THRESHOLD - 1) is False


class TestPayload:
    def test_title_is_iso_date_and_has_no_master_relation(self):
        props = H.price_history_properties(record())
        assert props[S.HISTORY_PROP_DATE_TITLE]["title"][0]["text"]["content"] == "2026-09-04"
        assert S.PROP_MASTER_RELATION not in props
        assert props[S.PRICE_PROP_CLOSE]["number"] == 3343.0
        assert props[S.PRICE_PROP_RSI14]["number"] == 55.0
        assert props[S.PROP_LICENSE_TAG]["select"]["name"] == LicenseTag.PERSONAL_ONLY.value

    def test_filter_uses_title_equals(self):
        flt = H.price_history_filter(date(2026, 9, 4))
        assert flt["property"] == S.HISTORY_PROP_DATE_TITLE
        assert flt["title"]["equals"] == "2026-09-04"


class TestParseMasterState:
    def test_reads_pointer_fields(self):
        page = {
            "id": "master-7203",
            "properties": {
                S.MASTER_PROP_CODE: {"rich_text": [{"plain_text": "7203"}]},
                S.MASTER_PROP_HISTORY_DB_ID: {"rich_text": [{"plain_text": "hist-db-1"}]},
                S.MASTER_PROP_HISTORY_SHARD: {"number": 2},
                S.MASTER_PROP_HISTORY_ROW_COUNT: {"number": 12},
            },
        }
        parsed = H.parse_master_state(page)
        assert parsed is not None
        code, state = parsed
        assert code == "7203"
        assert state.history_db_id == "hist-db-1"
        assert state.history_shard == 2
        assert state.history_row_count == 12

    def test_missing_pointer_defaults(self):
        page = {
            "id": "master-7203",
            "properties": {S.MASTER_PROP_CODE: {"rich_text": [{"plain_text": "7203"}]}},
        }
        _code, state = H.parse_master_state(page)
        assert state.history_db_id is None
        assert state.history_shard == 1
        assert state.history_row_count == 0


class TestEnsureAndUpsert:
    def test_creates_child_db_under_master_page_then_appends_row(self):
        client = dry_client()
        state = H.StockMasterState(page_id="master-7203")
        page_id, new_state, created = H.upsert_price_history(
            client, settings(), record(), state
        )
        assert created is True
        assert new_state.history_row_count == 1
        assert new_state.history_shard == 1
        creates = [op for op in client.ops if op.op == "create_database"]
        assert len(creates) == 1
        assert creates[0].payload["parent"]["page_id"] == "master-7203"
        assert creates[0].payload["title"][0]["text"]["content"] == S.HISTORY_DB_TITLE
        raw = creates[0].payload["properties"][S.PROP_RAW_RELATION]["relation"]
        assert raw["type"] == "single_property"
        row_ops = [
            op for op in client.ops
            if op.op == "create_page"
            and S.HISTORY_PROP_DATE_TITLE in (op.payload.get("properties") or {})
        ]
        assert len(row_ops) == 1
        assert page_id.startswith("dry-run-")
        pointer_ops = [
            op for op in client.ops
            if op.op == "update_page" and S.MASTER_PROP_HISTORY_DB_ID in op.payload["properties"]
        ]
        assert pointer_ops[-1].payload["properties"][S.MASTER_PROP_HISTORY_ROW_COUNT]["number"] == 1

    def test_same_date_updates_without_incrementing_count(self):
        client = dry_client()
        state = H.StockMasterState(
            page_id="master-7203", history_db_id="hist-db-1", history_shard=1, history_row_count=3
        )
        existing_page = "hist-row-20260904"

        def fake_query(db_id, **kwargs):
            flt = kwargs.get("filter") or {}
            if flt.get("title", {}).get("equals") == "2026-09-04":
                return [{"id": existing_page}]
            return []

        client.query_database = fake_query  # type: ignore[method-assign]
        page_id, new_state, created = H.upsert_price_history(
            client, settings(), record(), state
        )
        assert created is False
        assert page_id == existing_page
        assert new_state.history_row_count == 3
        assert any(op.op == "update_page" and op.payload.get("page_id") == existing_page for op in client.ops)
        assert not any(op.op == "create_database" for op in client.ops)

    def test_rolls_to_next_shard_at_threshold(self):
        client = dry_client()
        state = H.StockMasterState(
            page_id="master-7203",
            history_db_id="hist-db-1",
            history_shard=1,
            history_row_count=S.HISTORY_SHARD_THRESHOLD,
        )
        _page_id, new_state, created = H.upsert_price_history(
            client, settings(), record(), state
        )
        assert created is True
        assert new_state.history_shard == 2
        assert new_state.history_row_count == 1
        titles = [
            op.payload["title"][0]["text"]["content"]
            for op in client.ops
            if op.op == "create_database"
        ]
        assert titles == [f"{S.HISTORY_DB_TITLE}_2"]
