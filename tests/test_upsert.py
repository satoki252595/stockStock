"""notion/upsert.py のテスト (§8.1-6)。

dry-run の NotionClient を使い実 Notion へは接続しない。
- Record → payload 構築 (None 除外 §3-1 / Provenance 必須 §3-3 / select 値が enum と一致)
- 複合キー filter の構造
- 冪等 upsert の create 経路 (dry-run では query が空 → create)
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import notion_env, provenance

from jp_stock_pipeline.config import load_settings
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import (
    DataQuality,
    DisclosureRecord,
    FinancialSummaryRecord,
    JST,
    Provenance,
    Source,
    StockMasterRecord,
)
from jp_stock_pipeline.notion import schema as S
from jp_stock_pipeline.notion import upsert

ENV = notion_env()

FETCHED_AT = datetime(2026, 6, 10, 19, 30, tzinfo=JST)


def make_settings():
    return load_settings(dry_run=True, env=ENV)


def prov(**overrides) -> Provenance:
    overrides.setdefault("raw_page_id", "raw-page-id-123")
    return provenance(**overrides)


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

    def test_lifecycle_fields(self):
        """状態/上場日/上場廃止日 (§ Phase3)。未設定は明示クリア (§3-1)。"""
        rec = StockMasterRecord(code="7203", name="トヨタ自動車", provenance=prov())
        props = upsert.stock_master_properties(rec)
        assert props[S.MASTER_PROP_STATUS] == {"select": None}
        assert props[S.MASTER_PROP_LISTING_DATE] == {"date": None}
        assert props[S.MASTER_PROP_DELISTING_DATE] == {"date": None}

        rec2 = StockMasterRecord(
            code="9999", name="廃止予定", provenance=prov(),
            listed=False, status="上場廃止", delisting_date=date(2026, 7, 1),
        )
        props2 = upsert.stock_master_properties(rec2)
        assert props2[S.MASTER_PROP_LISTED]["checkbox"] is False
        assert props2[S.MASTER_PROP_STATUS]["select"]["name"] == "上場廃止"
        assert props2[S.MASTER_PROP_DELISTING_DATE]["date"]["start"] == "2026-07-01"


def _read_page(code="7203", **overrides):
    """① の query 返却形状（読み取り形）の properties を作る。"""

    def text(value):
        return {
            "rich_text": [
                {"type": "text", "plain_text": value, "text": {"content": value}}
            ]
        }

    def title(value):
        return {
            "title": [{"type": "text", "plain_text": value, "text": {"content": value}}]
        }

    def select(value):
        return {"select": {"name": value} if value is not None else None}

    props = {
        S.MASTER_PROP_NAME: title("トヨタ自動車"),
        S.MASTER_PROP_CODE: text(code),
        S.MASTER_PROP_LISTED: {"checkbox": True},
        S.MASTER_PROP_MARKET: select("プライム"),
        S.MASTER_PROP_SECTOR33: select("輸送用機器"),
        S.MASTER_PROP_SECTOR17: select("自動車・輸送機"),
        S.MASTER_PROP_EDINET_CODE: text("E02144"),
        # 時刻系は古いまま（同値 skip では見ないので一致しなくてよい）。
        S.MASTER_PROP_LAST_UPDATED: {"date": {"start": "2026-06-10"}},
        S.PROP_FETCHED_AT: {"date": {"start": "2026-06-10T19:30:00+09:00"}},
    }
    props.update(overrides)
    return props


def _master_rec(**overrides):
    kwargs = dict(
        code="7203",
        name="トヨタ自動車",
        provenance=prov(),
        market="プライム",
        sector33="輸送用機器",
        sector17="自動車・輸送機",
        edinet_code="E02144",
        listed=True,
    )
    kwargs.update(overrides)
    return StockMasterRecord(**kwargs)


class TestStockMasterMatchesPage:
    """L-19: 既存行と同値なら PATCH を省く（月次 3,841 件 → 差分のみ）。"""

    def test_同値なら真(self):
        assert upsert.stock_master_matches_page(_read_page(), _master_rec()) is True

    def test_由来や時刻が違っても同値(self):
        """data_date/fetched_at は毎 run 変わる。見ると skip が死ぬ。"""
        rec = _master_rec(
            provenance=prov(
                data_date=date(2026, 7, 10),
                fetched_at=datetime(2026, 7, 10, 19, 30, tzinfo=JST),
            )
        )
        assert upsert.stock_master_matches_page(_read_page(), rec) is True

    def test_ライフサイクルは見ない(self):
        """状態 3 項目は開示・消失が所有し master_sync は書かない。"""
        props = _read_page()
        props[S.MASTER_PROP_STATUS] = {"select": {"name": "上場廃止"}}
        rec = _master_rec(status="上場", listed=True)
        assert upsert.stock_master_matches_page(props, rec) is True

    @pytest.mark.parametrize(
        "prop,value",
        [
            (S.MASTER_PROP_NAME, {"title": [{"plain_text": "別名"}]}),
            (S.MASTER_PROP_LISTED, {"checkbox": False}),
            (S.MASTER_PROP_MARKET, {"select": {"name": "スタンダード"}}),
            (S.MASTER_PROP_SECTOR33, {"select": None}),
            (S.MASTER_PROP_EDINET_CODE, {"rich_text": []}),
        ],
    )
    def test_意味が違えば偽(self, prop, value):
        assert upsert.stock_master_matches_page(_read_page(**{prop: value}), _master_rec()) is False

    def test_読めない形は偽に倒す(self):
        """欠損より二重 PATCH がまし（書く側に倒す）。"""
        assert upsert.stock_master_matches_page({}, _master_rec()) is False
        props = _read_page()
        props[S.MASTER_PROP_MARKET] = {"select": {"id": "xxx"}}
        assert upsert.stock_master_matches_page(props, _master_rec()) is False

    def test_エントリは最古勝ちで_properties_を保持する(self):
        pages = [
            {"id": "new", "created_time": "2026-07-02T00:00:00.000Z",
             "properties": _read_page()},
            {"id": "old", "created_time": "2026-07-01T00:00:00.000Z",
             "properties": _read_page()},
        ]
        entries = upsert._master_entries_from_pages(pages)
        assert entries["7203"][0] == "old"
        assert entries["7203"][1][S.MASTER_PROP_CODE]["rich_text"][0]["plain_text"] == "7203"


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
        """§2.2 汚染防止: 別ソース更新時に前ソースの値 (例 別ソースの予想値) が
        commercial-ok 行に残存しないことの基盤 = 全マップ対象列の明示クリア。"""
        props = upsert.financial_summary_properties(self.make_record(net_sales=1000.0))
        assert props[S.FIN_PROP_NET_SALES] == {"number": 1000.0}
        assert props[S.FIN_PROP_ROE] == {"number": None}
        assert props[S.FIN_PROP_CF_OPERATING] == {"number": None}
        assert props[S.FIN_PROP_FC_NET_INCOME] == {"number": None}


class TestFinancialSummaryFilter:
    """③ のキーは 銘柄コード×決算期末×開示種別×連結単体（連結と単体を後勝ちで潰さない）。"""

    CONS_EQ = {"property": S.FIN_PROP_CONSOLIDATED, "select": {"equals": "連結"}}
    CONS_EMPTY = {"property": S.FIN_PROP_CONSOLIDATED, "select": {"is_empty": True}}

    def test_compound_and_filter_structure(self):
        flt = upsert.financial_summary_filter("7203", date(2026, 3, 31), "本決算", "連結")
        assert flt == {
            "and": [
                {"property": S.FIN_PROP_CODE, "rich_text": {"equals": "7203"}},
                {"property": S.FIN_PROP_PERIOD_END, "date": {"equals": "2026-03-31"}},
                {"property": S.FIN_PROP_DISCLOSURE_TYPE, "select": {"equals": "本決算"}},
                self.CONS_EQ,
            ]
        }

    def test_undetermined_consolidation_keys_the_empty_select(self):
        flt = upsert.financial_summary_filter("7203", date(2026, 3, 31), "本決算", None)
        assert flt["and"][3] == self.CONS_EMPTY

    def test_lookup_also_matches_rows_without_consolidation(self):
        flt = upsert.financial_summary_lookup_filter("7203", date(2026, 3, 31), "本決算", "連結")
        assert flt["and"][2] == {"property": S.FIN_PROP_DISCLOSURE_TYPE, "select": {"equals": "本決算"}}
        assert flt["and"][3] == {"or": [self.CONS_EQ, self.CONS_EMPTY]}

    def test_lookup_for_undetermined_consolidation_does_not_match_known_rows(self):
        flt = upsert.financial_summary_lookup_filter("7203", date(2026, 3, 31), "本決算", None)
        assert flt["and"][3] == self.CONS_EMPTY

    def test_lookup_for_interim_also_matches_the_legacy_second_quarter(self):
        flt = upsert.financial_summary_lookup_filter("7203", date(2026, 6, 30), "中間", "連結")
        assert flt["and"][2] == {
            "or": [
                {"property": S.FIN_PROP_DISCLOSURE_TYPE, "select": {"equals": "中間"}},
                {"property": S.FIN_PROP_DISCLOSURE_TYPE, "select": {"equals": "2Q"}},
            ]
        }

    def test_second_quarter_lookup_does_not_match_interim_rows(self):
        flt = upsert.financial_summary_lookup_filter("7203", date(2023, 9, 30), "2Q", "連結")
        assert flt["and"][2] == {"property": S.FIN_PROP_DISCLOSURE_TYPE, "select": {"equals": "2Q"}}

    def test_lookup_nests_at_most_two_levels(self):
        """Notion の複合フィルタは 2 段 (and → or → 条件) までしかネストできない。"""
        flt = upsert.financial_summary_lookup_filter("7203", date(2026, 6, 30), "中間", "連結")
        for cond in flt["and"]:
            for sub in cond.get("or", []):
                assert "or" not in sub and "and" not in sub


class TestPickFinancialPage:
    @staticmethod
    def _page(page_id, minute, dtype, consolidated):
        return {
            "id": page_id,
            "created_time": f"2026-09-13T00:{minute:02d}:00.000Z",
            "properties": {
                S.FIN_PROP_DISCLOSURE_TYPE: {"select": {"name": dtype}},
                S.FIN_PROP_CONSOLIDATED: {"select": {"name": consolidated} if consolidated else None},
            },
        }

    def test_priority_prefers_consolidation_then_disclosure_type(self):
        pages = [
            self._page("legacy-both", 1, "2Q", None),
            self._page("empty-cons", 2, "中間", None),
            self._page("legacy-type", 3, "2Q", "連結"),
            self._page("exact", 4, "中間", "連結"),
        ]
        pick = upsert.pick_financial_page
        assert pick(pages, "中間", "連結")["id"] == "exact"
        assert pick(pages[:3], "中間", "連結")["id"] == "legacy-type"
        assert pick(pages[:2], "中間", "連結")["id"] == "empty-cons"
        assert pick(pages[:1], "中間", "連結")["id"] == "legacy-both"

    def test_oldest_wins_within_the_same_rank(self):
        pages = [self._page("new", 5, "本決算", "連結"), self._page("old", 1, "本決算", "連結")]
        assert upsert.pick_financial_page(pages, "本決算", "連結")["id"] == "old"

    def test_no_pages(self):
        assert upsert.pick_financial_page([], "本決算", "連結") is None


class TestKeyFilters:
    def test_stock_master_filter_is_rich_text_equals(self):
        assert upsert.stock_master_filter("7203") == {
            "property": S.MASTER_PROP_CODE,
            "rich_text": {"equals": "7203"},
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
        # コーポレートアクション属性は無ければ明示クリア (§3-1)
        assert props[S.DISC_PROP_SPLIT_RATIO] == {"rich_text": []}
        assert props[S.DISC_PROP_SPLIT_FACTOR] == {"number": None}
        assert props[S.DISC_PROP_EFFECTIVE_DATE] == {"date": None}

    def test_split_attributes(self):
        """分割開示は比率/係数/効力発生日を構造化保持する (§ Phase2/4)。"""
        rec = DisclosureRecord(
            doc_id="d1",
            title="株式分割（1株を3株に分割）に関するお知らせ",
            disclosed_at=datetime(2026, 5, 8, tzinfo=JST),
            provenance=prov(source=Source.TDNET, license_tag=LicenseTag.FACTUAL_CITE),
            code="7203",
            doc_type="株式分割",
            split_ratio="1:3",
            split_factor=3.0,
            effective_date=date(2026, 7, 1),
        )
        props = upsert.disclosure_properties(rec)
        assert props[S.DISC_PROP_SPLIT_RATIO]["rich_text"][0]["text"]["content"] == "1:3"
        assert props[S.DISC_PROP_SPLIT_FACTOR] == {"number": 3.0}
        assert props[S.DISC_PROP_EFFECTIVE_DATE]["date"]["start"] == "2026-07-01"


class TestLifecycleUpdates:
    """① ライフサイクル更新 (§ Phase3): 上場廃止検知 / 開示由来の状態反映。"""

    def _updates(self, dry_client):
        return [o for o in dry_client.ops if o.op == "update_page"]

    def test_mark_absent_from_codelist(self, dry_client):
        upsert.mark_master_absent_from_codelist(dry_client, make_settings(), "master-7203")
        (op,) = self._updates(dry_client)
        assert op.payload["page_id"] == "master-7203"
        props = op.payload["properties"]
        assert props[S.MASTER_PROP_LISTED]["checkbox"] is False
        # 状態の上場廃止確定は一次開示に一本化 (§3-1/§3-7)。コードリスト消失だけでは
        # 状態を倒さない(listed=True∧状態=上場廃止 の矛盾行や誤検知固着を防ぐ)
        assert S.MASTER_PROP_STATUS not in props

    def test_apply_lifecycle_delisting(self, dry_client, monkeypatch):
        monkeypatch.setattr(dry_client, "query_database", lambda *a, **k: [{"id": "m-7203"}])
        rec = DisclosureRecord(
            doc_id="d1", title="上場廃止に関するお知らせ",
            disclosed_at=datetime(2026, 5, 8, tzinfo=JST),
            provenance=prov(source=Source.TDNET, license_tag=LicenseTag.FACTUAL_CITE),
            code="7203", doc_type="上場廃止", effective_date=date(2026, 8, 31),
        )
        page_id = upsert.apply_disclosure_lifecycle(dry_client, make_settings(), rec)
        assert page_id == "m-7203"
        props = self._updates(dry_client)[-1].payload["properties"]
        assert props[S.MASTER_PROP_STATUS]["select"]["name"] == "上場廃止"
        assert props[S.MASTER_PROP_DELISTING_DATE]["date"]["start"] == "2026-08-31"
        # 発表時点では listed は触らない（効力発生まで取得継続。停止は消失検知が担う §3-1）
        assert S.MASTER_PROP_LISTED not in props

    def test_apply_lifecycle_delisting_without_date_preserves_existing(
        self, dry_client, monkeypatch
    ):
        """効力発生日を持たない後続の上場廃止開示は状態のみ更新し、上場廃止日を
        消さない（先行開示で取り込んだ確定日付を {date:None} で上書きしない §3-1）。"""
        monkeypatch.setattr(dry_client, "query_database", lambda *a, **k: [{"id": "m-7203"}])
        rec = DisclosureRecord(
            doc_id="d5", title="上場廃止後の当社株式の取り扱いに関するお知らせ",
            disclosed_at=datetime(2026, 9, 1, tzinfo=JST),
            provenance=prov(source=Source.TDNET, license_tag=LicenseTag.FACTUAL_CITE),
            code="7203", doc_type="上場廃止",  # effective_date は未指定(None)
        )
        upsert.apply_disclosure_lifecycle(dry_client, make_settings(), rec)
        props = self._updates(dry_client)[-1].payload["properties"]
        assert props[S.MASTER_PROP_STATUS]["select"]["name"] == "上場廃止"
        # 日付キーは送らない = Notion 側の既存「上場廃止日」を保持する
        assert S.MASTER_PROP_DELISTING_DATE not in props

    def test_apply_lifecycle_new_listing(self, dry_client, monkeypatch):
        monkeypatch.setattr(dry_client, "query_database", lambda *a, **k: [{"id": "m-300A"}])
        rec = DisclosureRecord(
            doc_id="d2", title="新規上場に関するお知らせ",
            disclosed_at=datetime(2026, 5, 8, tzinfo=JST),
            provenance=prov(source=Source.TDNET, license_tag=LicenseTag.FACTUAL_CITE),
            code="300A", doc_type="新規上場",
        )
        upsert.apply_disclosure_lifecycle(dry_client, make_settings(), rec)
        props = self._updates(dry_client)[-1].payload["properties"]
        assert props[S.MASTER_PROP_STATUS]["select"]["name"] == "上場"
        # 効力発生日が取れないときは上場日キーを送らない (発表日を流用しない §3-1。
        # かつ先行開示で取り込んだ既存の確定日付を {date:None} で消さない)
        assert S.MASTER_PROP_LISTING_DATE not in props
        assert S.MASTER_PROP_LISTED not in props  # listed は codelist 所有

    def test_apply_lifecycle_new_listing_with_date(self, dry_client, monkeypatch):
        """効力発生日が判明している場合は上場日キーを書く(取れた時のみ設定)。"""
        monkeypatch.setattr(dry_client, "query_database", lambda *a, **k: [{"id": "m-300A"}])
        rec = DisclosureRecord(
            doc_id="d6", title="新規上場（効力発生日 2026年4月1日）に関するお知らせ",
            disclosed_at=datetime(2026, 3, 1, tzinfo=JST),
            provenance=prov(source=Source.TDNET, license_tag=LicenseTag.FACTUAL_CITE),
            code="300A", doc_type="新規上場", effective_date=date(2026, 4, 1),
        )
        upsert.apply_disclosure_lifecycle(dry_client, make_settings(), rec)
        props = self._updates(dry_client)[-1].payload["properties"]
        assert props[S.MASTER_PROP_STATUS]["select"]["name"] == "上場"
        assert props[S.MASTER_PROP_LISTING_DATE]["date"]["start"] == "2026-04-01"

    def test_codelist_sync_does_not_clobber_lifecycle(self):
        """master_sync は include_lifecycle=False で 状態/日付 を payload に含めない
        （開示・消失が設定した値を月次同期が消さない §Phase3 二重所有回避）。"""
        rec = StockMasterRecord(code="7203", name="トヨタ自動車", provenance=prov())
        props = upsert.stock_master_properties(rec, include_lifecycle=False)
        assert S.MASTER_PROP_STATUS not in props
        assert S.MASTER_PROP_LISTING_DATE not in props
        assert S.MASTER_PROP_DELISTING_DATE not in props
        # codelist 所有フィールドは常に書く
        assert S.MASTER_PROP_LISTED in props
        assert S.MASTER_PROP_NAME in props

    def test_apply_lifecycle_master_not_found_noop(self, dry_client, monkeypatch):
        monkeypatch.setattr(dry_client, "query_database", lambda *a, **k: [])
        rec = DisclosureRecord(
            doc_id="d3", title="上場廃止のお知らせ",
            disclosed_at=datetime(2026, 5, 8, tzinfo=JST),
            provenance=prov(source=Source.TDNET, license_tag=LicenseTag.FACTUAL_CITE),
            code="7203", doc_type="上場廃止",
        )
        assert upsert.apply_disclosure_lifecycle(dry_client, make_settings(), rec) is None
        assert self._updates(dry_client) == []

    def test_apply_lifecycle_ignores_non_lifecycle_types(self, dry_client):
        rec = DisclosureRecord(
            doc_id="d4", title="決算短信",
            disclosed_at=datetime(2026, 5, 8, tzinfo=JST),
            provenance=prov(source=Source.TDNET, license_tag=LicenseTag.FACTUAL_CITE),
            code="7203", doc_type="短信",
        )
        assert upsert.apply_disclosure_lifecycle(dry_client, make_settings(), rec) is None
        assert self._updates(dry_client) == []


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


class TestFinancialSummaryDisclosedAtGuard:
    """#14: 古い原報告の再実行で訂正後の値を巻き戻さない開示日時ガード。"""

    def _existing_page(self, page_id: str, disclosed_at_iso: str | None):
        return {
            "id": page_id,
            "properties": {
                S.FIN_PROP_DISCLOSED_AT: (
                    {"date": {"start": disclosed_at_iso}}
                    if disclosed_at_iso
                    else {"date": None}
                ),
            },
        }

    def test_older_report_does_not_overwrite_newer_existing(self, dry_client, monkeypatch):
        """既存(開示日=6/10)より古い報告(開示日=5/1)は上書きしない。"""
        existing = self._existing_page(
            "fin-7203", datetime(2026, 6, 10, tzinfo=JST).isoformat()
        )
        monkeypatch.setattr(dry_client, "query_database", lambda *a, **k: [existing])
        settings = make_settings()
        rec = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
            net_sales=999.0,  # 古い訂正前の値を模す
            disclosed_at=datetime(2026, 5, 1, tzinfo=JST),
            provenance=prov(),
        )
        page_id = upsert.upsert_financial_summary(dry_client, settings, rec)
        assert page_id == "fin-7203"
        assert [o for o in dry_client.ops if o.op in ("update_page", "create_page")] == []

    def test_newer_report_overwrites_older_existing(self, dry_client, monkeypatch):
        """既存(開示日=5/1)より新しい訂正(開示日=6/10)は正しく上書きする。"""
        existing = self._existing_page(
            "fin-7203", datetime(2026, 5, 1, tzinfo=JST).isoformat()
        )
        monkeypatch.setattr(dry_client, "query_database", lambda *a, **k: [existing])
        settings = make_settings()
        rec = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
            net_sales=1000.0,
            disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            provenance=prov(),
        )
        page_id = upsert.upsert_financial_summary(dry_client, settings, rec)
        assert page_id == "fin-7203"
        (op,) = [o for o in dry_client.ops if o.op == "update_page"]
        assert op.payload["page_id"] == "fin-7203"

    def test_same_disclosed_at_is_idempotent_and_overwrites(self, dry_client, monkeypatch):
        """同一開示の再実行（開示日が同じ）は従来どおり上書きされる（冪等 upsert）。"""
        same = datetime(2026, 6, 10, tzinfo=JST)
        existing = self._existing_page("fin-7203", same.isoformat())
        monkeypatch.setattr(dry_client, "query_database", lambda *a, **k: [existing])
        settings = make_settings()
        rec = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
            disclosed_at=same, provenance=prov(),
        )
        upsert.upsert_financial_summary(dry_client, settings, rec)
        assert len([o for o in dry_client.ops if o.op == "update_page"]) == 1

    def test_unknown_incoming_disclosed_at_does_not_block(self, dry_client, monkeypatch):
        """今回の開示日が不明なら順序を判断できないので推測でブロックしない (§3-1)。"""
        existing = self._existing_page(
            "fin-7203", datetime(2026, 6, 10, tzinfo=JST).isoformat()
        )
        monkeypatch.setattr(dry_client, "query_database", lambda *a, **k: [existing])
        settings = make_settings()
        rec = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
            disclosed_at=None, provenance=prov(),
        )
        upsert.upsert_financial_summary(dry_client, settings, rec)
        assert len([o for o in dry_client.ops if o.op == "update_page"]) == 1

    def test_existing_with_unknown_disclosed_at_always_allows_overwrite(
        self, dry_client, monkeypatch
    ):
        """既存側の開示日が不明（過去にNULLで書かれた行）なら常に上書きを許可する。"""
        existing = self._existing_page("fin-7203", None)
        monkeypatch.setattr(dry_client, "query_database", lambda *a, **k: [existing])
        settings = make_settings()
        rec = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
            disclosed_at=datetime(2026, 1, 1, tzinfo=JST), provenance=prov(),
        )
        upsert.upsert_financial_summary(dry_client, settings, rec)
        assert len([o for o in dry_client.ops if o.op == "update_page"]) == 1

    def test_no_double_query_when_page_already_resolved(self, dry_client, monkeypatch):
        """既存有無を一度確定させたら _upsert 内で再クエリしない（往復削減）。"""
        calls = []

        def counting_query(*a, **k):
            calls.append(1)
            return []

        monkeypatch.setattr(dry_client, "query_database", counting_query)
        settings = make_settings()
        rec = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
            disclosed_at=datetime(2026, 6, 10, tzinfo=JST), provenance=prov(),
        )
        upsert.upsert_financial_summary(dry_client, settings, rec)
        assert len(calls) == 1  # _find_page_full の1回だけ（_upsert内の再検索なし）

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


