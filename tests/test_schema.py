"""notion/schema.py のテスト (§6, P0)。

dry-run の NotionClient (conftest の dry_client) を使い、実 Notion へは接続しない。
書き込み操作は client.ops に記録される。
"""

from __future__ import annotations

import pytest

from jp_stock_pipeline.config import DB_REGISTRY, load_settings
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import ConvertStatus, DataQuality, Source
from jp_stock_pipeline.notion import schema


def make_settings():
    """dry-run 設定 (db_ids.json / 環境変数に依存しない)。"""
    return load_settings(dry_run=True, env={"NOTION_DB_IDS_FILE": "/nonexistent/db_ids.json"})


@pytest.fixture
def ensured(dry_client):
    settings = make_settings()
    db_ids = schema.ensure_all(dry_client, settings)
    return dry_client, settings, db_ids


def _creates(client):
    return [op for op in client.ops if op.op == "create_database"]


def _props_by_title(client) -> dict[str, dict]:
    return {
        op.payload["title"][0]["text"]["content"]: op.payload["properties"]
        for op in _creates(client)
    }


class TestEnsureAll:
    def test_creates_four_databases(self, ensured):
        client, _settings, _db_ids = ensured
        assert len(_creates(client)) == 4

    def test_titles_match_db_registry(self, ensured):
        client, _settings, _db_ids = ensured
        titles = {op.payload["title"][0]["text"]["content"] for op in _creates(client)}
        assert titles == {title for _env, title in DB_REGISTRY.values()}

    def test_creation_order_raw_first_then_master(self, ensured):
        """⑤→①の順 (relation 先が先に存在する必要がある §6.2)。"""
        client, _settings, _db_ids = ensured
        titles = [op.payload["title"][0]["text"]["content"] for op in _creates(client)]
        assert titles[0] == DB_REGISTRY["raw_files"][1]
        assert titles[1] == DB_REGISTRY["stock_master"][1]

    def test_returns_db_ids_for_all_registry_keys(self, ensured):
        _client, _settings, db_ids = ensured
        assert set(db_ids) == set(DB_REGISTRY)
        assert all(isinstance(v, str) and v for v in db_ids.values())

    def test_parent_page_is_settings_parent(self, ensured):
        client, settings, _db_ids = ensured
        for op in _creates(client):
            assert op.payload["parent"]["page_id"] == settings.notion_parent_page_id


class TestCommonProperties:
    """§6.3 共通プロパティの適用範囲。"""

    COMMON = (
        schema.PROP_SOURCE,
        schema.PROP_LICENSE_TAG,
        schema.PROP_DATA_DATE,
        schema.PROP_FETCHED_AT,
        schema.PROP_RAW_RELATION,
        schema.PROP_QUALITY,
    )

    def test_full_common_on_master_fin_disc(self, ensured):
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)
        for key in ("stock_master", "financials", "disclosures"):
            title = DB_REGISTRY[key][1]
            for name in self.COMMON:
                assert name in props[title], f"{title} に {name} が無い"

    def test_raw_files_has_subset_only(self, ensured):
        """⑤: ソース/ライセンスタグ/データ基準日/取得日時のみ。原本relation・品質なし。"""
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)[DB_REGISTRY["raw_files"][1]]
        for name in (
            schema.PROP_SOURCE,
            schema.PROP_LICENSE_TAG,
            schema.PROP_DATA_DATE,
            schema.PROP_FETCHED_AT,
        ):
            assert name in props
        assert schema.PROP_RAW_RELATION not in props
        assert schema.PROP_QUALITY not in props
        assert schema.RAW_PROP_CONVERT_STATUS in props  # 代わりに変換状態 select


class TestSelectOptionsMatchEnums:
    """select の選択肢は models / licensing の enum 値と一致する (§6.3)。"""

    def _options(self, props, name):
        return [o["name"] for o in props[name]["select"]["options"]]

    def test_source_options(self, ensured):
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)[DB_REGISTRY["stock_master"][1]]
        assert self._options(props, schema.PROP_SOURCE) == [s.value for s in Source]

    def test_license_options(self, ensured):
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)[DB_REGISTRY["stock_master"][1]]
        assert self._options(props, schema.PROP_LICENSE_TAG) == [t.value for t in LicenseTag]

    def test_quality_options(self, ensured):
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)[DB_REGISTRY["stock_master"][1]]
        assert self._options(props, schema.PROP_QUALITY) == [q.value for q in DataQuality]

    def test_convert_status_options(self, ensured):
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)[DB_REGISTRY["raw_files"][1]]
        assert self._options(props, schema.RAW_PROP_CONVERT_STATUS) == [
            c.value for c in ConvertStatus
        ]


