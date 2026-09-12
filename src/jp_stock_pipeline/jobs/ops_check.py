"""ops_check: 鮮度とジョブ結果を読んで SLO 違反だけを報告する。

**何も書かない。** 読んで判定し、違反があれば非ゼロで終了する。
GitHub Actions 側はそれを見て Issue を立てる（記録・判定・通知を分ける）。

これまで「壊れても音が鳴らない」状態だった:
- 14 workflow に `if: failure()` も webhook も 0 行
- `jss_job_runs` / `jss_dataset_freshness` は器だけで 0 行
- stockStock は一部失敗を exit 0 にするので毎日 failed>0 でも緑
- kabulab-cf は failed>0 で exit 1 なので 35% が赤（どちらも情報量ゼロ）

判定は「異常だけを出す」。正常時に何も言わないのは、通知が多いと
見なくなるため（設計書 §7.6）。
"""

from __future__ import annotations

import logging

from ..cloud_store import slo
from ..cloud_store.d1 import D1Error, D1Store
from .runner import JobContext, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "ops_check"

FRESHNESS_SQL = "SELECT dataset, latest_data_date, updated_at FROM jss_dataset_freshness"

# 直近の実行で「処理ゼロの成功」が続いていないか。EDINET の 11 営業日は
# これで拾える。D1 の compound SELECT 上限を避けて 1 文にする。
IDLE_RUNS_SQL = (
    "SELECT job_name, COUNT(*) AS n FROM ("
    "  SELECT job_name, processed, ROW_NUMBER() OVER"
    "   (PARTITION BY job_name ORDER BY finished_at DESC) AS rn"
    "  FROM jss_job_runs"
    ") WHERE rn <= ? AND processed = 0 GROUP BY job_name HAVING COUNT(*) >= ?"
)

# 連続で処理ゼロが続いたら異常とみなす本数。
IDLE_RUN_WINDOW = 5
IDLE_RUN_THRESHOLD = 3


def execute(ctx: JobContext) -> None:
    cloud = ctx.cloud
    if cloud is None or not cloud.settings.d1_enabled():
        ctx.add_failure("d1", "D1 が未設定。鮮度を判定できない")
        return
    store = D1Store(cloud.settings, writer=JOB_NAME)

    try:
        rows = store.query(FRESHNESS_SQL)
    except D1Error as exc:
        ctx.add_failure("freshness", f"鮮度表を読めない: {exc}")
        return

    if not rows:
        # 空を「問題なし」と読ませない。器はあるのに writer が動いていない状態。
        ctx.add_failure(
            "freshness",
            "jss_dataset_freshness が空。記録する writer が動いていない可能性がある",
        )
        return

    problems: list[str] = []
    for row in rows:
        dataset = str(row.get("dataset") or "")
        verdict = slo.judge(dataset, row.get("updated_at"))
        age = slo.age_hours(row.get("updated_at"))
        age_txt = f"{age / 24:.1f}日" if age is not None else "不明"
        line = f"{dataset}: {verdict} (最終更新から {age_txt})"
        if verdict in ("red", "yellow", "unknown"):
            problems.append(line)
            logger.warning("%s", line)
        else:
            logger.info("%s", line)

    # SLO を定義しているのに鮮度表に載っていないデータセット
    missing = sorted(set(slo.SLO_BY_DATASET) - {str(r.get("dataset") or "") for r in rows})
    if missing:
        problems.append(f"鮮度が記録されていないデータセット: {missing}")

    try:
        idle = store.query(IDLE_RUNS_SQL, [IDLE_RUN_WINDOW, IDLE_RUN_THRESHOLD])
    except D1Error as exc:
        logger.warning("ジョブ履歴を読めない: %s", exc)
        idle = []
    for row in idle:
        problems.append(
            f"{row['job_name']}: 直近 {IDLE_RUN_WINDOW} 回中 {row['n']} 回が"
            " 処理ゼロで成功している（空振りの疑い）"
        )

    if problems:
        for p in problems:
            ctx.add_failure("slo", p)
        return
    logger.info("SLO 違反なし（%d データセット）", len(rows))
    ctx.add_success()


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("鮮度とジョブ結果の SLO 判定（読み取りのみ）")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
