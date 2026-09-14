"""`core_stocks` の列定義の地図（E7）と孤児検査（G-core-5）のテスト。

P4a の列追加は適用済みで、DDL 発行コード（`--apply` / `plan_ddl` /
`build_column_update`）は削除した（D-14-1）。このファイルが固定するのは
「本番 PRAGMA が正で 21 列」の地図と、検証用 SQL の形だけである。

`PROD_DDL` と `set_targets` と `APPLIED_DDL` は他のテストからも使う
（`test_core_stocks_job.py` / `test_core_stocks_sector33.py` /
`test_license_map.py` が本番と同一 DDL の複製を作る）。
"""

from __future__ import annotations

import re
import sqlite3

from _doubles import RecordingD1

from jp_stock_pipeline.cloud_store import core_stocks as cs
from jp_stock_pipeline.jobs import core_stocks_migrate as csm

# core_stocks は本番 D1 の実 DDL（2026-09-12 に sqlite_master から取得）。
# 子表は最小再現用で、**FK の ON DELETE は本番と異なる**（本番の yutai_benefits は
# `ON UPDATE no action ON DELETE no action` で、genre_id への 2 本目の FK も持つ）。
# 本番 14 本の内訳は cascade 11 / no action 3
# （otakara_stock_financials / otakara_stock_scores / yutai_benefits）。
# ここで cascade にしているのは「子表があっても ALTER が壊さない」ことの確認用。
PROD_DDL = """
CREATE TABLE `core_stocks` (
    `id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
    `code` text NOT NULL,
    `name` text NOT NULL,
    `market` text NOT NULL,
    `sector` text,
    `is_active` integer DEFAULT true NOT NULL,
    `is_yutai` integer DEFAULT false NOT NULL,
    `created_at` integer DEFAULT (unixepoch()) NOT NULL,
    `updated_at` integer DEFAULT (unixepoch()) NOT NULL
);
CREATE UNIQUE INDEX `core_stocks_code_unique` ON `core_stocks` (`code`);
CREATE TABLE yutai_benefits (
    id integer PRIMARY KEY AUTOINCREMENT,
    stock_id integer NOT NULL,
    FOREIGN KEY (stock_id) REFERENCES core_stocks(id) ON DELETE cascade
);
"""

# P4a の適用内容（適用済み）。テストが本番と同一の複製を作るための素材。
# 本番の形をここで作文するのではなく `NEW_COLUMNS` / `NEW_INDEXES` から作る。
APPLIED_DDL: list[str] = [
    f"ALTER TABLE {cs.TABLE} ADD COLUMN {name} {coltype}"
    for name, coltype in cs.NEW_COLUMNS.items()
] + list(cs.NEW_INDEXES.values())

_SET_RE = re.compile(r"\bSET\b(.*?)(?:\bWHERE\b|$)", re.IGNORECASE | re.DOTALL)
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_FORBIDDEN_RE = re.compile(
    r"\b(DELETE|DROP|TRUNCATE|REPLACE\s+INTO|INSERT\s+OR\s+REPLACE)\b", re.IGNORECASE
)


def set_targets(sql: str) -> set[str]:
    """SET 句の左辺識別子だけを取り出す。"""
    found: set[str] = set()
    for body in _SET_RE.findall(sql):
        for part in body.split(","):
            m = _ASSIGN_RE.match(part)
            if m:
                found.add(m.group(1).lower())
    return found


class TestSetTargetExtractor:
    """検査器そのものが素通ししないことを先に固定する（負のテスト）。"""

    def test_id_を含む_SET_句を検出できる(self) -> None:
        assert "id" in set_targets("UPDATE core_stocks SET id = 1, x = 2 WHERE code='7203'")

    def test_複数行の_SET_句も拾う(self) -> None:
        assert set_targets("UPDATE t\n SET a = ?,\n     id = ?\n WHERE code = ?") == {
            "a",
            "id",
        }

    def test_WHERE_の右側は拾わない(self) -> None:
        assert set_targets("UPDATE t SET a = ? WHERE id = 5") == {"a"}


class TestAppliedDdl:
    def test_12列と2索引の適用内容である(self) -> None:
        assert len(APPLIED_DDL) == 14
        assert sum(1 for s in APPLIED_DDL if s.startswith("ALTER")) == 12
        assert sum(1 for s in APPLIED_DDL if s.startswith("CREATE INDEX")) == 2

    def test_追加列は全て_nullable(self) -> None:
        for sql in APPLIED_DDL:
            if sql.startswith("ALTER"):
                assert "NOT NULL" not in sql.upper()
                assert "UNIQUE" not in sql.upper()

    def test_適用すると本番の21列になる(self) -> None:
        con = sqlite3.connect(":memory:")
        con.executescript(PROD_DDL)
        for sql in APPLIED_DDL:
            con.execute(sql)
        observed = {r[1] for r in con.execute("PRAGMA table_info(core_stocks)")}
        assert observed == cs.EXPECTED_COLUMNS


