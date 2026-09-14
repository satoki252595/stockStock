"""core_stocks_migrate: `core_stocks` の列定義ドリフトと孤児の日次検証（移行 P4a 適用済み）。

`core_stocks` は移行元 kabulab-cf が所有する既存表で、14 子表が `stock_id` で
参照している。P4a の列追加（12 列 + 2 索引）は適用済みで、DDL 発行コード
（`--apply` / `--sql-dump` / `--snapshot` / `--state-dump` / `--compare-to`）は
削除した（D-14-1）。このジョブは `--verify` 専用で、**SELECT / PRAGMA しか
発行しない**ので CI から毎日回せる。`.github/workflows/ops_check.yml` の
第3ステップが呼ぶ。

## 判定内容

- G-core-5: 子表の孤児（`子表 LEFT JOIN core_stocks`）。本番では宣言の無い
  2 表（`jss_financials` / `p_momentum`）だけを数える（L-16。FK 宣言のある
  14 表は DB が守る）。「行を 1 行も走査しない」ではない
- 追加列・追加索引の有無（適用済みの確認）と型・nullability
- E7: 本番にあって stockStock の定義に無い列・索引（superset 方向）。
  `core_stocks` の列定義は両リポジトリに散っていて**本番の PRAGMA が正**
  （21 列）なので、kabulab-cf 側が列を足した瞬間に stockStock の地図が古くなる。
  気づけるのはこの向きの検査だけ

なお「1 文も書かない」ではない: 他の全ジョブと同じく `jobs.runner.run_job` が
終了時に D1 `jss_job_runs` へ 1 行 INSERT する（移行対象表には触らないが、
読み取り専用トークンでは動かない）。
"""

from __future__ import annotations

import logging

from ..cloud_store import core_stocks as cs
from ..cloud_store.d1 import D1Error, D1Store
from .runner import JobContext, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "core_stocks_migrate"


def _store(ctx: JobContext) -> D1Store | None:
    """D1 が使えないなら失敗として記録する。

    検証のジョブは「対象に触れなかった」を成功にしてはいけない。
    環境変数名の間違いや `--dry-run` の付けっぱなしで、孤児が出ていても
    列が消えていても「成功 (processed=0)」に見えてしまう。
    """
    cloud = ctx.cloud
    if cloud is None or not cloud.settings.d1_enabled():
        ctx.add_failure(
            "d1",
            "D1 が未設定（CF_ACCOUNT_ID / CF_API_TOKEN / CF_D1_DATABASE_ID）。"
            " --dry-run では ctx.cloud が張られないので使えない",
        )
        return None
    return D1Store(cloud.settings, writer=JOB_NAME)


def _observe(store: D1Store) -> dict:
    """現状を読む（列・索引・孤児のみ。`core_stocks` の行断面は読まない）。

    名前だけでなく**定義**まで持つ。列名の集合しか見ていないと
    「型の違う同名列」を見逃す。行の値の突き合わせ（旧 G-core-2/3）は
    適用前後の比較が要る移行時限定の判定だったので、適用済みの今は持たない。

    孤児検査は `PRAGMA foreign_keys=1` の本番では宣言の無い 2 表だけを数える
    （L-16。FK 宣言のある 14 表は DB が INSERT 時に弾く）。`0` の環境では
    全表検査に戻す安全弁（素の SQLite の既定は 0 のため）。
    """
    columns = {
        str(r["name"]): {
            "type": str(r["type"] or ""),
            "notnull": int(r["notnull"] or 0),
            "dflt": r["dflt_value"],
        }
        for r in store.query(cs.TABLE_INFO_SQL)
    }
    indexes = {str(r["name"]): r.get("sql") for r in store.query(cs.INDEX_LIST_SQL)}
    fk_rows = store.query(cs.FOREIGN_KEYS_PRAGMA)
    fk_on = bool(fk_rows and int(fk_rows[0].get("foreign_keys", 0) or 0))
    statements = (
        cs.daily_orphan_check_statements() if fk_on else cs.orphan_check_statements()
    )
    orphans: dict[str, int] = {}
    for sql in statements:
        orphans.update({str(r["t"]): int(r["n"] or 0) for r in store.query(sql)})
    return {"columns": columns, "indexes": indexes, "orphans": orphans}


