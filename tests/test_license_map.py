"""列単位ライセンス地図の投入と照合 (`jobs/license_map.py`) のテスト。

このジョブが埋めた穴は「宣言に実行主体が無い」こと。`seed_reference_tables()` は
定義と `__all__` とテストだけがあり、`jobs/*.py` からの呼び出しが 0 件だった。
本番 `jss_column_license` の 7 行は経路外で一度だけ手で入れられたもので、
宣言を直しても実表に届かない状態だった。

したがってここで固定するのは「関数が動く」ではなく **実行される経路が
存在すること**と、**upsert では消えない孤児宣言が消えること**である。
"""

from __future__ import annotations

import pytest

from _doubles import SqliteD1

from jp_stock_pipeline.cloud_store import core_stocks as cs
from jp_stock_pipeline.cloud_store import governance as G
from jp_stock_pipeline.cloud_store import schema as S
from jp_stock_pipeline.jobs import license_map, runner

from test_core_stocks_migrate import APPLIED_DDL, PROD_DDL

_D1_ENV = {
    "NOTION_TOKEN": "dummy-token",
    "CF_ACCOUNT_ID": "acct",
    "CF_API_TOKEN": "token",
    "CF_D1_DATABASE_ID": "db",
}


class _FakeStore(SqliteD1):
    """sqlite 裏打ちの D1Store + 本番形の core_stocks と kabulab-cf 所有表。

    `upsert()` を自前で書かずに継承するのが要点。上書きすると
    「ON CONFLICT の SET 句をどう組み立てるか」という本番の振る舞いを
    テスト側で作文してしまい、`seed_reference_tables` が本当に届くのかを
    確かめられなくなる。
    """

    def __init__(self) -> None:
        super().__init__(ddl=S.SCHEMA_STATEMENTS)
        # `core_stocks` は本番の実 DDL + P4a の 12 列。地図が本番の 21 列すべてを
        # 見るので、ここを削ると網羅性の検査が意味を失う。
        self.con.executescript(PROD_DDL)
        for stmt in APPLIED_DDL:
            self.con.execute(stmt)
        # kabulab-cf 所有の表。**区分の宣言から名前を導いている**（ここに
        # リテラルで並べると宣言と二重管理になり、どちらが古いか分からなくなる）。
        # 列の中身は検査が見ないので最小で足りる。
        for name in sorted(G.TABLE_LICENSE):
            if name.startswith("jss_") or name in ("core_stocks", "yutai_benefits"):
                continue
            self.con.execute(f"CREATE TABLE IF NOT EXISTS {name} (id INTEGER PRIMARY KEY)")
        self.con.commit()

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


class _ClaimUpsertLostStore(_FakeStore):
    """`jss_writer_claims` への upsert だけが届かない store。

    投入の直後に読み直す設計なので、writer の食い違いや宣言漏れが照合に残るのは
    「upsert が届かなかった」ときだけである。それを再現するための差し替え。
    他の参照表は通常どおり投入する（そちらの失敗と混ざらないように）。
    """

    def upsert(self, table, columns, rows, *, conflict):
        if table == "jss_writer_claims":
            return 0
        return super().upsert(table, columns, rows, conflict=conflict)


