"""列単位ライセンス地図の投入と照合 (`jobs/license_map.py`) のテスト。

このジョブが埋めた穴は「宣言に実行主体が無い」こと。`seed_reference_tables()` は
定義と `__all__` とテストだけがあり、`jobs/*.py` からの呼び出しが 0 件だった。
本番 `jss_column_license` の 7 行は経路外で一度だけ手で入れられたもので、
宣言を直しても実表に届かない状態だった。

したがってここで固定するのは「関数が動く」ではなく **実行される経路が
存在すること**と、**upsert では消えない孤児宣言が消えること**である。
"""

from __future__ import annotations

import sqlite3

import pytest

from jp_stock_pipeline.cloud_store import schema as S
from jp_stock_pipeline.cloud_store.d1 import D1Error, D1Store
from jp_stock_pipeline.config import CloudStoreSettings
from jp_stock_pipeline.jobs import license_map, runner

_D1_ENV = {
    "NOTION_TOKEN": "dummy-token",
    "CF_ACCOUNT_ID": "acct",
    "CF_API_TOKEN": "token",
    "CF_D1_DATABASE_ID": "db",
}


class _FakeStore(D1Store):
    """`query()` だけをローカル sqlite へ差し替えた D1Store。

    `upsert()` を自前で書かずに継承するのが要点。上書きすると
    「ON CONFLICT の SET 句をどう組み立てるか」という本番の振る舞いを
    テスト側で作文してしまい、`seed_reference_tables` が本当に届くのかを
    確かめられなくなる。
    """

    def __init__(self) -> None:
        super().__init__(CloudStoreSettings(), writer="test")
        self.con = sqlite3.connect(":memory:")
        for stmt in S.SCHEMA_STATEMENTS:
            self.con.execute(stmt)
        self.con.commit()
        self.sql_log: list[str] = []

    def query(self, sql: str, params: list | None = None, *, idempotent: bool = True):
        self.sql_log.append(sql)
        try:
            cur = self.con.execute(sql, params or [])
        except sqlite3.Error as exc:
            raise D1Error(f"sqlite: {exc} sql={sql[:120]!r}") from exc
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        self.con.commit()
        return rows

    @property
    def write_sql(self) -> list[str]:
        return [
            s for s in self.sql_log
            if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        ]

    @property
    def reference_writes(self) -> list[str]:
        """参照表への書き込みだけ。

        `jss_job_runs` への 1 行は `runner.run_job` が全ジョブ共通で書くもので、
        このジョブの判断ではない（ops_check.yml の第3ステップも同じ行を書く）。
        """
        return [s for s in self.write_sql if "jss_job_runs" not in s]

    def license_rows(self) -> set[tuple[str, str, str]]:
        return {
            (r["table_name"], r["column_name"], r["license_tag"])
            for r in self.query(S.COLUMN_LICENSE_SQL)
        }


def _wire(monkeypatch, store: _FakeStore) -> None:
    from jp_stock_pipeline.cloud_store import d1 as d1_module

    def factory(settings, *, writer, database_id=None):
        return store

    monkeypatch.setattr(license_map, "D1Store", factory)
    monkeypatch.setattr(d1_module, "D1Store", factory)
    monkeypatch.setattr(runner, "write_job_log", lambda *a, **k: "dummy")
    monkeypatch.setattr(runner, "connect_local_store", lambda *a, **k: None)