class _RecordingClient:
    """query_database / update_page / create_page の呼び出しを記録するダブル。

    事前マップ運用（page_resolved=True）で per-record 検索が省かれることを
    «query が呼ばれない» で検証するために使う（dry_client は query を記録しない）。
    """

    def __init__(self, *, query_result=None, update_raises=None):
        self.calls = []
        self._query_result = query_result if query_result is not None else []
        self._update_raises = update_raises
        self._n = 0

    def query_database(self, db_id, **kw):
        self.calls.append(("query", db_id))
        return self._query_result

    def update_page(self, page_id, props):
        self.calls.append(("update", page_id))
        if self._update_raises is not None:
            raise self._update_raises
        return {"id": page_id}

    def create_page(self, *, parent, properties):
        self.calls.append(("create", parent["database_id"]))
        self._n += 1
        return {"id": f"created-{self._n}"}


def _api_error(code: str):
    from notion_client.errors import APIResponseError

    class _Resp:
        status_code = 404
        headers: dict = {}
        text = "{}"

        def json(self):
            return {}

    return APIResponseError(_Resp(), "boom", code)


class TestPrefetchedPageMap:
    """事前マップ(page_resolved=True)で per-record 検索を省く (§8.3)。

    全銘柄ループの 1req/銘柄 削減。all-or-nothing で渡すこと・stale page_id の
    create フォールバックが本クラスの回帰防止対象。
    """

    def test_resolved_existing_updates_without_query(self):
        c = _RecordingClient()
        pid = upsert._upsert(
            c, "db-master", {"k": 1}, {"p": 1}, existing_page_id="p-7203", page_resolved=True
        )
        assert pid == "p-7203"
        assert ("query", "db-master") not in c.calls  # 検索を省いた
        assert ("update", "p-7203") in c.calls

    def test_resolved_missing_creates_without_query(self):
        c = _RecordingClient()
        pid = upsert._upsert(
            c, "db-master", {"k": 1}, {"p": 1}, existing_page_id=None, page_resolved=True
        )
        assert pid == "created-1"
        # 未収録キーは作成前に検索せず create する。作成後の 1 回は重複の再確認 (#13)
        assert c.calls == [("create", "db-master"), ("query", "db-master")]

    def test_unresolved_falls_back_to_query(self):
        # page_resolved=False（マップ取得失敗の degrade）→ 従来どおり per-record 検索
        c = _RecordingClient(query_result=[{"id": "found"}])
        pid = upsert._upsert(c, "db-master", {"k": 1}, {"p": 1}, page_resolved=False)
        assert pid == "found"
        assert ("query", "db-master") in c.calls
        assert ("update", "found") in c.calls

    def test_stale_page_id_self_heals_to_create(self):
        # 事前マップの page_id が実在しない（削除済み）→ object_not_found を握って create
        c = _RecordingClient(update_raises=_api_error("object_not_found"))
        pid = upsert._upsert(
            c, "db-master", {"k": 1}, {"p": 1}, existing_page_id="stale", page_resolved=True
        )
        assert pid == "created-1"
        assert ("update", "stale") in c.calls
        assert ("create", "db-master") in c.calls

    def test_non_object_not_found_error_propagates(self):
        # 他のAPIエラーは握り潰さず伝播（隠れた書き込み失敗にしない）
        c = _RecordingClient(update_raises=_api_error("validation_error"))
        with pytest.raises(Exception):  # noqa: B017 - APIResponseError 伝播の確認
            upsert._upsert(
                c, "db-master", {"k": 1}, {"p": 1}, existing_page_id="x", page_resolved=True
            )


