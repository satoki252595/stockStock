"""`jobs/core_stocks_migrate.py` のジョブ本体のテスト（移行 P4a）。

本番 D1 と同一 DDL のローカル sqlite を `D1Store` の代わりに差し込み、
`_observe` / `_verify` / `execute` の分岐を実際に通す。

守るべき不変条件:
- 検証・適用のジョブは「対象に触れなかった」を**成功にしない**
- `_verify` は列名の集合だけでなく**定義**と**既存行の内容**まで見る
- `--apply` が発行してよいのは `ALTER` / `CREATE INDEX` だけ
"""

from __future__ import annotations

import argparse
import sqlite3
from types import SimpleNamespace

import pytest

from jp_stock_pipeline.cloud_store import core_stocks as cs
from jp_stock_pipeline.jobs import core_stocks_migrate as job

from test_core_stocks_migrate import PROD_DDL

CHILD_STUBS = "".join(
    f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, stock_id INTEGER NOT NULL);"
    for t in cs.CHILD_TABLES
    if t != "yutai_benefits"
) + "CREATE TABLE jss_financials (id INTEGER PRIMARY KEY, stock_id INTEGER);"


class FakeStore:
    """`D1Store` の最小スタブ。ローカル sqlite に対して本物の SQL を流す。"""

    def __init__(self) -> None:
        self.con = sqlite3.connect(":memory:")
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
        self.sqls: list[str] = []

    def query(self, sql: str, params: list | None = None, *, idempotent: bool = True):
        self.sqls.append(sql)
        cur = self.con.execute(sql, params or [])
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        self.con.commit()
        return rows


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


class TestNoOpIsNotSuccess:
    def test_D1_未設定は失敗として記録する(self) -> None:
        """「触れなかった」を成功にすると、孤児が出ていても緑に見える。"""
        ctx = FakeCtx(None, check_only=True)
        real_store = job._store(ctx)  # noqa: SLF001 - 分岐の確認が目的
        assert real_store is None
        assert ctx.failures, "D1 未設定が失敗として記録されていない"
        assert "D1 が未設定" in ctx.failures[0][1]


class TestCheckOnly:
    def test_1文も書かずに計画を返す(self, store: FakeStore) -> None:
        ctx = FakeCtx(store, check_only=True)
        job.execute(ctx)
        assert ctx.failures == []
        assert ctx.successes == 1
        writes = [s for s in store.sqls if s.split()[0].upper() in ("ALTER", "CREATE", "UPDATE", "INSERT", "DELETE")]
        assert writes == [], writes
        cols = {r[1] for r in store.con.execute("PRAGMA table_info(core_stocks)")}
        assert not (set(cs.NEW_COLUMNS) & cols)


class TestApply:
    def test_DDL_を適用して検証まで通る(self, store: FakeStore) -> None:
        ctx = FakeCtx(store, apply=True)
        job.execute(ctx)
        assert ctx.failures == [], ctx.failures
        assert ctx.successes == 1
        cols = {r[1] for r in store.con.execute("PRAGMA table_info(core_stocks)")}
        assert set(cs.NEW_COLUMNS) <= cols
        idx = {r[0] for r in store.con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='core_stocks'"
        )}
        assert set(cs.NEW_INDEXES) <= idx

    def test_適用は冪等(self, store: FakeStore) -> None:
        job.execute(FakeCtx(store, apply=True))
        ctx2 = FakeCtx(store, apply=True)
        job.execute(ctx2)
        assert ctx2.failures == []
        assert ctx2.successes == 1

    def test_既存行を1バイトも変えない(self, store: FakeStore) -> None:
        before = store.con.execute(cs.SNAPSHOT_SQL).fetchall()
        seq_before = store.con.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='core_stocks'"
        ).fetchone()
        job.execute(FakeCtx(store, apply=True))
        assert store.con.execute(cs.SNAPSHOT_SQL).fetchall() == before
        assert (
            store.con.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='core_stocks'"
            ).fetchone()
            == seq_before
        )

    def test_ALTER_と_CREATE_INDEX_以外は発行しない(self, store: FakeStore) -> None:
        job.execute(FakeCtx(store, apply=True))
        for sql in store.sqls:
            head = sql.split()[0].upper()
            assert head in ("ALTER", "CREATE", "SELECT", "PRAGMA"), sql


class TestVerifyDetectsDamage:
    """名前だけ見ていると素通りする壊れ方を、実際に起こして検出できるか確かめる。"""

    def _applied(self, store: FakeStore) -> dict:
        job.execute(FakeCtx(store, apply=True))
        # 突き合わせ相手 (before) を作るので断面まで読む
        return job._observe(store, baseline=True)  # noqa: SLF001

    def test_既存行の値の書き換えを検出する(self, store: FakeStore) -> None:
        before = self._applied(store)
        store.con.execute("UPDATE core_stocks SET name = 'すり替え' WHERE code='7203'")
        store.con.commit()
        problems = job._verify(job._observe(store, baseline=True), before)  # noqa: SLF001
        assert any("G-core-3" in p for p in problems), problems

    def test_is_active_の反転を検出する(self, store: FakeStore) -> None:
        before = self._applied(store)
        store.con.execute("UPDATE core_stocks SET is_active = 1 - is_active")
        store.con.commit()
        problems = job._verify(job._observe(store, baseline=True), before)  # noqa: SLF001
        assert problems, "件数が同じでも内容が変わったことを検出できていない"

    def test_同名で定義の違う索引を検出する(self, store: FakeStore) -> None:
        """`CREATE INDEX IF NOT EXISTS` は同名があると no-op になる。

        名前だけ突き合わせていると「追加索引が入っている」と誤判定する。
        """
        store.con.execute(
            "CREATE INDEX idx_core_stocks_active_market ON core_stocks (sector)"
        )
        store.con.commit()
        ctx = FakeCtx(store, apply=True)
        job.execute(ctx)
        assert ctx.failures, "同名だが定義の違う索引を見逃している"
        assert any("定義の違う索引" in r for _, r in ctx.failures), ctx.failures

    def test_孤児を検出する(self, store: FakeStore) -> None:
        before = self._applied(store)
        store.con.execute("INSERT INTO yutai_benefits (stock_id) VALUES (999)")
        store.con.commit()
        problems = job._verify(job._observe(store, baseline=True), before)  # noqa: SLF001
        assert any("G-core-5" in p for p in problems), problems

    def test_soft参照の未解決行は孤児にしない(self, store: FakeStore) -> None:
        before = self._applied(store)
        store.con.execute("INSERT INTO jss_financials (stock_id) VALUES (NULL)")
        store.con.commit()
        problems = job._verify(job._observe(store, baseline=True), before)  # noqa: SLF001
        assert not any("G-core-5" in p for p in problems), problems


