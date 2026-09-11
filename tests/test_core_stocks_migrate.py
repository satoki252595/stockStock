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

# 本番 D1 の実 DDL（2026-09-12 に sqlite_master から取得したもの）
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

    @pytest.mark.parametrize("column", sorted(cs.PROTECTED_COLUMNS))
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
        assert len(cs.CHILD_TABLES) == 15  # FK 14 + jss_financials の soft 参照
        joined = " ".join(cs.orphan_check_statements())
        for table in cs.CHILD_TABLES:
            assert f"FROM {table} c" in joined
        assert not _FORBIDDEN_RE.search(joined)

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