class TestExpectedColumns:
    """E7: `core_stocks` の列定義の地図。本番 PRAGMA が正で 21 列。

    地図は両リポジトリに散っている（本番 PRAGMA 21 列 / kabulab-cf の
    `core-schema.ts` 9 列 / drizzle `0008_snapshot.json` 9 列 / ここ 21 列）。
    ここが古くなると `--verify` の superset 方向が意味を失うので、
    **実装定数ではなくリテラルで列挙する**（定数から要素を削る変異を
    テストが追随してしまうと検出できない）。
    """

    # 2026-09-12 に本番 D1 の PRAGMA table_info(core_stocks) から取得した 21 列。
    PROD_COLUMNS = {
        # P4a より前からある 9 列
        "id",
        "code",
        "name",
        "market",
        "sector",
        "is_active",
        "is_yutai",
        "created_at",
        "updated_at",
        # P4a で足した 12 列
        "instrument_type",
        "sector33",
        "sector17",
        "edinet_code",
        "listing_status",
        "listing_date",
        "delisting_date",
        "license_tag",
        "src_source",
        "src_data_date",
        "src_fetched_at",
        "quality",
    }

    def test_期待する列集合は本番の_21_列と一致する(self) -> None:
        assert len(self.PROD_COLUMNS) == 21
        assert cs.EXPECTED_COLUMNS == self.PROD_COLUMNS

    def test_BASE_COLUMNS_と_PROTECTED_COLUMNS_が食い違わない(self) -> None:
        """2 本のリテラルを持っている理由の突き合わせ。

        `updated_at` だけは PROTECTED から外す。kabulab-cf の `universe.ts`
        が進める列で、stockStock 側の充填が触らないことを別途固定している。
        """
        assert set(cs.BASE_COLUMNS) - {"updated_at"} == cs.PROTECTED_COLUMNS
        assert "updated_at" not in cs.PROTECTED_COLUMNS

    def test_本番の_DDL_から読んだ列と一致する(self) -> None:
        """テスト内の PROD_DDL（本番の sqlite_master 由来）とも突き合わせる。"""
        con = sqlite3.connect(":memory:")
        con.executescript(PROD_DDL)
        observed = {r[1] for r in con.execute("PRAGMA table_info(core_stocks)")}
        assert observed == set(cs.BASE_COLUMNS)

    def test_定義に無い列を検出する(self) -> None:
        """E7 の穴だった向き。kabulab-cf が勝手に足した列を見つける。"""
        assert cs.unexpected_columns(self.PROD_COLUMNS) == []
        assert cs.unexpected_columns(self.PROD_COLUMNS | {"shares_outstanding"}) == [
            "shares_outstanding"
        ]

    def test_定義に無い索引を検出する(self) -> None:
        assert cs.unexpected_indexes(set(cs.EXPECTED_INDEXES)) == []
        assert cs.unexpected_indexes({"idx_core_stocks_sector"}) == [
            "idx_core_stocks_sector"
        ]

    def test_自動生成索引は誤検知しない(self) -> None:
        """列に UNIQUE が足されると sqlite_autoindex_* が湧く。宣言対象ではない。"""
        assert cs.unexpected_indexes({"sqlite_autoindex_core_stocks_1"}) == []