class TestDisclosurePrefetch:
    """④/① 事前マップで開示ジョブの per-record 検索を排除する (§8.3)。"""

    def test_load_disclosure_page_map_reads_doc_id_and_date_scopes(self):
        # ④ は doc_id(rich_text)キー。disclosed_date 指定で [d, d+1) の date フィルタを送る
        settings = make_settings()
        captured = {}

        class _C:
            def query_database(self, db_id, *, filter=None, **kw):
                captured["db_id"] = db_id
                captured["filter"] = filter
                return [
                    {"id": "d-1", "properties": {S.DISC_PROP_DOC_ID: {"rich_text": [{"plain_text": "S100A"}]}}},
                    {"id": "d-2", "properties": {S.DISC_PROP_DOC_ID: {"rich_text": []}}},  # 空はスキップ
                ]

        out = upsert.load_disclosure_page_map(_C(), settings, disclosed_date=date(2026, 6, 28))
        assert out == {"S100A": "d-1"}
        assert captured["db_id"] == "db-disc"
        # 半開区間 [2026-06-28, 2026-06-29)
        conds = captured["filter"]["and"]
        assert conds[0] == {"property": S.DISC_PROP_DISCLOSED_AT, "date": {"on_or_after": "2026-06-28T00:00:00+09:00"}}
        assert conds[1] == {"property": S.DISC_PROP_DISCLOSED_AT, "date": {"before": "2026-06-29T00:00:00+09:00"}}

    def test_load_disclosure_page_map_no_date_sends_no_filter(self):
        settings = make_settings()
        captured = {}

        class _C:
            def query_database(self, db_id, *, filter=None, **kw):
                captured["filter"] = filter
                return []

        upsert.load_disclosure_page_map(_C(), settings)
        assert captured["filter"] is None

    def test_load_disclosure_page_entries_keeps_properties(self):
        settings = make_settings()

        class _C:
            def query_database(self, db_id, *, filter=None, **kw):
                return [
                    {
                        "id": "d-1",
                        "properties": {
                            S.DISC_PROP_DOC_ID: {"rich_text": [{"plain_text": "S100A"}]},
                            S.DISC_PROP_TITLE: {"title": [{"plain_text": "決算短信"}]},
                        },
                    },
                ]

        out = upsert.load_disclosure_page_entries(_C(), settings, disclosed_date=date(2026, 6, 28))
        assert out["S100A"][0] == "d-1"
        assert out["S100A"][1][S.DISC_PROP_TITLE]["title"][0]["plain_text"] == "決算短信"