class TestSeedHasARunnableEntryPoint:
    def test_実行すると宣言が実表へ届く(self, monkeypatch) -> None:
        """`grep` で 0 件だった「本番の呼び出し元」がここ。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert store.license_rows() == set()

        assert license_map.main([], env=dict(_D1_ENV)) == 0
        assert store.license_rows() == {tuple(r) for r in S.column_license_rows()}

    def test_二度流しても同じ結果になる(self, monkeypatch) -> None:
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        first = store.license_rows()
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        assert store.license_rows() == first

    def test_dry_runは1文も書き込まない(self, monkeypatch) -> None:
        """`run_job` 経由で確認する。

        `ctx.cloud` は dry-run では張られないので、ジョブ側が `ctx.cloud` に
        依存していると dry-run が即失敗する（`ops_check` が実際に踏んだ）。
        fake store を直接注入するだけではその欠陥を踏まずに緑になる。
        """
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main(["--dry-run"], env=dict(_D1_ENV)) == 0
        assert store.write_sql == []
        assert store.license_rows() == set()


class TestOrphanDeclarations:
    def test_宣言から外した列の行が消える(self, monkeypatch) -> None:
        """upsert は `conflict=(table_name, column_name)` で DELETE を伴わない。

        `commercial-ok` の孤児が残ると「公開してよい」と宣言したまま誰も
        管理していない列ができる。これが残らないことを固定する。
        """
        store = _FakeStore()
        _wire(monkeypatch, store)
        store.query(
            "INSERT INTO jss_column_license (table_name, column_name, license_tag)"
            " VALUES ('core_stocks', 'removed_column', 'commercial-ok')"
        )
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        assert ("core_stocks", "removed_column", "commercial-ok") not in store.license_rows()
        assert store.license_rows() == {tuple(r) for r in S.column_license_rows()}

    def test_指数シンボルの孤児は削除せず報告する(self, monkeypatch, caplog) -> None:
        """`r2_key` が実オブジェクトを指すので消さない（`r2.py` に削除 API が無い）。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        store.query(
            "INSERT INTO jss_index_symbols (slug, yahoo_symbol, r2_key, license_tag)"
            " VALUES ('retired', '^X', 'index/retired.json', 'personal-only')"
        )
        with caplog.at_level("WARNING"):
            assert license_map.main([], env=dict(_D1_ENV)) == 0
        slugs = {r["slug"] for r in store.query("SELECT slug FROM jss_index_symbols")}
        assert "retired" in slugs, "R2 オブジェクトを孤立させるので自動削除しない"
        assert "retired" in caplog.text

    def test_未確認シンボルは欠損として扱わない(self, monkeypatch) -> None:
        """`nkvi` は Yahoo シンボル未確認で投入しない = 実表に無いのが正常。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        diff = S.index_symbol_diff(store.query(S.INDEX_SYMBOLS_SQL))
        assert diff.clean, diff


class TestDoesNotScanRows:
    """D1 は走査行課金。実データの表を 1 行も読まないこと。"""

    def test_読むのは_sqlite_master_と参照表だけ(self, monkeypatch) -> None:
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        selects = [s for s in store.sql_log if s.lstrip().upper().startswith("SELECT")]
        # 参照表（宣言の件数と同じオーダー）と sqlite_master 以外は読まない。
        allowed = ("jss_column_license", "jss_index_symbols", "sqlite_master")
        for sql in selects:
            assert any(name in sql for name in allowed), sql

    def test_日次で_DDL_を流さない(self, monkeypatch) -> None:
        """`apply_schema` の 24 文を毎日 no-op で投げると往復時間だけ増える。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        assert not [
            s for s in store.sql_log if s.lstrip().upper().startswith(("CREATE", "ALTER"))
        ]


class TestMissingTables:
    def test_表が無ければ失敗して_apply_schema_を促す(self, monkeypatch) -> None:
        """ブートストラップを日次 cron の仕事にしない（1 回きりの操作）。"""
        store = _FakeStore()
        store.con.execute("DROP TABLE jss_column_license")
        store.con.commit()
        _wire(monkeypatch, store)
        assert license_map.main([], env=dict(_D1_ENV)) == 1
        assert store.reference_writes == [], "表が欠けた状態で参照表へ書き込みを試みない"

    def test_D1未設定なら成功にしない(self, monkeypatch) -> None:
        """「対象に触れなかった」を成功にすると、環境変数の間違いが緑になる。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main([], env={"NOTION_TOKEN": "dummy-token"}) == 1


class TestReferenceDiff:
    """純粋な比較関数。I/O を持たないので単体で固定できる。"""

    def test_タグの食い違いを検出する(self) -> None:
        rows = [
            {"table_name": "core_stocks", "column_name": "sector33",
             "license_tag": "personal-only"},
        ]
        diff = S.column_license_diff(rows)
        assert diff.mismatched[0][1:] == ("personal-only", "commercial-ok")
        assert not diff.clean

    def test_孤児と欠損を区別する(self) -> None:
        diff = S.column_license_diff(
            [{"table_name": "x", "column_name": "y", "license_tag": "commercial-ok"}]
        )
        assert ("x", "y") in diff.orphan
        assert ("core_stocks", "sector") in diff.missing

    @pytest.mark.parametrize("rows", [[], None])
    def test_空でも落ちない(self, rows) -> None:
        assert not S.column_license_diff(rows or []).clean
