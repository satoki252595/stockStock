"""①銘柄マスタへの列追加（移行 P4a）のテスト。

最重要は **G-core-1**: 発行する SQL にサロゲートキー `id` が SET 句として
一度も現れないこと。`core_stocks.id` は 14 子表が `stock_id` で参照しており、
書き換えた瞬間に全子表の紐付けが壊れる（多くは ON DELETE cascade）。

型でも lint でも「どの列を SET したか」は検出できないので、
**SQL を実行せず組み立てだけして静的に検査する**（設計書 G-core-1 の字義どおり）。
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from jp_stock_pipeline.cloud_store import core_stocks as cs
from jp_stock_pipeline.cloud_store.d1 import D1Error, D1Store
from jp_stock_pipeline.config import CloudStoreSettings

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

_SET_RE = re.compile(r"\bSET\b(.*?)(?:\bWHERE\b|$)", re.IGNORECASE | re.DOTALL)
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_FORBIDDEN_RE = re.compile(
    r"\b(DELETE|DROP|TRUNCATE|REPLACE\s+INTO|INSERT\s+OR\s+REPLACE)\b", re.IGNORECASE
)
_ALLOWED_HEAD_RE = re.compile(
    r"^\s*(ALTER\s+TABLE|CREATE\s+INDEX|SELECT|PRAGMA|UPDATE)\b", re.IGNORECASE
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


class TestBuildColumnUpdate:
    def test_新列だけの_UPDATE_を組み立てる(self) -> None:
        sql, params = cs.build_column_update(
            "7203", {"instrument_type": "equity", "sector33": "輸送用機器"}
        )
        assert sql == (
            "UPDATE core_stocks SET instrument_type = ?, sector33 = ?,"
            " updated_at = (unixepoch()) WHERE code = ?"
        )
        assert params == ["equity", "輸送用機器", "7203"]

    def test_G_core_1_SET_句にサロゲートキーが現れない(self) -> None:
        sql, _ = cs.build_column_update("7203", dict.fromkeys(cs.NEW_COLUMNS, "x"))
        assert "id" not in set_targets(sql)
        assert set_targets(sql) == set(cs.NEW_COLUMNS) | {"updated_at"}

    # 実装定数ではなくリテラルで列挙する。cs.PROTECTED_COLUMNS を parametrize に
    # 使うと、定数から要素を削る変異をテストが追随してしまい検出できない。
    @pytest.mark.parametrize(
        "column",
        [
            "id",
            "code",
            "name",
            "market",
            "sector",
            "is_active",
            "is_yutai",
            "created_at",
        ],
    )
    def test_既存列は書けない(self, column: str) -> None:
        with pytest.raises(D1Error, match="既存列は stockStock から書かない"):
            cs.build_column_update("7203", {column: "x"})

    def test_未知の列も書けない(self) -> None:
        with pytest.raises(D1Error, match="追加対象外の列"):
            cs.build_column_update("7203", {"nonexistent": 1})

    def test_id_を混ぜたら止まる(self) -> None:
        with pytest.raises(D1Error):
            cs.build_column_update("7203", {"id": 999, "instrument_type": "equity"})

    def test_code_なし_値なしは受け付けない(self) -> None:
        with pytest.raises(D1Error):
            cs.build_column_update("", {"instrument_type": "equity"})
        with pytest.raises(D1Error):
            cs.build_column_update("7203", {})

    def test_WHERE_は_code_で_id_を使わない(self) -> None:
        sql, _ = cs.build_column_update("7203", {"quality": "正常"})
        assert sql.endswith("WHERE code = ?")


class TestExpectedColumns:
    """E7: `core_stocks` の列定義の地図。本番 PRAGMA が正で 21 列。

    地図は両リポジトリに散っている（本番 PRAGMA 21 列 / kabulab-cf の
    `core-schema.ts` 9 列 / drizzle `0008_snapshot.json` 9 列 / ここ 21 列）。
    ここが古くなると `--verify` の superset 方向が意味を失うので、
    **実装定数ではなくリテラルで列挙する**（`test_既存列は書けない` と同じ理由。
    定数から要素を削る変異をテストが追随してしまうと検出できない）。
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

        `updated_at` だけは PROTECTED から外す。`build_column_update` が明示的に
        進める唯一の既存列で、PROTECTED に入れると自分の UPDATE が自分で弾かれる。
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