class TestColumnDriftDetection:
    """E7: 列定義のドリフトを `--verify` で捕まえる。

    既存実装は `NEW_COLUMNS ⊆ 本番`（subset 方向）しか見ていなかったので、
    **本番にあって stockStock の定義に無い列**を素通りさせていた。`core_stocks`
    の列定義は両リポジトリに散っていて本番 PRAGMA が正なので、kabulab-cf 側が
    列を足した瞬間に stockStock の地図が古くなる。気づけるのはこの向きだけ。
    """

    def _applied(self, store: FakeStore) -> dict:
        job.execute(FakeCtx(store, apply=True))
        # 突き合わせ相手 (before) を作るので断面まで読む
        return job._observe(store, baseline=True)  # noqa: SLF001

    def test_適用直後は何も検出しない(self, store: FakeStore) -> None:
        before = self._applied(store)
        assert job._verify(before, None) == []  # noqa: SLF001

    def test_定義に無い列が足されたら検出する(self, store: FakeStore) -> None:
        """対向リポジトリが勝手に列を足す = 本番が正で定義が古い状態。"""
        self._applied(store)
        store.con.execute("ALTER TABLE core_stocks ADD COLUMN shares_outstanding INTEGER")
        store.con.commit()
        problems = job._verify(job._observe(store, baseline=False), None)  # noqa: SLF001
        assert any("E7" in p for p in problems), problems
        assert any("shares_outstanding" in p for p in problems), problems

    def test_定義に無い索引が足されたら検出する(self, store: FakeStore) -> None:
        self._applied(store)
        store.con.execute("CREATE INDEX idx_core_stocks_sector ON core_stocks (sector)")
        store.con.commit()
        problems = job._verify(job._observe(store, baseline=False), None)  # noqa: SLF001
        assert any("定義に無い索引" in p for p in problems), problems

    def test_追加列の欠落は従来どおり検出する(self, store: FakeStore) -> None:
        """superset 方向を足しても subset 方向を壊していないこと。"""
        state = job._observe(store, baseline=False)  # noqa: SLF001 - P4a 未適用の状態
        problems = job._verify(state, None)  # noqa: SLF001
        assert any("追加列が入っていない" in p for p in problems), problems

    def test_verify_は読み取りだけで_CI_から回せる(self, store: FakeStore) -> None:
        """ops_check の cron から毎日呼ぶ前提。1 文でも書いたら本番で事故になる。"""
        self._applied(store)
        store.sqls.clear()
        ctx = FakeCtx(store, verify=True)
        job.execute(ctx)
        assert ctx.failures == [], ctx.failures
        assert ctx.successes == 1
        assert store.sqls, "1 文も発行していない（観測していない）"
        for sql in store.sqls:
            assert sql.split()[0].upper() in ("SELECT", "PRAGMA"), sql
        # D1 は走査行課金。`--compare-to` を渡さない CI の形では `core_stocks` の
        # 行を 1 行も走査しない（G-core-2/3 は比較相手が無ければ評価されないので、
        # 断面を読んでも結果を捨てるだけで料金だけが増える）。
        assert cs.SNAPSHOT_SQL not in store.sqls
        assert cs.COUNTS_SQL not in store.sqls
        # 孤児検査 (G-core-5) は比較相手なしでも判定に使うので消えていないこと。
        assert any("LEFT JOIN core_stocks" in sql for sql in store.sqls)

    def test_ドリフトがあれば_verify_が失敗して_CI_が赤くなる(self, store: FakeStore) -> None:
        self._applied(store)
        store.con.execute("ALTER TABLE core_stocks ADD COLUMN shares_outstanding INTEGER")
        store.con.commit()
        ctx = FakeCtx(store, verify=True)
        job.execute(ctx)
        assert ctx.failures, "ドリフトを検出しても失敗として記録していない"
        assert ctx.successes == 0
        assert any("E7" in reason for _, reason in ctx.failures), ctx.failures


class TestAlreadyApplied:
    @pytest.mark.parametrize(
        "message",
        [
            "D1 エラー: duplicate column name: quality",
            "D1 エラー: index idx_core_stocks_edinet already exists",
        ],
    )
    def test_再送のすり抜けを識別する(self, message: str) -> None:
        assert job._already_applied(Exception(message))  # noqa: SLF001

    def test_無関係なエラーは識別しない(self) -> None:
        assert not job._already_applied(Exception("D1 エラー: no such table"))  # noqa: SLF001