class TestSpecificProperties:
    def test_master_specific(self, ensured):
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)[DB_REGISTRY["stock_master"][1]]
        assert props[schema.MASTER_PROP_NAME] == {"title": {}}
        assert "rich_text" in props[schema.MASTER_PROP_CODE]
        for name in (
            schema.MASTER_PROP_MARKET,
            schema.MASTER_PROP_SECTOR33,
            schema.MASTER_PROP_SECTOR17,
        ):
            assert "select" in props[name]
        assert "checkbox" in props[schema.MASTER_PROP_LISTED]
        # ライフサイクル (§ Phase3): 状態 select(選択肢一致) + 上場日/上場廃止日 date
        status_opts = [o["name"] for o in props[schema.MASTER_PROP_STATUS]["select"]["options"]]
        assert status_opts == list(schema.LISTING_STATUS_OPTIONS)
        assert "date" in props[schema.MASTER_PROP_LISTING_DATE]
        assert "date" in props[schema.MASTER_PROP_DELISTING_DATE]

    def test_financials_specific(self, ensured):
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)[DB_REGISTRY["financials"][1]]
        assert props[schema.FIN_PROP_TITLE] == {"title": {}}
        assert "date" in props[schema.FIN_PROP_PERIOD_END]
        opts = [o["name"] for o in props[schema.FIN_PROP_DISCLOSURE_TYPE]["select"]["options"]]
        assert opts == list(schema.DISCLOSURE_TYPES)

    def test_disclosures_specific(self, ensured):
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)[DB_REGISTRY["disclosures"][1]]
        assert "rich_text" in props[schema.DISC_PROP_DOC_ID]
        assert "checkbox" in props[schema.DISC_PROP_HAS_XBRL]
        opts = [o["name"] for o in props[schema.DISC_PROP_DOC_TYPE]["select"]["options"]]
        assert opts == list(schema.DOC_TYPES)
        assert "株式分割" in opts and "上場廃止" in opts  # コーポレートアクション (§ Phase2)
        # 分割比率(text)/分割係数(number)/効力発生日(date) (§ Phase2/4)
        assert "rich_text" in props[schema.DISC_PROP_SPLIT_RATIO]
        assert "number" in props[schema.DISC_PROP_SPLIT_FACTOR]
        assert "date" in props[schema.DISC_PROP_EFFECTIVE_DATE]

    def test_raw_files_specific(self, ensured):
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)[DB_REGISTRY["raw_files"][1]]
        assert props[schema.RAW_PROP_FILENAME] == {"title": {}}
        assert "files" in props[schema.RAW_PROP_FILES]
        assert "rich_text" in props[schema.RAW_PROP_SHA256]
        assert "number" in props[schema.RAW_PROP_SIZE]

    def test_relations_are_dual_property(self, ensured):
        client, _settings, _db_ids = ensured
        props = _props_by_title(client)[DB_REGISTRY["financials"][1]]
        for name in (schema.PROP_MASTER_RELATION, schema.PROP_RAW_RELATION):
            assert props[name]["relation"]["type"] == "dual_property"


class TestRawRelatedMasterBackfill:
    def test_raw_gets_related_master_relation_after_master_created(self, ensured):
        """⑤の「関連銘柄」relation→① は①作成後に update_database で後付けされる。"""
        client, _settings, _db_ids = ensured
        updates = [op for op in client.ops if op.op == "update_database"]
        assert any(
            schema.RAW_PROP_RELATED_MASTER in op.payload.get("properties", {}) for op in updates
        )


