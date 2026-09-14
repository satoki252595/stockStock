"""ops_check: 鮮度とジョブ結果を読んで SLO 違反だけを報告する。

**何も書かない。** 読んで判定し、違反があれば非ゼロで終了する。
記録は `jobs/freshness_probe.py`（別ジョブ）が行い、Issue は GitHub Actions 側が
立てる（記録・判定・通知を分ける）。

これまで「壊れても音が鳴らない」状態だった:
- 14 workflow に `if: failure()` も webhook も 0 行
- `jss_job_runs` / `jss_dataset_freshness` は器だけで 0 行
- stockStock は一部失敗を exit 0 にするので毎日 failed>0 でも緑
- kabulab-cf は failed>0 で exit 1 なので 35% が赤（どちらも情報量ゼロ）

判定は「異常だけを出す」。正常時に何も言わないのは、通知が多いと
見なくなるため（設計書 §7.6）。**この原則は自分自身にも適用する**: 毎日必ず
鳴る判定を作ったら、それは通知を殺すのと同じである。だから既知の赤は
`slo.ACCEPTED_RED` で宣言して警告に落とし、空振り検知からは診断ジョブを除く。
更新しないと決めたデータセット（`slo.NOT_REFRESHED`）は加齢を判定せず、
「判定対象外」と理由をログに出す（0 行・測れないは引き続き違反にする）。
"""

from __future__ import annotations

import logging

from ..cloud_store import slo
from ..cloud_store.d1 import D1Error, D1Store
from .runner import STATUS_SUCCESS, JobContext, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "ops_check"

# 観測ジョブ（鮮度を記録する側）。生存確認の対象。
PROBE_JOB_NAME = "freshness_probe"

# 判定に必要な4列。row_or_object_count を読まないと「0 件なのに緑」が起きる。
FRESHNESS_SQL = (
    "SELECT dataset, latest_data_date, row_or_object_count, updated_at"
    " FROM jss_dataset_freshness"
)

# 空振り検知から外すジョブ。収集を1件もしないのが正常な診断・観測ジョブたち。
#
# allowlist（収集ジョブだけを検知対象にする）にはしない。新しい収集ジョブを
# 足した人が登録を忘れると「黙って検知しない穴」が空くが、denylist の書き忘れは
# 「うるさいが安全な誤警報」で済む。安全側に倒す。
DIAGNOSTIC_JOBS: tuple[str, ...] = ("ops_check", "cloud_check", PROBE_JOB_NAME)

# 直近の実行で「処理ゼロの成功」が続いていないか。EDINET の 11 営業日は
# これで拾える。D1 の compound SELECT 上限を避けて 1 文にする。
#
# `AND status = ?` が無いと**失敗した実行まで「処理ゼロで成功している」と報告する**。
# 実測で `jss_job_runs` の 2 行はどちらも ops_check 自身の
# `status='失敗' processed=0` で、このまま 3 日続けば発火する状態だった。
#
# `finished_at >= ... -30 days` (L-17): 全行の ROW_NUMBER は表が育つと
# 全走査になる。`idx_jss_job (job_name, finished_at)` を効かせるため
# 直近 30 日に窓を切る。`record_job_run` の 90 日剪定と対で範囲を保つ。
IDLE_RUNS_SQL = (
    "SELECT job_name, COUNT(*) AS n FROM ("
    "  SELECT job_name, processed, status, ROW_NUMBER() OVER"
    "   (PARTITION BY job_name ORDER BY finished_at DESC) AS rn"
    "  FROM jss_job_runs WHERE job_name NOT IN ({placeholders})"
    "  AND finished_at >= strftime('%s','now','-30 days')"
    ") WHERE rn <= ? AND processed = 0 AND status = ? GROUP BY job_name"
    " HAVING COUNT(*) >= ?"
).format(placeholders=", ".join("?" for _ in DIAGNOSTIC_JOBS))

# 観測ジョブの生存確認。**status を絞らないと「毎日失敗していても生きている」を
# 返す**（runner は status に関わらず 1 行書く）。
PROBE_ALIVE_SQL = (
    "SELECT MAX(finished_at) AS last_ok FROM jss_job_runs"
    " WHERE job_name = ? AND status = ?"
)

# 連続で処理ゼロが続いたら異常とみなす本数。
IDLE_RUN_WINDOW = 5
IDLE_RUN_THRESHOLD = 3

# 観測ジョブが日次なので、2 日黙ったら止まっていると見る（祝日でも cron は動く）。
PROBE_MAX_SILENCE_HOURS = 48.0