class TestOrphanCheck:
    def test_13子表と_soft_参照1表と宣言無し2表を数える(self) -> None:
        # L-52 で swing_stock_screening を抜いて 13 表。
        assert len(cs.CHILD_TABLES) == 13  # FK 宣言のある子表
        assert cs.SOFT_CHILD_TABLES == ("jss_financials",)
        assert cs.NOFK_CHILD_TABLES == ("p_momentum", "p_yuho_growth")
        assert len(cs.ALL_CHECKED_TABLES) == 16
        joined = " ".join(cs.orphan_check_statements())
        for table in cs.ALL_CHECKED_TABLES:
            assert f"FROM {table} c" in joined
        assert not _FORBIDDEN_RE.search(joined)

    def test_日次は宣言の無い3表だけを数える(self) -> None:
        assert cs.DAILY_CHECK_TABLES == ("jss_financials", "p_momentum", "p_yuho_growth")
        joined = " ".join(cs.daily_orphan_check_statements())
        assert "FROM jss_financials c" in joined
        assert "FROM p_momentum c" in joined
        assert "FROM p_yuho_growth c" in joined
        for table in cs.CHILD_TABLES:
            assert f"FROM {table} c" not in joined

    def test_soft参照だけ_stock_id_NULL_を除外する(self) -> None:
        joined = " ".join(cs.orphan_check_statements())
        assert "FROM jss_financials c LEFT JOIN core_stocks s ON s.id = c.stock_id" in joined
        assert (
            "FROM jss_financials c LEFT JOIN core_stocks s ON s.id = c.stock_id"
            " WHERE c.stock_id IS NOT NULL AND s.id IS NULL" in joined
        )
        # FK 宣言のある表は stock_id が NOT NULL なので余計な条件を付けない
        assert (
            "FROM yutai_benefits c LEFT JOIN core_stocks s ON s.id = c.stock_id"
            " WHERE s.id IS NULL" in joined
        )

    def test_孤児判定の意味を_sqlite_で確かめる(self) -> None:
        """NULL は「未解決」であって孤児ではない。実在しない id だけを数える。"""
        con = sqlite3.connect(":memory:")
        con.executescript(PROD_DDL)
        con.execute("CREATE TABLE jss_financials (id INTEGER PRIMARY KEY, stock_id INTEGER)")
        con.execute(
            "INSERT INTO core_stocks (code,name,market) VALUES ('7203','ト','プライム')"
        )
        con.executemany(
            "INSERT INTO jss_financials (stock_id) VALUES (?)",
            [(1,), (None,), (None,), (999,)],  # 正常1 / 未解決2 / 本物の孤児1
        )
        con.commit()
        counts: dict[str, int] = {}
        for sql in cs.orphan_check_statements():
            if "jss_financials" not in sql:
                continue
            # 他の子表は存在しないので jss_financials の項だけ取り出して実行する
            term = next(p for p in sql.split(" UNION ALL ") if "jss_financials" in p)
            for name, n in con.execute(term):
                counts[name] = n
        assert counts == {"jss_financials": 1}, counts

    def test_D1_の_compound_SELECT_上限を超えない(self) -> None:
        """D1 は UNION ALL 5 項までで、6 項目から SQLITE_ERROR を返す（本番実測）。"""
        from jp_stock_pipeline.cloud_store.d1 import MAX_COMPOUND_SELECT_TERMS

        assert MAX_COMPOUND_SELECT_TERMS == 5
        statements = cs.orphan_check_statements()
        assert statements, "孤児検査の文が空"
        for sql in statements:
            assert sql.count("UNION ALL") <= MAX_COMPOUND_SELECT_TERMS - 1, sql
        assert len(statements) == 4  # 16 表 / 5 項（K4b で p_yuho_growth を足した）
        assert len(cs.daily_orphan_check_statements()) == 1  # 3 表


class _RecordingD1Store(RecordingD1):
    """発行文の形だけ見る（通信しない）。"""


class TestObserveShape:
    """`_observe` は列・索引・孤児だけを読む。行断面は読まない。"""

    def test_列と索引と孤児だけを観測する(self) -> None:
        store = _RecordingD1Store()
        state = csm._observe(store)  # noqa: SLF001
        assert set(state) == {"columns", "indexes", "orphans"}
        allowed = (
            {cs.TABLE_INFO_SQL, cs.INDEX_LIST_SQL, cs.FOREIGN_KEYS_PRAGMA}
            | set(cs.orphan_check_statements())
            | set(cs.daily_orphan_check_statements())
        )
        assert store.sqls, "1 文も発行していない（観測していない）"
        assert set(store.sqls) <= allowed, set(store.sqls) - allowed
        assert cs.TABLE_INFO_SQL in store.sqls
        assert cs.INDEX_LIST_SQL in store.sqls
        assert cs.FOREIGN_KEYS_PRAGMA in store.sqls


class TestMigrationPrepSymbolsAreGone:
    """移行準備の書込口は「呼び出し 0」ではなく「シンボル不在」で固定する（F4）。

    D-14-1 で `plan_ddl` / `build_column_update` / `normalize_sql` を削除した。
    復活させるなら P4b の前提（鮮度の基準を `updated_at` から外す等）を先に解くこと。
    """

    def test_core_stocks_に書込口のシンボルが無い(self) -> None:
        for name in ("plan_ddl", "add_column_sql", "build_column_update", "normalize_sql"):
            assert not hasattr(cs, name), f"{name} が復活している"

    def test_jobs_から書込口を呼んでいない(self) -> None:
        import ast
        import pathlib

        # **文字列検索ではなく AST の Call ノードで見る。** docstring で
        # 「この関数は呼ばない」と説明している箇所を「呼んでいる」と誤検出する。
        jobs = pathlib.Path(cs.__file__).resolve().parent.parent / "jobs"
        callers = []
        for path in sorted(jobs.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in ("plan_ddl", "build_column_update"):
                    callers.append(path.name)
        assert callers == [], f"{callers} が移行準備の書込口を呼んでいる"
