"""core_stocks_migrate: ①銘柄マスタへの列追加（移行 P4a）。

`core_stocks` は移行元 kabulab-cf が所有する既存表で、14 子表が `stock_id` で
参照している。P4a で発行するのは **`ALTER TABLE ... ADD COLUMN` と
`CREATE INDEX` だけ**で、既存行・既存列・子表には1バイトも触れない。

値の充填（`instrument_type` 等）は P4a の範囲外。列追加（DDL・1回きり）と
値の充填（UPDATE・繰り返し）で承認とロールバックの単位が違うため、別フェーズに
割っている。`sector33` の正本ソースと `instrument_type` の語彙が未決なのも理由
（詳細は docs/CF-CANONICAL-DESIGN.md の P4a 実施記録）。

## モード

- `--check-only`（既定）: 何も書かず、現状と発行予定の差分だけを報告する
- `--sql-dump PATH`: 発行予定の全文をファイルへ出す（G-core-1 の静的検査用）
- `--apply`: DDL を実際に発行する。**唯一の書込モード**
- `--verify`: 適用後の検証（G-core-2 / G-core-3 / G-core-5 / E7）だけを実行する。
  **`core_stocks` へは SELECT / PRAGMA しか発行しないので CI から毎日回せる。**
  `.github/workflows/ops_check.yml` の第3ステップが `--compare-to` なしで呼ぶ。
  なお「1 文も書かない」ではない: 他の全ジョブと同じく `jobs.runner.run_job` が
  終了時に ⑦ `jss_job_runs` へ 1 行 INSERT する（移行対象表には触らないが、
  読み取り専用トークンでは動かない）

`--snapshot PATH` を付けると、既存 3,818 行のスナップショットを JSON で保存する
（ロールバックの原本）。
"""

from __future__ import annotations

import hashlib
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
    """D1 が使えないなら失敗として記録する。

    検証・適用のジョブは「対象に触れなかった」を成功にしてはいけない。
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
    database_id = (
        cloud.settings.kabulab_d1_database_id or cloud.settings.d1_database_id
    )
    return D1Store(cloud.settings, writer=JOB_NAME, database_id=database_id)


def _observe(store: D1Store) -> dict:
    """現状を読む（SELECT / PRAGMA のみ）。

    名前だけでなく**定義**まで持つ。列名の集合しか見ていないと
    「型の違う同名列」「(is_active, market) ではなく (sector) 上に作られた
    同名索引」を素通りさせる（`CREATE INDEX IF NOT EXISTS` は no-op になる）。
    行の値は `SNAPSHOT_SQL` のハッシュで丸ごと突き合わせる。
    """
    columns = {
        str(r["name"]): {
            "type": str(r["type"] or ""),
            "notnull": int(r["notnull"] or 0),
            "dflt": r["dflt_value"],
        }
        for r in store.query(cs.TABLE_INFO_SQL)
    }
    indexes = {
        str(r["name"]): cs.normalize_sql(r.get("sql")) for r in store.query(cs.INDEX_LIST_SQL)
    }
    counts = (store.query(cs.COUNTS_SQL) or [{}])[0]
    seq_rows = store.query(cs.SEQ_SQL)
    orphans: dict[str, int] = {}
    for sql in cs.orphan_check_statements():
        orphans.update({str(r["t"]): int(r["n"] or 0) for r in store.query(sql)})
    rows = store.query(cs.SNAPSHOT_SQL)
    digest = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "columns": columns,
        "indexes": indexes,
        "counts": {k: (int(v) if v is not None else None) for k, v in counts.items()},
        "sqlite_sequence": int(seq_rows[0]["seq"]) if seq_rows else None,
        "orphans": orphans,
        "rows": len(rows),
        "rows_sha256": digest,
    }


def _report(state: dict, pending: list[str]) -> None:
    logger.info(
        "列 %d 個 / 索引 %s", len(state["columns"]), sorted(state["indexes"])
    )
    logger.info("行 %s (sha256 %s)", state.get("rows"), str(state.get("rows_sha256"))[:16])
    logger.info("件数: %s / sqlite_sequence=%s", state["counts"], state["sqlite_sequence"])
    bad = {t: n for t, n in state["orphans"].items() if n}
    logger.info("孤児: %s", bad or "全子表で 0 件")
    logger.info("未適用の DDL: %d 文", len(pending))
    for sql in pending:
        logger.info("  %s", sql)


def _verify(state: dict, before: dict | None) -> list[str]:
    """G-core-2 / G-core-3 / G-core-5 / E7 を突き合わせる。差分の説明を返す。

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
    for name, want_sql in cs.NEW_INDEXES.items():
        got_sql = state["indexes"].get(name)
        if got_sql is None:
            problems.append(f"追加索引が入っていない: {name}")
        elif got_sql != cs.normalize_sql(want_sql):
            problems.append(
                f"同名だが定義の違う索引がある: {name} 実際={got_sql!r} 期待={cs.normalize_sql(want_sql)!r}"
            )
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
    for name in set(before["columns"]) & set(state["columns"]):
        if before["columns"][name] != state["columns"][name]:
            problems.append(
                f"既存列 {name} の定義が変わった: "
                f"{before['columns'][name]} -> {state['columns'][name]}"
            )
    lost_idx = set(before["indexes"]) - set(state["indexes"])
    if lost_idx:
        problems.append(f"既存索引が消えた: {sorted(lost_idx)}")
    for name in set(before["indexes"]) & set(state["indexes"]):
        if before["indexes"][name] != state["indexes"][name]:
            problems.append(f"既存索引 {name} の定義が変わった")
    # G-core-3: 既存行が1バイトでも変わっていないこと。件数一致だけでは
    # 「全行の name を書き換えた」「is_active を反転した」を見逃す。
    if before.get("rows_sha256") and before["rows_sha256"] != state.get("rows_sha256"):
        problems.append(
            f"G-core-3: 既存行の内容が変わった "
            f"(sha256 {before['rows_sha256'][:16]} -> {str(state.get('rows_sha256'))[:16]})"
        )
    return problems


def _already_applied(exc: Exception) -> bool:
    """「もう入っている」ことを示す D1 のエラーか。"""
    text = str(exc).lower()
    return "duplicate column name" in text or "already exists" in text


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
        logger.info("検証 OK: G-core-2 / G-core-3 / G-core-5 / E7 をすべて満たす")
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
            # 非冪等な DDL なので自動再送を止める。再送されると D1 側では
            # 成功しているのに 2 回目が duplicate column name を返し、
            # 「適用済みなのに失敗」と誤って報告される。
            store.query(sql, idempotent=False)
        except D1Error as exc:
            if _already_applied(exc):
                # 応答が失われただけで実体は入っている可能性がある。
                # 打ち切らず、最後の _observe と _verify に判断させる。
                logger.warning("既に適用済みとして続行: %s (%s)", sql, exc)
                continue
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
        "--verify",
        action="store_true",
        help="検証だけ実行する (G-core-2/3/5 と E7 の列定義ドリフト)",
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