class TestDdlShape:
    def test_発行するのは_ALTER_と_CREATE_INDEX_だけ(self) -> None:
        for sql in cs.plan_ddl(set(), set()):
            assert _ALLOWED_HEAD_RE.match(sql), sql
            assert not _FORBIDDEN_RE.search(sql), sql
            assert not set_targets(sql), f"DDL に SET 句がある: {sql}"

    def test_12列と2索引を計画する(self) -> None:
        pending = cs.plan_ddl(set(), set())
        assert len(pending) == 14
        assert sum(1 for s in pending if s.startswith("ALTER")) == 12
        assert sum(1 for s in pending if s.startswith("CREATE INDEX")) == 2

    def test_適用済みなら何も計画しない(self) -> None:
        assert cs.plan_ddl(set(cs.NEW_COLUMNS), set(cs.NEW_INDEXES)) == []

    def test_部分適用でも残りだけを計画する(self) -> None:
        pending = cs.plan_ddl({"instrument_type", "sector33"}, {"idx_core_stocks_edinet"})
        assert len(pending) == 11
        assert not any("instrument_type" in s for s in pending)
        assert not any("idx_core_stocks_edinet" in s for s in pending)

    def test_追加列は全て_nullable(self) -> None:
        for sql in cs.plan_ddl(set(), set()):
            if sql.startswith("ALTER"):
                assert "NOT NULL" not in sql.upper()
                assert "UNIQUE" not in sql.upper()

    def test_対象外の列の_ALTER_は作れない(self) -> None:
        with pytest.raises(D1Error):
            cs.add_column_sql("id")


class TestAgainstRealSchema:
    """本番と同一 DDL の複製へ実際に流して壊れないことを確かめる。"""

    @pytest.fixture()
    def con(self) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        con.executescript(PROD_DDL)
        con.execute(
            "INSERT INTO core_stocks (code,name,market,sector)"
            " VALUES ('7203','トヨタ自動車','プライム（内国株式）','輸送用機器')"
        )
        con.execute("INSERT INTO yutai_benefits (stock_id) VALUES (1)")
        con.commit()
        return con

    def test_DDL_は既存行と子表を壊さない(self, con: sqlite3.Connection) -> None:
        before = con.execute("SELECT id,code,name,market,sector FROM core_stocks").fetchall()
        seq_before = con.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='core_stocks'"
        ).fetchone()
        for sql in cs.plan_ddl(set(), set()):
            con.execute(sql)
        con.commit()
        assert (
            con.execute("SELECT id,code,name,market,sector FROM core_stocks").fetchall()
            == before
        )
        assert (
            con.execute("SELECT seq FROM sqlite_sequence WHERE name='core_stocks'").fetchone()
            == seq_before
        )
        orphans = con.execute(
            "SELECT COUNT(*) FROM yutai_benefits c"
            " LEFT JOIN core_stocks s ON s.id=c.stock_id WHERE s.id IS NULL"
        ).fetchone()[0]
        assert orphans == 0
        cols = {r[1] for r in con.execute("PRAGMA table_info(core_stocks)")}
        assert set(cs.NEW_COLUMNS) <= cols

    def test_移行元の_upsert_は列追加後も通る(self, con: sqlite3.Connection) -> None:
        """kabulab-cf の universe.ts:181-190 と等価の文。新規上場の INSERT 経路。"""
        for sql in cs.plan_ddl(set(), set()):
            con.execute(sql)
        upsert = (
            "INSERT INTO core_stocks (code,name,market,sector,is_active)"
            " VALUES (?,?,?,?,1)"
            " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
            " market=excluded.market, sector=excluded.sector, is_active=1,"
            " updated_at=(unixepoch())"
        )
        con.execute(upsert, ("9999", "新規上場", "グロース（内国株式）", "情報・通信業"))
        con.execute(upsert, ("7203", "トヨタ自動車", "プライム（内国株式）", "輸送用機器"))
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM core_stocks").fetchone()[0] == 2

    def test_部分_upsert_は既存行でも失敗する(self, con: sqlite3.Connection) -> None:
        """name が NOT NULL・既定値なしのため、新列だけの upsert は成立しない。

        この事実が「P4a の書込は UPDATE 一択」の根拠。
        """
        for sql in cs.plan_ddl(set(), set()):
            con.execute(sql)
        with pytest.raises(sqlite3.IntegrityError, match="core_stocks.name"):
            con.execute(
                "INSERT INTO core_stocks (code,instrument_type) VALUES ('7203','equity')"
                " ON CONFLICT(code) DO UPDATE SET instrument_type=excluded.instrument_type"
            )

    def test_build_column_update_が実際に効く(self, con: sqlite3.Connection) -> None:
        for sql in cs.plan_ddl(set(), set()):
            con.execute(sql)
        sql, params = cs.build_column_update(
            "7203", {"instrument_type": "equity", "sector33": "輸送用機器"}
        )
        con.execute(sql, params)
        con.commit()
        assert con.execute(
            "SELECT instrument_type, sector33 FROM core_stocks WHERE code='7203'"
        ).fetchone() == ("equity", "輸送用機器")
        # 既存列は1バイトも変わっていない
        assert con.execute("SELECT name, market, sector FROM core_stocks").fetchone() == (
            "トヨタ自動車",
            "プライム（内国株式）",
            "輸送用機器",
        )

    def test_索引が実際に使われる(self, con: sqlite3.Connection) -> None:
        for sql in cs.plan_ddl(set(), set()):
            con.execute(sql)
        plan = con.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM core_stocks WHERE edinet_code='E02144'"
        ).fetchall()
        assert any("idx_core_stocks_edinet" in str(r) for r in plan), plan
        plan = con.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM core_stocks"
            " WHERE is_active=1 AND market='プライム（内国株式）'"
        ).fetchall()
        assert any("idx_core_stocks_active_market" in str(r) for r in plan), plan