def _wire(monkeypatch, store: _FakeStore) -> None:
    from jp_stock_pipeline.cloud_store import d1 as d1_module

    def factory(settings, *, writer, database_id=None):
        return store

    monkeypatch.setattr(license_map, "D1Store", factory)
    monkeypatch.setattr(d1_module, "D1Store", factory)
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

    def test_一致していれば参照表へ書き込まない(self, monkeypatch) -> None:
        """差分が無い日の upsert は D1 の書き込み課金になるだけ（L-17）。

        2 回目は宣言と実表が一致しているので、参照表への書き込みは
        0 文のはず（`jss_job_runs` の 1 行は runner が全ジョブ共通で書く）。
        """
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        store.sql_log.clear()
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        assert store.reference_writes == []

    def test_食い違いがあればその表だけ書き直す(self, monkeypatch) -> None:
        """片方の表を壊しても、もう片方への書き込みは出ない。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        first = store.license_rows()
        table, column, tag = sorted(first)[0]
        flipped = "personal-only" if tag == "commercial-ok" else "commercial-ok"
        store.con.execute(
            "UPDATE jss_column_license SET license_tag=?"
            " WHERE table_name=? AND column_name=?",
            (flipped, table, column),
        )
        store.con.commit()
        store.sql_log.clear()
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        assert store.license_rows() == first
        assert store.reference_writes != []
        assert all("jss_column_license" in s for s in store.reference_writes)

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
        assert not (diff.missing or diff.mismatched or diff.orphan), diff


class TestDoesNotScanRows:
    """D1 は走査行課金。実データの表を 1 行も読まないこと。"""

    def test_読むのは_sqlite_master_と参照表だけ(self, monkeypatch) -> None:
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        selects = [s for s in store.sql_log if s.lstrip().upper().startswith("SELECT")]
        # 参照表（宣言の件数と同じオーダー）と sqlite_master 以外は読まない。
        allowed = (
            "jss_column_license", "jss_index_symbols", "jss_writer_claims",
            "sqlite_master",
        )
        for sql in selects:
            assert any(name in sql for name in allowed), sql

    def test_日次で_DDL_を流さない(self, monkeypatch) -> None:
        """`apply_schema` の 20 文を毎日 no-op で投げると往復時間だけ増える。"""
        # 文数は docstring の「20 文」と一致させる（ずれたら書き換え忘れ）。
        assert len(S.SCHEMA_STATEMENTS) == 20, len(S.SCHEMA_STATEMENTS)
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
        assert diff.missing or diff.mismatched or diff.orphan

    def test_孤児と欠損を区別する(self) -> None:
        diff = S.column_license_diff(
            [{"table_name": "x", "column_name": "y", "license_tag": "commercial-ok"}]
        )
        assert ("x", "y") in diff.orphan
        assert ("core_stocks", "sector") in diff.missing

    @pytest.mark.parametrize("rows", [[], None])
    def test_空でも落ちない(self, rows) -> None:
        diff = S.column_license_diff(rows or [])
        assert diff.missing or diff.mismatched or diff.orphan


class TestWriterClaims:
    """writer の排他宣言。本番 4 行に対して照合コードが両リポジトリに 0 行だった。"""

    def _claims(self, store: _FakeStore) -> set[tuple[str, str, str]]:
        return {
            (r["dataset"], r["column_group"], r["writer"])
            for r in store.query(G.WRITER_CLAIMS_SQL)
        }

    def test_実行すると宣言が実表へ届く(self, monkeypatch) -> None:
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert self._claims(store) == set()
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        assert self._claims(store) == {
            (c.dataset, c.column_group, c.writer) for c in G.WRITER_CLAIMS
        }

    def test_core_stocks_の_base_は_kabulab_cf_のまま(self, monkeypatch) -> None:
        """`base` を `stockStock` で入れると universe.ts の照合が毎回 throw して

        JPX 母集団同期が止まる。`core_stocks` の行の作成と既存列を書くのは
        kabulab-cf の universe.ts だけで、stockStock が書くのは enrich 群
        （`sector33`）だけである。
        """
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        claims = self._claims(store)
        assert ("core_stocks", "base", "kabulab-cf") in claims
        assert ("core_stocks", "base", "stockStock") not in claims
        assert ("core_stocks", "enrich", "stockStock") in claims

    def test_enrich_の宣言には実際の書込経路がある(self) -> None:
        """writer が居ない群は宣言しない、という規律は enrich を宣言した今も守る。

        以前は「enrich 群は宣言しない」を固定していたが、その意図は
        「writer が居ない群を宣言すると、列を足したが writer を入れ忘れて黙って
        死ぬを検出できなくなる」（`estimate_source_url` は 8,314 行すべて NULL
        なのに `estimated_value` は 5,333 行ある、という状態が実在した）である。
        `master_sync` が `sector33` を書き始めたので enrich = stockStock を期待し、
        その宣言が**実際に呼ばれる書込経路**に裏付けられていることを AST で確かめる
        （docstring の言及を呼び出しと誤認しないよう Call ノードで見る）。
        """
        import ast
        import pathlib

        claims = {(c.dataset, c.column_group): c.writer for c in G.WRITER_CLAIMS}
        assert claims[("core_stocks", "enrich")] == G.WRITER_STOCKSTOCK

        jobs = pathlib.Path(cs.__file__).resolve().parent.parent / "jobs"
        callers = set()
        for path in sorted(jobs.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "build_sector33_updates":
                        callers.add(path.name)
        assert callers == {"master_sync.py"}, callers

    def test_all_を使わず_base_で始める(self) -> None:
        """PK が `(dataset, column_group)` なので後からの改名は破壊的書換になる。

        `core_stocks` を `all` で入れると、P4b で `base` / `enrich` へ割るときに
        `all` 行の DELETE が必要になる。
        """
        for claim in G.WRITER_CLAIMS:
            if claim.column_group == G.COLUMN_GROUP_ALL:
                assert claim.dataset.startswith("jss_"), claim
        assert set(G.COLUMN_GROUPS) == {"all", "base", "enrich"}

    def test_再実行で_updated_at_が動かない(self, monkeypatch) -> None:
        """宣言日を入れる列に実行時刻を入れない（B-8 と同じ間違いを繰り返さない）。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        first = store.query("SELECT dataset, column_group, updated_at FROM jss_writer_claims")
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        assert store.query(
            "SELECT dataset, column_group, updated_at FROM jss_writer_claims"
        ) == first

    def test_2_段リリースの_2_段目に上がっている(self) -> None:
        """1 段目の本番実行（run 34750652448）は食い違い warning 0 件だった。"""
        assert G.CLAIM_MISMATCH_IS_FAILURE is True

    def test_行が無い_DB_への初回は投入してから照合して成功する(self, monkeypatch) -> None:
        """claim 行の無い DB への最初の実行を異常終了させない（鶏と卵）。

        以前は「1 段目では失敗させない」を固定していたが、その意図はブートストラップ
        （投入する側も書込ジョブなので、照合を先にすると初回が必ず落ちる）である。
        2 段目でもその意図は変わらないので、**失敗にしたうえで**初回が成功すること、
        そして成功の理由が「投入 → 照合」の順序であることを SQL の順で確かめる。

        差分書き込み化（L-17）で最初の SELECT は「差分の有無を見る読み」に
        変わった。照合は投入のあとの**読み直し**なので、比較するのは最後の
        SELECT である。
        """
        store = _FakeStore()
        _wire(monkeypatch, store)
        assert self._claims(store) == set()
        store.sql_log.clear()  # 上の確認用 SELECT をジョブの順序と混ぜない
        assert license_map.main([], env=dict(_D1_ENV)) == 0
        log = store.sql_log
        insert_at = next(
            i for i, s in enumerate(log)
            if s.lstrip().upper().startswith("INSERT") and "jss_writer_claims" in s
        )
        select_at = max(i for i, s in enumerate(log) if s == G.WRITER_CLAIMS_SQL)
        assert insert_at < select_at, "照合が投入より前にある = 初回が落ちる順序"

    def test_本番と同じく宣言と一致していれば成功する(self, monkeypatch, caplog) -> None:
        """宣言と一致する行が既にある状態。warning も出さない（件数は宣言に従う）。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        for row in G.writer_claim_rows():
            store.query(
                "INSERT INTO jss_writer_claims (dataset, column_group, writer, updated_at)"
                " VALUES (?, ?, ?, ?)",
                row,
            )
        with caplog.at_level("WARNING"):
            assert license_map.main([], env=dict(_D1_ENV)) == 0
        assert "writer claim" not in caplog.text
        assert "jss_writer_claims" not in caplog.text

    def test_投入が届かず_writer_が食い違ったままなら落ちる(self, monkeypatch, caplog) -> None:
        """投入の直後に読み直しているので、食い違いが残る = upsert が届いていない。

        upsert だけを握りつぶした store で、実表に食い違う writer を置いておく。
        """
        store = _ClaimUpsertLostStore()
        _wire(monkeypatch, store)
        for dataset, group, writer, epoch in G.writer_claim_rows():
            if (dataset, group) == ("core_stocks", "base"):
                writer = G.WRITER_STOCKSTOCK  # 宣言は kabulab-cf
            store.query(
                "INSERT INTO jss_writer_claims (dataset, column_group, writer, updated_at)"
                " VALUES (?, ?, ?, ?)",
                [dataset, group, writer, epoch],
            )
        with caplog.at_level("WARNING"):
            assert license_map.main([], env=dict(_D1_ENV)) == 1
        assert "食い違う" in caplog.text
        assert "core_stocks" in caplog.text

    def test_投入が届かず宣言が実表に無ければ落ちる(self, monkeypatch, caplog) -> None:
        """宣言漏れ。行の無い DB で upsert が届かなければ初回でも成功にしない。"""
        store = _ClaimUpsertLostStore()
        _wire(monkeypatch, store)
        with caplog.at_level("WARNING"):
            assert license_map.main([], env=dict(_D1_ENV)) == 1
        assert "claim が実表に無い" in caplog.text

    def test_宣言に無い_claim_は消さずに失敗として報告する(self, monkeypatch, caplog) -> None:
        """誰かが宣言外の writer を名乗っている。推測で消さず、人に決めさせる。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        store.query(
            "INSERT INTO jss_writer_claims (dataset, column_group, writer, updated_at)"
            " VALUES ('unknown_dataset', 'all', 'stockStock', 0)"
        )
        with caplog.at_level("WARNING"):
            assert license_map.main([], env=dict(_D1_ENV)) == 1
        assert ("unknown_dataset", "all", "stockStock") in self._claims(store)
        assert "unknown_dataset" in caplog.text

    def test_列群を分けた表への_all_行は整理が必要だと言って落ちる(
        self, monkeypatch, caplog
    ) -> None:
        """`core_stocks/all` と `core_stocks/base` が同居すると主張が矛盾する。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        store.query(
            "INSERT INTO jss_writer_claims (dataset, column_group, writer, updated_at)"
            " VALUES ('core_stocks', 'all', 'stockStock', 0)"
        )
        with caplog.at_level("WARNING"):
            assert license_map.main([], env=dict(_D1_ENV)) == 1
        assert "整理が必要" in caplog.text

    def test_1_段目へ戻せば同じ差分で落ちない(self, monkeypatch) -> None:
        """戻し方が 1 行で済むことを固定する（定数を True で残した理由）。"""
        store = _FakeStore()
        _wire(monkeypatch, store)
        monkeypatch.setattr(G, "CLAIM_MISMATCH_IS_FAILURE", False)
        store.query(
            "INSERT INTO jss_writer_claims (dataset, column_group, writer, updated_at)"
            " VALUES ('core_stocks', 'all', 'stockStock', 0)"
        )
        assert license_map.main([], env=dict(_D1_ENV)) == 0

    @pytest.mark.parametrize("as_failure", [False, True])
    def test_段階で_failure_と_warning_が入れ替わる(self, monkeypatch, as_failure) -> None:
        """純粋関数として固定する（I/O を挟まないので両段を直接比べられる）。"""
        monkeypatch.setattr(G, "CLAIM_MISMATCH_IS_FAILURE", as_failure)
        rows = [{"dataset": "core_stocks", "column_group": "base", "writer": "stockStock"}]
        failures, warnings = G.writer_claim_problems(rows)
        assert bool(failures) is as_failure
        assert bool(warnings) is not as_failure
        messages = failures or warnings
        assert any("食い違う" in m for m in messages), messages
