"""`jobs/core_stocks_migrate.py` のジョブ本体のテスト（移行 P4a 適用済み）。

本番 D1 と同一 DDL のローカル sqlite を `D1Store` の代わりに差し込み、
`_observe` / `_verify` / `execute` の分岐を実際に通す。

守るべき不変条件:
- 検証のジョブは「対象に触れなかった」を**成功にしない**
- `--verify` は SELECT / PRAGMA しか発行しない（CI から毎日回す）
- E7: 本番にあって定義に無い列・索引を検出する
- G-core-5: 子表の孤児を検出する（soft 参照の NULL は孤児にしない）
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from _doubles import SqliteD1

from jp_stock_pipeline.cloud_store import core_stocks as cs
from jp_stock_pipeline.jobs import core_stocks_migrate as job

from test_core_stocks_migrate import APPLIED_DDL, PROD_DDL

CHILD_STUBS = "".join(
    f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, stock_id INTEGER NOT NULL);"
    for t in cs.CHILD_TABLES
    if t != "yutai_benefits"
) + (
    "CREATE TABLE jss_financials (id INTEGER PRIMARY KEY, stock_id INTEGER);"
    # 本番と同じく stock_id が PRIMARY KEY（NOT NULL。FK 宣言なし）。
    "CREATE TABLE p_momentum (stock_id INTEGER PRIMARY KEY NOT NULL);"
    "CREATE TABLE p_yuho_growth (stock_id INTEGER PRIMARY KEY NOT NULL);"
)


class FakeStore(SqliteD1):
    """sqlite 裏打ちの `D1Store` + 本番形 `core_stocks` と子表スタブ。"""

    def __init__(self) -> None:
        super().__init__()
        self.con.executescript(PROD_DDL)
        self.con.executescript(CHILD_STUBS)
        self.con.execute(
            "INSERT INTO core_stocks (code,name,market,sector)"
            " VALUES ('7203','トヨタ','プライム（内国株式）','輸送用機器')"
        )
        self.con.execute(
            "INSERT INTO core_stocks (code,name,market,sector,is_active)"
            " VALUES ('9999','旧上場','グロース（内国株式）',NULL,0)"
        )
        self.con.commit()


class FakeCtx:
    def __init__(self, store: FakeStore | None, **args) -> None:
        self._store = store
        self.args = argparse.Namespace(**args)
        self.failures: list[tuple[str, str]] = []
        self.successes = 0
        self.cloud = (
            SimpleNamespace(settings=SimpleNamespace(d1_enabled=lambda: store is not None))
            if store is not None
            else None
        )

    def add_failure(self, code: str, reason: str = "") -> None:
        self.failures.append((code, reason))

    def add_success(self, n: int = 1) -> None:
        self.successes += n


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    s = FakeStore()
    monkeypatch.setattr(job, "_store", lambda ctx: s if ctx.cloud is not None else None)
    return s


def _apply_ddl(store: FakeStore) -> None:
    """P4a の適用内容（適用済みの本番と同じ形）を流す。"""
    for stmt in APPLIED_DDL:
        store.con.execute(stmt)
    store.con.commit()


class TestNoOpIsNotSuccess:
    def test_D1_未設定は失敗として記録する(self) -> None:
        """「触れなかった」を成功にすると、孤児が出ていても緑に見える。"""
        ctx = FakeCtx(None, verify=True)
        real_store = job._store(ctx)  # noqa: SLF001 - 分岐の確認が目的
        assert real_store is None
        assert ctx.failures, "D1 未設定が失敗として記録されていない"
        assert "D1 が未設定" in ctx.failures[0][1]


class TestVerifyDetectsDamage:
    def test_孤児を検出する(self, store: FakeStore) -> None:
        _apply_ddl(store)
        store.con.execute("INSERT INTO yutai_benefits (stock_id) VALUES (999)")
        store.con.commit()
        problems = job._verify(job._observe(store))  # noqa: SLF001
        assert any("G-core-5" in p for p in problems), problems

    def test_soft参照の未解決行は孤児にしない(self, store: FakeStore) -> None:
        _apply_ddl(store)
        store.con.execute("INSERT INTO jss_financials (stock_id) VALUES (NULL)")
        store.con.commit()
        problems = job._verify(job._observe(store))  # noqa: SLF001
        assert not any("G-core-5" in p for p in problems), problems


class TestDailyOrphanScope:
    """L-16: 日次は宣言の無い 3 表だけ。FK 強制の無い環境では全表に戻す。"""

    def test_fk_on_では宣言の無い3表だけを数える(self, store: FakeStore) -> None:
        _apply_ddl(store)
        store.con.execute("PRAGMA foreign_keys = ON")
        store.sql_log.clear()
        job._observe(store)  # noqa: SLF001
        orphan_sqls = [s for s in store.sql_log if "LEFT JOIN core_stocks" in s]
        assert orphan_sqls == cs.daily_orphan_check_statements()
        assert cs.FOREIGN_KEYS_PRAGMA in store.sql_log

    def test_fk_off_では全表検査に戻す(self, store: FakeStore) -> None:
        """素の SQLite の既定は 0。安全弁が無いと FK 表の孤児を見逃す。"""
        _apply_ddl(store)
        assert store.con.execute("PRAGMA foreign_keys").fetchone() == (0,)
        store.sql_log.clear()
        job._observe(store)  # noqa: SLF001
        orphan_sqls = [s for s in store.sql_log if "LEFT JOIN core_stocks" in s]
        assert orphan_sqls == cs.orphan_check_statements()

    def test_p_momentum_の孤児を日次で検出する(self, store: FakeStore) -> None:
        _apply_ddl(store)
        store.con.execute("PRAGMA foreign_keys = ON")
        store.con.execute("INSERT INTO p_momentum (stock_id) VALUES (999)")
        store.con.commit()
        problems = job._verify(job._observe(store))  # noqa: SLF001
        assert any("G-core-5" in p and "p_momentum" in p for p in problems), problems


class TestColumnDriftDetection:
    """E7: 列定義のドリフトを `--verify` で捕まえる。

    既存実装は `NEW_COLUMNS ⊆ 本番`（subset 方向）しか見ていなかったので、
    **本番にあって stockStock の定義に無い列**を素通りさせていた。`core_stocks`
    の列定義は両リポジトリに散っていて本番 PRAGMA が正なので、kabulab-cf 側が
    列を足した瞬間に stockStock の地図が古くなる。気づけるのはこの向きだけ。
    """

    def test_適用直後は何も検出しない(self, store: FakeStore) -> None:
        _apply_ddl(store)
        assert job._verify(job._observe(store)) == []  # noqa: SLF001

    def test_定義に無い列が足されたら検出する(self, store: FakeStore) -> None:
        """対向リポジトリが勝手に列を足す = 本番が正で定義が古い状態。"""
        _apply_ddl(store)
        store.con.execute("ALTER TABLE core_stocks ADD COLUMN shares_outstanding INTEGER")
        store.con.commit()
        problems = job._verify(job._observe(store))  # noqa: SLF001
        assert any("E7" in p for p in problems), problems
        assert any("shares_outstanding" in p for p in problems), problems

    def test_定義に無い索引が足されたら検出する(self, store: FakeStore) -> None:
        _apply_ddl(store)
        store.con.execute("CREATE INDEX idx_core_stocks_sector ON core_stocks (sector)")
        store.con.commit()
        problems = job._verify(job._observe(store))  # noqa: SLF001
        assert any("定義に無い索引" in p for p in problems), problems

    def test_追加列の欠落は従来どおり検出する(self, store: FakeStore) -> None:
        """superset 方向を足しても subset 方向を壊していないこと。"""
        state = job._observe(store)  # noqa: SLF001 - P4a 未適用の状態
        problems = job._verify(state)  # noqa: SLF001
        assert any("追加列が入っていない" in p for p in problems), problems

    def test_verify_は読み取りだけで_CI_から回せる(self, store: FakeStore) -> None:
        """ops_check の cron から毎日呼ぶ前提。1 文でも書いたら本番で事故になる。"""
        _apply_ddl(store)
        store.sql_log.clear()
        ctx = FakeCtx(store, verify=True)
        job.execute(ctx)
        assert ctx.failures == [], ctx.failures
        assert ctx.successes == 1
        assert store.sql_log, "1 文も発行していない（観測していない）"
        for sql in store.sql_log:
            assert sql.split()[0].upper() in ("SELECT", "PRAGMA"), sql
        # D1 は走査行課金。`core_stocks` の行断面は読まない（列・索引・孤児だけ）。
        allowed = (
            {cs.TABLE_INFO_SQL, cs.INDEX_LIST_SQL, cs.FOREIGN_KEYS_PRAGMA}
            | set(cs.orphan_check_statements())
            | set(cs.daily_orphan_check_statements())
        )
        assert set(store.sql_log) <= allowed, set(store.sql_log) - allowed
        # 孤児検査 (G-core-5) は判定に使うので消えていないこと。
        assert any("LEFT JOIN core_stocks" in sql for sql in store.sql_log)

    def test_ドリフトがあれば_verify_が失敗して_CI_が赤くなる(self, store: FakeStore) -> None:
        _apply_ddl(store)
        store.con.execute("ALTER TABLE core_stocks ADD COLUMN shares_outstanding INTEGER")
        store.con.commit()
        ctx = FakeCtx(store, verify=True)
        job.execute(ctx)
        assert ctx.failures, "ドリフトを検出しても失敗として記録していない"
        assert ctx.successes == 0
        assert any("E7" in reason for _, reason in ctx.failures), ctx.failures
