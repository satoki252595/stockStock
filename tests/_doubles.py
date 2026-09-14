"""テスト用ダブルの置き場（L-34）。

D1 ダブル 8 個・R2 ダブル 3 個が各テストに散らばっていたのを 3 クラスへ集約する。
差は DDL・seed・失敗条件だけだったので、引数で渡す。

- `SqliteD1`: 本物の DDL を流した sqlite に `D1Store` の query/upsert を当てる。
  `upsert` は継承した本物を使う（`query` 経由で sqlite へ落ちる）。`query` を
  直接書いたスタブにすると、本番の upsert 経路（チャンク分割・keep・bind 上限）
  をテストが踏まなくなる（`test_license_map` の方針を踏襲）。
- `RecordingD1`: SQL を積むだけで実行しない（発行文の形だけ見るテスト用）。
- `FakeR2`: 原本系（exists/put_bytes）と需給系（get_json/put_json_guarded）の
  両方の口を持つ。ガードは本物を通す。

`sql_log` に流れた SQL を全部残す。`write_sql` は書き込み文だけを抜く。
"""

from __future__ import annotations

import sqlite3

from jp_stock_pipeline.cloud_store.d1 import MAX_BOUND_PARAMS, D1Error, D1Store
from jp_stock_pipeline.config import CloudStoreSettings


class SqliteD1(D1Store):
    """sqlite 裏打ちの `D1Store`。DDL と初期行と失敗条件を引数で渡す。"""

    def __init__(
        self,
        *,
        ddl: tuple[str, ...] = (),
        seed: tuple[tuple[str, tuple], ...] = (),
        fail_on_prefix: tuple[str, ...] = (),
        check_bind_limit: bool = True,
        writer: str = "test",
    ) -> None:
        super().__init__(CloudStoreSettings(), writer=writer)
        self.con = sqlite3.connect(":memory:")
        for statement in ddl:
            self.con.execute(statement)
        for sql, params in seed:
            self.con.execute(sql, params)
        self.con.commit()
        self.fail_on_prefix = tuple(fail_on_prefix)
        self.check_bind_limit = check_bind_limit
        self.sql_log: list[str] = []

    def query(self, sql: str, params: list | None = None, *, idempotent: bool = True):
        self.sql_log.append(sql)
        if self.check_bind_limit and params and len(params) > MAX_BOUND_PARAMS:
            raise D1Error(f"バインドパラメータ上限超過: {len(params)}")
        upper = sql.lstrip().upper()
        for prefix in self.fail_on_prefix:
            if upper.startswith(prefix):
                raise D1Error(f"テスト: {prefix} を失敗させる: {sql[:80]!r}")
        try:
            cur = self.con.execute(sql, params or [])
        except sqlite3.Error as exc:
            # D1Store は失敗を必ず D1Error に包む（呼び出し側の分岐を本番と同じに）。
            raise D1Error(f"sqlite: {exc} sql={sql[:120]!r}") from exc
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        self.con.commit()
        return rows

    @property
    def write_sql(self) -> list[str]:
        """書き込み（INSERT/UPDATE/DELETE）に見える文だけを抜く。"""
        return [
            s for s in self.sql_log
            if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        ]


class RecordingD1(D1Store):
    """SQL を積むだけで実行しない `D1Store`（発行文の形だけ見る）。"""

    def __init__(self, *, writer: str = "test", fail_on_upsert: bool = False) -> None:
        super().__init__(CloudStoreSettings(), writer=writer)
        self.sqls: list[str] = []
        self.upserts: list[tuple] = []
        self.fail_on_upsert = fail_on_upsert

    def query(self, sql: str, params: list | None = None, *, idempotent: bool = True):  # type: ignore[override]
        self.sqls.append(sql)
        return []

    def upsert(self, table, columns, rows, *, conflict, keep=None):  # type: ignore[override]
        if self.fail_on_upsert:
            raise D1Error("テスト: upsert 失敗")
        self.upserts.append((table, columns, rows, conflict, keep))
        return len(rows)


class FakeR2:
    """原本系と需給系の両方の口を持つ R2 ダブル。"""

    def __init__(
        self,
        objects: dict | set[str] | None = None,
        *,
        writer: str = "test",
        fail_on_put: bool = False,
    ) -> None:
        if isinstance(objects, (set, frozenset)):
            # 存在だけ決める用途（本文は見ない）。set で渡せるようにする。
            self.objects: dict = {key: b"" for key in objects}
        else:
            self.objects = dict(objects or {})
        self.writer = writer
        self.fail_on_put = fail_on_put
        self.puts: list[str] = []
        self.heads: list[str] = []
        self.bodies: dict[str, bytes] = {}

    # --- 原本系（CloudSink が使う） ---

    def exists(self, key: str) -> bool:
        self.heads.append(key)
        return key in self.objects

    def put_bytes(self, key: str, body: bytes, *, content_type: str) -> None:
        if self.fail_on_put:
            from jp_stock_pipeline.cloud_store.r2 import R2Error

            raise R2Error("boom")
        self.objects[key] = body
        self.puts.append(key)
        self.bodies[key] = body

    # --- 需給系（supply が使う。ガードは本物、writer 付与は呼び出し側） ---

    def get_json(self, key: str):
        if key in self.objects:
            return self.objects[key], True
        return None, False

    def put_json_guarded(self, key: str, payload, *, contract=None) -> None:
        from jp_stock_pipeline.cloud_store.guards import check_no_regression

        check_no_regression(
            self.objects.get(key), payload, writer=self.writer, contract=contract
        )
        self.objects[key] = payload
        self.puts.append(key)