def _report(state: dict) -> None:
    logger.info(
        "列 %d 個 / 索引 %s", len(state["columns"]), sorted(state["indexes"])
    )
    bad = {t: n for t, n in state["orphans"].items() if n}
    logger.info("孤児: %s", bad or "全子表で 0 件")


def _verify(state: dict) -> list[str]:
    """G-core-5 / 追加列の有無 / E7 を突き合わせる。差分の説明を返す。

    列と索引は **両方向**で見る。`NEW_COLUMNS ⊆ 本番`（subset 方向）だけを
    見ていた頃は「本番にあって stockStock の定義に無い列」を素通りさせていた。
    `core_stocks` の列定義は両リポジトリに散っていて本番の PRAGMA が正なので、
    kabulab-cf 側が列を足した瞬間に stockStock の地図が古くなる。それを
    気づけるのはこの向きの検査だけ（E7）。
    """
    problems: list[str] = []
    bad = {t: n for t, n in state["orphans"].items() if n}
    if bad:
        problems.append(f"G-core-5: 孤児が残っている {bad}")
    missing = [c for c in cs.NEW_COLUMNS if c not in state["columns"]]
    if missing:
        problems.append(f"追加列が入っていない: {missing}")
    # E7 superset 方向: 本番にあって定義に無い列 / 索引。
    extra_columns = cs.unexpected_columns(set(state["columns"]))
    if extra_columns:
        problems.append(
            f"E7: 本番 core_stocks に stockStock の定義に無い列がある {extra_columns}"
            "（列定義のドリフト。本番 PRAGMA が正なので cloud_store/core_stocks.py の"
            " BASE_COLUMNS / NEW_COLUMNS を追随させ、kabulab-cf 側の"
            " core-schema.ts / drizzle snapshot も同時に直す）"
        )
    extra_indexes = cs.unexpected_indexes(set(state["indexes"]))
    if extra_indexes:
        problems.append(
            f"E7: 本番 core_stocks に stockStock の定義に無い索引がある {extra_indexes}"
            "（cloud_store/core_stocks.py の BASE_INDEXES / NEW_INDEXES を追随させる）"
        )
    # 型と nullability まで見る。ALTER は NOT NULL / UNIQUE を付けられないので、
    # notnull=1 の同名列があるなら別物が既に居る。
    for name, want_type in cs.NEW_COLUMNS.items():
        got = state["columns"].get(name)
        if not isinstance(got, dict):
            continue
        if got.get("type", "").upper() != want_type.upper():
            problems.append(f"{name} の型が違う: {got.get('type')!r} != {want_type!r}")
        if got.get("notnull"):
            problems.append(f"{name} が NOT NULL になっている（ALTER では付かないはず）")
    for name in cs.NEW_INDEXES:
        if name not in state["indexes"]:
            problems.append(f"追加索引が入っていない: {name}")
    return problems


def execute(ctx: JobContext) -> None:
    store = _store(ctx)
    if store is None:
        return
    try:
        state = _observe(store)
    except D1Error as exc:
        ctx.add_failure("observe", f"現状を読めない: {exc}")
        return
    problems = _verify(state)
    _report(state)
    if problems:
        for p in problems:
            ctx.add_failure("verify", p)
        return
    logger.info("検証 OK: G-core-5 と列・索引の有無・E7 をすべて満たす")
    ctx.add_success()


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("core_stocks の列定義ドリフトと孤児の日次検証 (E7 / G-core-5)")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="検証を実行する（唯一のモード。ops_check.yml が渡す）",
    )
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