class TestCatalogPage:
    def test_catalog_page_created(self, ensured):
        client, settings, _db_ids = ensured
        pages = [op for op in client.ops if op.op == "create_page"]
        catalog = [
            op
            for op in pages
            if op.payload["properties"]["title"]["title"][0]["text"]["content"]
            == schema.CATALOG_TITLE
        ]
        assert len(catalog) == 1
        assert catalog[0].payload["parent"]["page_id"] == settings.notion_parent_page_id

    def test_catalog_mentions_license_and_disclaimer(self, ensured):
        client, _settings, db_ids = ensured
        blocks = schema.catalog_blocks(db_ids)
        text = " ".join(
            rt["text"]["content"]
            for b in blocks
            for rt in b[b["type"]].get("rich_text", [])
        )
        assert "commercial-ok" in text
        assert "投資助言ではありません" in text
        assert "EDINET（金融庁）" in text  # licensing.ATTRIBUTION の出典表記

    def test_catalog_lists_all_schema_select_options(self, ensured):
        """ドリフト検知: カタログ説明文に各 select の全選択肢が載っているか。

        _CATALOG_DICTIONARY (説明文) は schema 実定数 (DOC_TYPES /
        LISTING_STATUS_OPTIONS / SOURCE_OPTIONS) と二重管理になるため、定数へ
        選択肢を足して説明を更新し忘れると本テストが落ちる (§7 ドリフト防止)。
        """
        _client, _settings, db_ids = ensured
        blocks = schema.catalog_blocks(db_ids)
        text = " ".join(
            rt["text"]["content"]
            for b in blocks
            for rt in b[b["type"]].get("rich_text", [])
        )
        for value in schema.DOC_TYPES:
            assert value in text, f"④書類種別 '{value}' がカタログ説明に未記載"
        for value in schema.LISTING_STATUS_OPTIONS:
            assert value in text, f"①状態 '{value}' がカタログ説明に未記載"
        for value in schema.SOURCE_OPTIONS:
            assert value in text, f"ソース '{value}' がカタログ説明に未記載"

    def test_recommended_views_reference_known_dbs(self):
        """ビューはAPI未対応のため定数定義+カタログ記載 (§7)。"""
        for view in schema.RECOMMENDED_VIEWS:
            assert view["db"] in DB_REGISTRY
            assert view["name"]
            assert view["setup"]


class TestMissingProperties:
    """既存DBがある場合の不足プロパティ差分計算 (冪等性)。"""

    def test_returns_only_missing(self):
        desired = {"A": {"rich_text": {}}, "B": {"number": {}}}
        existing = {"A": {"id": "x", "type": "rich_text", "rich_text": {}}}
        assert schema.missing_properties(desired, existing) == {"B": {"number": {}}}

    def test_does_not_touch_existing_with_different_type(self):
        """同名プロパティは型が違っても変更しない (型変更・削除禁止)。"""
        desired = {"A": {"number": {}}}
        existing = {"A": {"id": "x", "type": "select", "select": {"options": []}}}
        assert schema.missing_properties(desired, existing) == {}

    def test_all_missing_when_existing_empty(self):
        desired = {"A": {"date": {}}}
        assert schema.missing_properties(desired, {}) == {"A": {"date": {}}}

    def test_nothing_missing_when_identical(self):
        desired = schema.raw_files_schema()
        existing = {name: {"id": "x", **spec} for name, spec in desired.items()}
        assert schema.missing_properties(desired, existing) == {}


class TestEnsureDatabaseIdempotency:
    def test_existing_db_gets_only_missing_props(self, dry_client, monkeypatch):
        """既存DB発見時は create せず不足分のみ update_database。"""
        settings = make_settings()
        setup = schema.SchemaSetup(dry_client, settings)
        title = DB_REGISTRY["disclosures"][1]
        # 親ページ子ブロックに既存DBがあるとみなす
        monkeypatch.setattr(
            setup,
            "_parent_children",
            lambda: [{"id": "db-existing", "type": "child_database", "child_database": {"title": title}}],
        )
        desired = schema.disclosures_schema("db-master", "db-raw")
        existing_props = dict(desired)
        removed = schema.DISC_PROP_URL
        existing_props.pop(removed)
        monkeypatch.setattr(
            dry_client, "retrieve_database", lambda db_id: {"properties": existing_props}
        )
        ensured = setup.ensure_database("disclosures", desired)
        assert ensured.db_id == "db-existing"
        assert ensured.created is False
        creates = [op for op in dry_client.ops if op.op == "create_database"]
        assert creates == []
        updates = [op for op in dry_client.ops if op.op == "update_database"]
        assert len(updates) == 1
        assert set(updates[0].payload["properties"]) == {removed}