class TestOrphanCheck:
    def test_14子表と_soft_参照1表を数える(self) -> None:
        assert len(cs.CHILD_TABLES) == 14  # FK 宣言のある子表
        assert cs.SOFT_CHILD_TABLES == ("jss_financials",)
        assert len(cs.ALL_CHECKED_TABLES) == 15
        joined = " ".join(cs.orphan_check_statements())
        for table in cs.ALL_CHECKED_TABLES:
            assert f"FROM {table} c" in joined
        assert not _FORBIDDEN_RE.search(joined)

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
        assert len(statements) == 3  # 15 表 / 5 項


class _RecordingD1Store(D1Store):
    """`query()` を「SQL を積むだけ」に差し替えた D1Store（通信しない）。

    設計書 G-core-1 の「書き込み SQL を実行せずダンプする」を実装で担保する。
    """

    def __init__(self) -> None:
        super().__init__(CloudStoreSettings(), writer="test")
        self.sqls: list[str] = []

    def query(self, sql: str, params: list | None = None) -> list:  # type: ignore[override]
        self.sqls.append(sql)
        return []


class TestD1StoreUpsertIsNotUsable:
    """`D1Store.upsert` を core_stocks に使ってはいけない理由の固定。

    conflict 以外の全列を `c = excluded.c` へ機械展開する設計なので、
    `id` を渡した瞬間にサロゲートキーが SET 句に現れる。ラッパ
    (`core_stocks.build_column_update`) を必ず通すこと。
    """

    def test_汎用_upsert_に_id_を渡すと_SET_句へ出てしまう(self) -> None:
        store = _RecordingD1Store()
        store.upsert(
            "core_stocks",
            ["code", "id", "name"],
            [["7203", 1, "X"]],
            conflict=["code"],
        )
        assert len(store.sqls) == 1
        assert "id" in set_targets(store.sqls[0])

    def test_ラッパ経由なら_id_は絶対に出ない(self) -> None:
        sql, _ = cs.build_column_update("7203", {"instrument_type": "equity"})
        assert "id" not in set_targets(sql)

    def test_P4a_のジョブが発行する文は_ALTER_と_CREATE_INDEX_だけ(self) -> None:
        """ダンプした全文が禁止文を含まず、SET 句も持たないこと。"""
        store = _RecordingD1Store()
        for sql in cs.plan_ddl(set(), set()):
            store.query(sql)
        assert len(store.sqls) == 14
        for sql in store.sqls:
            assert _ALLOWED_HEAD_RE.match(sql), sql
            assert not _FORBIDDEN_RE.search(sql), sql
            assert not set_targets(sql), sql