def _disc_page(**overrides):
    """④ の query 返却形状（読み取り形）の properties を作る。"""

    def text(value):
        return {"rich_text": [{"plain_text": value}]} if value else {"rich_text": []}

    props = {
        S.DISC_PROP_TITLE: {"title": [{"plain_text": "決算短信"}]},
        S.DISC_PROP_DISCLOSED_AT: {"date": {"start": "2026-06-28T15:00:00+09:00"}},
        S.DISC_PROP_DOC_TYPE: {"select": {"name": "短信"}},
        S.DISC_PROP_DOC_ID: text("S100A"),
        S.DISC_PROP_HAS_XBRL: {"checkbox": True},
        S.DISC_PROP_CODE: text("7203"),
        S.DISC_PROP_URL: {"url": "https://example/doc"},
        S.DISC_PROP_SPLIT_RATIO: text(""),
        S.DISC_PROP_SPLIT_FACTOR: {"number": None},
        S.DISC_PROP_EFFECTIVE_DATE: {"date": None},
        S.PROP_MASTER_RELATION: {"relation": [{"id": "m-1"}]},
        S.PROP_RAW_RELATION: {"relation": [{"id": "raw-1"}]},
        # 取得日時は古いまま（同値 skip では見ない）。
        S.PROP_FETCHED_AT: {"date": {"start": "2026-06-28T16:00:00+09:00"}},
    }
    props.update(overrides)
    return props


