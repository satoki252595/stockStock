"""core_stocks_migrate: ①銘柄マスタへの列追加（移行 P4a）。

`core_stocks` は移行元 kabulab-cf が所有する既存表で、14 子表が `stock_id` で
参照している。P4a で発行するのは **`ALTER TABLE ... ADD COLUMN` と
`CREATE INDEX` だけ**で、既存行・既存列・子表には1バイトも触れない。

値の充填（`instrument_type` 等）は P4a の範囲外。供給源の JPX data_j.xls が
2026-09-12 時点で HTTP 404 を返しており、充填に使える一次データが無い。

## モード

- `--check-only`（既定）: 何も書かず、現状と発行予定の差分だけを報告する
- `--sql-dump PATH`: 発行予定の全文をファイルへ出す（G-core-1 の静的検査用）
- `--apply`: DDL を実際に発行する。**唯一の書込モード**
- `--verify`: 適用後の検証（G-core-2 / G-core-3 / G-core-5）だけを実行する

`--snapshot PATH` を付けると、既存 3,818 行のスナップショットを JSON で保存する
（ロールバックの原本）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..cloud_store import core_stocks as cs
from ..cloud_store.d1 import D1Error, D1Store
from .runner import JobContext, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "core_stocks_migrate"

# 移行元 D1（kabulab-cf）を読む。正本 DB と同一なので既定はそちら。
def _store(ctx: JobContext) -> D1Store | None:
    cloud = ctx.cloud
    if cloud is None or not cloud.settings.d1_enabled():
        logger.warning("D1 未設定のため何もしない（no-op）")
        return None
    database_id = (
        cloud.settings.kabulab_d1_database_id or cloud.settings.d1_database_id
    )
    return D1Store(cloud.settings, writer=JOB_NAME, database_id=database_id)


def _observe(store: D1Store) -> dict:
    """現状を読む（SELECT / PRAGMA のみ）。"""
    columns = {str(r["name"]) for r in store.query(cs.TABLE_INFO_SQL)}
    indexes = {str(r["name"]) for r in store.query(cs.INDEX_LIST_SQL)}
    counts = (store.query(cs.COUNTS_SQL) or [{}])[0]
    seq_rows = store.query(cs.SEQ_SQL)
    orphans: dict[str, int] = {}
    for sql in cs.orphan_check_statements():
        orphans.update({str(r["t"]): int(r["n"] or 0) for r in store.query(sql)})
    return {
        "columns": sorted(columns),
        "indexes": sorted(indexes),
        "counts": {k: (int(v) if v is not None else None) for k, v in counts.items()},
        "sqlite_sequence": int(seq_rows[0]["seq"]) if seq_rows else None,
        "orphans": orphans,
    }


def _report(state: dict, pending: list[str]) -> None:
    logger.info("列 %d 個 / 索引 %s", len(state["columns"]), state["indexes"])
    logger.info("件数: %s / sqlite_sequence=%s", state["counts"], state["sqlite_sequence"])
    bad = {t: n for t, n in state["orphans"].items() if n}
    logger.info("孤児: %s", bad or "全子表で 0 件")
    logger.info("未適用の DDL: %d 文", len(pending))
    for sql in pending:
        logger.info("  %s", sql)


def _verify(state: dict, before: dict | None) -> list[str]:
    """G-core-2 / G-core-3 / G-core-5 を突き合わせる。差分の説明を返す。"""
    problems: list[str] = []
    bad = {t: n for t, n in state["orphans"].items() if n}
    if bad:
        problems.append(f"G-core-5: 孤児が残っている {bad}")
    missing = [c for c in cs.NEW_COLUMNS if c not in state["columns"]]
    if missing:
        problems.append(f"追加列が入っていない: {missing}")
    missing_idx = [i for i in cs.NEW_INDEXES if i not in state["indexes"]]
    if missing_idx:
        problems.append(f"追加索引が入っていない: {missing_idx}")
    if before is None:
        return problems
    if before["counts"] != state["counts"]:
        problems.append(f"G-core-2: 件数が変わった {before['counts']} -> {state['counts']}")
    if before["sqlite_sequence"] != state["sqlite_sequence"]:
        problems.append(
            f"G-core-2: sqlite_sequence が動いた "
            f"{before['sqlite_sequence']} -> {state['sqlite_sequence']}"
        )
    lost = set(before["columns"]) - set(state["columns"])
    if lost:
        problems.append(f"既存列が消えた: {sorted(lost)}")
    lost_idx = set(before["indexes"]) - set(state["indexes"])
    if lost_idx:
        problems.append(f"既存索引が消えた: {sorted(lost_idx)}")
    return problems


def _snapshot(store: D1Store, path: Path) -> int:
    rows = store.query(cs.SNAPSHOT_SQL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    logger.info("スナップショット %d 行 -> %s", len(rows), path)
    return len(rows)


def execute(ctx: JobContext) -> None:
    store = _store(ctx)
    if store is None:
        return
    args = ctx.args

    try:
        state = _observe(store)
    except D1Error as exc:
        ctx.add_failure("observe", f"現状を読めない: {exc}")
        return

    pending = cs.plan_ddl(set(state["columns"]), set(state["indexes"]))

    snapshot_path = getattr(args, "snapshot", None)
    if snapshot_path:
        try:
            _snapshot(store, Path(snapshot_path))
        except (D1Error, OSError) as exc:
            ctx.add_failure("snapshot", f"スナップショットを保存できない: {exc}")
            return

    state_path = getattr(args, "state_dump", None)
    if state_path:
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        logger.info("現状の state -> %s", state_path)

    dump_path = getattr(args, "sql_dump", None)
    if dump_path:
        Path(dump_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dump_path).write_text(
            "".join(f"{sql};\n" for sql in pending), encoding="utf-8"
        )
        logger.info("発行予定 SQL %d 文 -> %s", len(pending), dump_path)

    if getattr(args, "verify", False):
        before = None
        before_path = getattr(args, "compare_to", None)
        if before_path and Path(before_path).exists():
            before = json.loads(Path(before_path).read_text(encoding="utf-8"))
        problems = _verify(state, before)
        _report(state, pending)
        if problems:
            for p in problems:
                ctx.add_failure("verify", p)
            return
        logger.info("検証 OK: G-core-2 / G-core-3 / G-core-5 をすべて満たす")
        ctx.add_success()
        return

    if not getattr(args, "apply", False):
        _report(state, pending)
        logger.info("=== check-only（1文も発行していない）===")
        ctx.add_success()
        return

    if not pending:
        logger.info("未適用の DDL は無い（適用済み）")
        ctx.add_success()
        return

    # --- ここからが唯一の書込。ALTER / CREATE INDEX 以外は出さない ---------
    for sql in pending:
        head = sql.split()[0].upper()
        if head not in ("ALTER", "CREATE"):
            ctx.add_failure("apply", f"P4a が発行してよいのは ALTER / CREATE INDEX のみ: {sql}")
            return
    for sql in pending:
        try:
            store.query(sql)
        except D1Error as exc:
            ctx.add_failure(sql, f"DDL 発行に失敗: {exc}")
            return
        logger.info("適用: %s", sql)

    try:
        after = _observe(store)
    except D1Error as exc:
        ctx.add_failure("observe", f"適用後の状態を読めない: {exc}")
        return
    problems = _verify(after, state)
    _report(after, cs.plan_ddl(set(after["columns"]), set(after["indexes"])))
    if problems:
        for p in problems:
            ctx.add_failure("verify", p)
        return
    logger.info("P4a 完了: 列 %d / 索引 %s", len(after["columns"]), after["indexes"])
    ctx.add_success()


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("①銘柄マスタ core_stocks への列追加 (移行 P4a)")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="何も書かず現状と差分だけ報告する（既定の挙動。明示用）",
    )
    parser.add_argument(
        "--apply", action="store_true", help="DDL を実際に発行する（唯一の書込モード）"
    )
    parser.add_argument(
        "--verify", action="store_true", help="検証だけ実行する (G-core-2/3/5)"
    )
    parser.add_argument(
        "--sql-dump", metavar="PATH", help="発行予定 SQL を実行せずファイルへ出す"
    )
    parser.add_argument(
        "--snapshot", metavar="PATH", help="既存行のスナップショットを JSON で保存する"
    )
    parser.add_argument(
        "--state-dump", metavar="PATH", help="現状(列/索引/件数/孤児)を JSON で保存する"
    )
    parser.add_argument(
        "--compare-to", metavar="PATH", help="--verify で突き合わせる適用前の状態 JSON"
    )
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