def _check_freshness(ctx: JobContext, store: D1Store, problems: list[str]) -> None:
    """鮮度表を判定する。宣言済みの赤は警告に落とす。"""
    try:
        rows = store.query(FRESHNESS_SQL)
    except D1Error as exc:
        ctx.add_failure("freshness", f"鮮度表を読めない: {exc}")
        return

    if not rows:
        # 空を「問題なし」と読ませない。器はあるのに writer が動いていない状態。
        ctx.add_failure(
            "freshness",
            "jss_dataset_freshness が空。freshness_probe を先に走らせること"
            f"（`python -m jp_stock_pipeline.jobs.{PROBE_JOB_NAME}`）。"
            "それでも空なら記録する writer が動いていない",
        )
        return

    for row in rows:
        dataset = str(row.get("dataset") or "")
        latest_data_date = row.get("latest_data_date")
        source_epoch = row.get("updated_at")
        row_count = row.get("row_or_object_count")
        verdict = slo.judge_observation(
            dataset,
            latest_data_date=latest_data_date,
            source_epoch=source_epoch,
            row_count=row_count,
        )
        age = slo.observation_age_hours(
            dataset, latest_data_date=latest_data_date, source_epoch=source_epoch
        )
        age_txt = f"{age / 24:.1f}日" if age is not None else "不明"
        basis = f"基準日 {latest_data_date}" if latest_data_date else "取得時刻"
        line = f"{dataset}: {verdict} ({basis}から {age_txt} / {row_count} 行)"

        exempt_reason = slo.NOT_REFRESHED.get(dataset)
        if verdict == slo.VERDICT_NOT_REFRESHED:
            # 更新しないデータセット。件数と止まっている日数は出すが判定しない。
            # info にするのは、毎日必ず出る warning は読まれなくなるから
            # （`ACCEPTED_RED` の赤は「いつか直す」ので warning のまま）。
            logger.info(
                "%s: 判定対象外 (%sから %s / %s 行) ← 理由: %s",
                dataset, basis, age_txt, row_count, exempt_reason,
            )
            continue
        if exempt_reason is not None:
            # 判定対象外でも 0 行（red）と測れない（unknown）は通す。
            # 更新しないことと、再取得不能な資産が消えてよいことは違う。
            line = f"{line} ← 判定対象外のデータセットだが加齢と無関係な異常"
            problems.append(line)
            logger.warning("%s（判定対象外の理由: %s）", line, exempt_reason)
            continue

        reason = slo.ACCEPTED_RED.get(dataset)
        if reason is not None and verdict == "red":
            # 宣言済みの赤。ログには出すが終了コードは落とさない。
            logger.warning("%s ← 受容済み: %s", line, reason)
            continue
        if reason is not None and verdict in ("green", "yellow"):
            # 直った。宣言を外せることを報告する（失敗にはしない）。
            logger.warning(
                "%s ← slo.ACCEPTED_RED から %s の宣言を外せる（受容理由: %s）",
                line, dataset, reason,
            )
            continue
        if verdict in ("red", "yellow", "unknown"):
            # unknown は宣言済みでも通す。「直った」ではなく「測れていない」ため。
            problems.append(line)
            logger.warning("%s", line)
            continue
        logger.info("%s", line)

    # SLO を定義している、または判定対象外として観測を続けると宣言しているのに
    # 鮮度表に載っていないデータセット。判定対象外を外すと、観測が止まって
    # 「0 行になった」を検知する手段が消えても静かなままになる。
    expected = set(slo.SLO_BY_DATASET) | set(slo.NOT_REFRESHED)
    missing = sorted(expected - {str(r.get("dataset") or "") for r in rows})
    if missing:
        problems.append(f"鮮度が記録されていないデータセット: {missing}")


def _check_probe_alive(store: D1Store, problems: list[str]) -> None:
    """観測ジョブが実際に成功しているかを見る。"""
    try:
        rows = store.query(PROBE_ALIVE_SQL, [PROBE_JOB_NAME, STATUS_SUCCESS])
    except D1Error as exc:
        logger.warning("観測ジョブの生存を確認できない: %s", exc)
        return
    last_ok = rows[0].get("last_ok") if rows else None
    if not last_ok:
        problems.append(
            f"{PROBE_JOB_NAME} が一度も成功していない（鮮度表の値が凍結している疑い）"
        )
        return
    age = slo.age_hours(int(last_ok))
    if age is not None and age > PROBE_MAX_SILENCE_HOURS:
        problems.append(
            f"{PROBE_JOB_NAME} の最後の成功から {age / 24:.1f}日"
            f"（上限 {PROBE_MAX_SILENCE_HOURS / 24:.0f}日）。観測が止まっている"
        )


def _check_idle_runs(store: D1Store, problems: list[str]) -> None:
    """処理ゼロの「成功」が続いているジョブを拾う。"""
    params = [*DIAGNOSTIC_JOBS, IDLE_RUN_WINDOW, STATUS_SUCCESS, IDLE_RUN_THRESHOLD]
    try:
        idle = store.query(IDLE_RUNS_SQL, params)
    except D1Error as exc:
        logger.warning("ジョブ履歴を読めない: %s", exc)
        return
    for row in idle:
        problems.append(
            f"{row['job_name']}: 直近 {IDLE_RUN_WINDOW} 回中 {row['n']} 回が"
            " 処理ゼロで成功している（空振りの疑い）"
        )


def execute(ctx: JobContext) -> None:
    # `ctx.cloud` は runner が `if not settings.dry_run:` の中でしか作らないため
    # 参照すると --dry-run が必ず即失敗する。設定から直接 D1Store を組む
    # （cloud_check.py と同じ形）。読み取りしかしないので dry-run でも安全。
    settings = ctx.settings.cloud_store
    if not settings.d1_enabled():
        ctx.add_failure("d1", "D1 が未設定。鮮度を判定できない")
        return
    store = D1Store(settings, writer=JOB_NAME)

    problems: list[str] = []
    _check_freshness(ctx, store, problems)
    if ctx.failed:
        # 鮮度表そのものが読めない/空 → それ以上の判定は意味がない
        return
    _check_probe_alive(store, problems)
    _check_idle_runs(store, problems)

    if problems:
        for p in problems:
            ctx.add_failure("slo", p)
        return
    logger.info(
        "SLO 違反なし（宣言済みの赤 %d 件は警告のみ・判定対象外 %d 件: %s）",
        len(slo.ACCEPTED_RED), len(slo.NOT_REFRESHED), sorted(slo.NOT_REFRESHED),
    )
    ctx.add_success()


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("鮮度とジョブ結果の SLO 判定（読み取りのみ）")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
