"""cloud_store/notion_pages.py のテスト（L-20）。

Notion ① の {コード: page_id} 写しを D1 で持つ。tdnet_hourly の毎時 39 req
スキャンを 1 SELECT に置き換える。D1Store を継承した sqlite fake で
本物の upsert 経路を通す。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from _doubles import SqliteD1

from jp_stock_pipeline.cloud_store import notion_pages as npages
from jp_stock_pipeline.cloud_store import schema as S
from jp_stock_pipeline.cloud_store.d1 import D1Error
from jp_stock_pipeline.config import CloudStoreSettings
from jp_stock_pipeline.jobs import master_sync
from jp_stock_pipeline.notion import schema as NS


def _FakeStore() -> SqliteD1:
    return SqliteD1(ddl=S.SCHEMA_STATEMENTS)


class TestRoundTrip:
    def test_書いた写しを1文で読める(self) -> None:
        store = _FakeStore()
        assert npages.save_map(store, npages.DB_STOCK_MASTER, {"7203": "p1"}, updated_at=1) == 1
        store.sql_log.clear()
        assert npages.load_stock_master_map(store) == {"7203": "p1"}
        assert store.sql_log == [npages.SELECT_SQL]

    def test_区画は混ざらない(self) -> None:
        store = _FakeStore()
        npages.save_map(store, npages.DB_STOCK_MASTER, {"7203": "p1"}, updated_at=1)
        npages.save_map(store, npages.DB_STOCK_MASTER_BY_EDINET, {"E02144": "7203"}, updated_at=1)
        assert npages.load_stock_master_map(store) == {"7203": "p1"}
        assert npages.load_edinet_code_map(store) == {"E02144": "7203"}

    def test_書き直すと更新される(self) -> None:
        """page_id が変わった行は UPDATE で追随する（作り直しではない）。"""
        store = _FakeStore()
        npages.save_map(store, npages.DB_STOCK_MASTER, {"7203": "p1"}, updated_at=1)
        npages.save_map(store, npages.DB_STOCK_MASTER, {"7203": "p2"}, updated_at=2)
        assert npages.load_stock_master_map(store) == {"7203": "p2"}
        rows = store.query(f"SELECT updated_at FROM {npages.TABLE}")
        assert [r["updated_at"] for r in rows] == [2]

    def test_空マップは書かない(self) -> None:
        store = _FakeStore()
        assert npages.save_map(store, npages.DB_STOCK_MASTER, {}, updated_at=1) == 0
        assert store.sql_log == []

    def test_未知の区画は書かない(self) -> None:
        store = _FakeStore()
        with pytest.raises(ValueError, match="未知の区画"):
            npages.save_map(store, "prices", {"7203": "p1"}, updated_at=1)


class TestReaderContract:
    def test_読む列はcodeとpage_idだけ(self) -> None:
        """SELECT * にすると列追加で読み手が壊れる。列を明示する。"""
        assert npages.SELECT_SQL == (
            f"SELECT code, page_id FROM {npages.TABLE} WHERE db = ?"
        )

    def test_表が無ければD1Error(self) -> None:
        """本番 DDL 前の読み手は例外→Notion スキャンへフォールバックする。"""
        store = _FakeStore()
        store.con.execute(f"DROP TABLE {npages.TABLE}")
        with pytest.raises(D1Error):
            npages.load_stock_master_map(store)


def _master_ctx(*, dry_run: bool = False, limit=None, d1: bool = True):
    cloud = CloudStoreSettings(
        cf_account_id="acct" if d1 else None,
        cf_api_token="token" if d1 else None,
        d1_database_id="db" if d1 else None,
    )
    failures: list[tuple[str, str]] = []
    return SimpleNamespace(
        settings=SimpleNamespace(cloud_store=cloud, dry_run=dry_run),
        args=SimpleNamespace(limit=limit),
        failures=failures,
        add_failure=lambda code, reason="": failures.append((code, reason)),
    )


def _entries():
    return {
        "7203": (
            "p-7203",
            {
                NS.MASTER_PROP_EDINET_CODE: {
                    "rich_text": [{"plain_text": "E02144"}]
                }
            },
        ),
        "9301": ("p-9301", {}),  # EDINETコード無し → 逆引きに出ない
    }


class TestSyncNotionPages:
    """master_sync が月次で写しを書く。"""

    def test_両区画を書く(self, monkeypatch) -> None:
        store = _FakeStore()
        monkeypatch.setattr(master_sync, "D1Store", lambda *a, **k: store)
        ctx = _master_ctx()
        master_sync._sync_notion_pages(ctx, _entries(), True)
        assert ctx.failures == []
        assert npages.load_stock_master_map(store) == {
            "7203": "p-7203", "9301": "p-9301",
        }
        assert npages.load_edinet_code_map(store) == {"E02144": "7203"}

    @pytest.mark.parametrize(
        "kwargs", [{"dry_run": True}, {"limit": 10}, {"d1": False}]
    )
    def test_dry_runとlimitとD1無しでは書かない(self, monkeypatch, kwargs) -> None:
        store = _FakeStore()
        monkeypatch.setattr(master_sync, "D1Store", lambda *a, **k: store)
        ctx = _master_ctx(**kwargs)
        master_sync._sync_notion_pages(ctx, _entries(), True)
        assert npages.load_stock_master_map(store) == {}
        assert ctx.failures == []

    def test_マップ失敗時は書かない(self, monkeypatch) -> None:
        """`{}` で上書きすると写しが空になる（ように見える）。書かない。"""
        store = _FakeStore()
        monkeypatch.setattr(master_sync, "D1Store", lambda *a, **k: store)
        ctx = _master_ctx()
        master_sync._sync_notion_pages(ctx, {}, False)
        assert npages.load_stock_master_map(store) == {}

    def test_D1失敗は記録だけで止めない(self, monkeypatch) -> None:
        """写しが古くても読み手はスキャンへ落ちる。同期は止めない。"""
        from jp_stock_pipeline.cloud_store.d1 import D1Error as _D1Error

        class _Boom(SqliteD1):
            def upsert(self, *a, **k):
                raise _D1Error("boom")

        store = _Boom(ddl=S.SCHEMA_STATEMENTS)
        monkeypatch.setattr(master_sync, "D1Store", lambda *a, **k: store)
        ctx = _master_ctx()
        master_sync._sync_notion_pages(ctx, _entries(), True)
        assert [c for c, _ in ctx.failures] == ["jss_notion_pages"]