def _disc_rec(**overrides):
    kwargs = dict(
        doc_id="S100A",
        title="決算短信",
        disclosed_at=datetime(2026, 6, 28, 15, 0, tzinfo=JST),
        provenance=prov(raw_page_id="raw-1"),
        code="7203",
        doc_type="短信",
        source_url="https://example/doc",
        has_xbrl=True,
    )
    kwargs.update(overrides)
    return DisclosureRecord(**kwargs)


class TestDisclosureMatchesPage:
    """L-20: 既存行と同値なら再 PATCH を省く（毎時 395 件 → 差分のみ）。"""

    def test_同値なら真(self):
        assert upsert.disclosure_matches_page(_disc_page(), _disc_rec(), "m-1") is True

    def test_取得日時が違っても同値(self):
        rec = _disc_rec(provenance=prov(raw_page_id="raw-1", fetched_at=datetime(2026, 7, 1, 12, 0)))
        assert upsert.disclosure_matches_page(_disc_page(), rec, "m-1") is True

    def test_日時の表記揺れを吸収する(self):
        """Notion が Z 正規化で返しても skip が死なない。"""
        props = _disc_page(**{S.DISC_PROP_DISCLOSED_AT: {"date": {"start": "2026-06-28T06:00:00Z"}}})
        # JST 15:00 == UTC 06:00
        assert upsert.disclosure_matches_page(props, _disc_rec(), "m-1") is True

    def test_master_noneならrelationがあっても同値(self):
        """relation を書かない run が既存 relation で不一致にしない。"""
        assert upsert.disclosure_matches_page(_disc_page(), _disc_rec(), None) is True

    def test_原本が変われば偽(self):
        """⑤ を上げ直した run は relation を張り替えるため PATCH する。"""
        rec = _disc_rec(provenance=prov(raw_page_id="raw-2"))
        assert upsert.disclosure_matches_page(_disc_page(), rec, "m-1") is False

    @pytest.mark.parametrize(
        "prop,value",
        [
            (S.DISC_PROP_TITLE, {"title": [{"plain_text": "訂正短信"}]}),
            (S.DISC_PROP_DOC_TYPE, {"select": {"name": "その他"}}),
            (S.DISC_PROP_HAS_XBRL, {"checkbox": False}),
            (S.DISC_PROP_CODE, {"rich_text": []}),
            (S.DISC_PROP_URL, {"url": None}),
            (S.DISC_PROP_SPLIT_FACTOR, {"number": 3.0}),
        ],
    )
    def test_意味が違えば偽(self, prop, value):
        assert upsert.disclosure_matches_page(_disc_page(**{prop: value}), _disc_rec(), "m-1") is False

    def test_masterが変われば偽(self):
        props = _disc_page(**{S.PROP_MASTER_RELATION: {"relation": [{"id": "m-2"}]}})
        assert upsert.disclosure_matches_page(props, _disc_rec(), "m-1") is False

    def test_読めない形は偽に倒す(self):
        assert upsert.disclosure_matches_page({}, _disc_rec(), "m-1") is False

    def test_upsert_disclosure_resolved_skips_dedup_query(self):
        settings = make_settings()
        c = _RecordingClient()
        rec = DisclosureRecord(
            doc_id="S100A", title="決算短信",
            disclosed_at=datetime(2026, 6, 28, 15, 0, tzinfo=JST), provenance=prov(),
        )
        pid = upsert.upsert_disclosure(
            c, settings, rec, existing_page_id="d-existing", page_resolved=True
        )
        assert pid == "d-existing"
        assert ("query", "db-disc") not in c.calls  # 検索を省いた
        assert ("update", "d-existing") in c.calls

    def test_apply_lifecycle_resolved_skips_master_query(self):
        # master_resolved=True → 内部の ① find をせず master_page_id を直接使う
        settings = make_settings()
        c = _RecordingClient()
        rec = DisclosureRecord(
            doc_id="S100A", title="上場廃止に関するお知らせ",
            disclosed_at=datetime(2026, 6, 28, tzinfo=JST), provenance=prov(),
            code="7203", doc_type="上場廃止",
        )
        pid = upsert.apply_disclosure_lifecycle(
            c, settings, rec, master_page_id="m-7203", master_resolved=True
        )
        assert pid == "m-7203"
        assert ("query", "db-master") not in c.calls  # ① 検索を省いた
        assert ("update", "m-7203") in c.calls

    def test_apply_lifecycle_resolved_none_is_noop(self):
        # 事前マップで「① に該当なし」が確定(master_page_id=None) → 検索せず no-op
        settings = make_settings()
        c = _RecordingClient()
        rec = DisclosureRecord(
            doc_id="S100A", title="上場廃止", disclosed_at=datetime(2026, 6, 28, tzinfo=JST),
            provenance=prov(), code="9999", doc_type="上場廃止",
        )
        pid = upsert.apply_disclosure_lifecycle(
            c, settings, rec, master_page_id=None, master_resolved=True
        )
        assert pid is None
        assert c.calls == []  # 検索も更新もしない
