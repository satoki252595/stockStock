"""yutai_backup: ⑨優待の LLM 派生値を R2 へ退避する (移行 P3)。

`docs/CF-CANONICAL-DESIGN.md` の移行 P3。⑨優待は TDnet の一次開示から作り直すが、
移行元 kabulab-cf の D1 が持つ `short_summary` / `estimated_value` は
**作り直せない**（出典サイトの規約が再取得を禁じている）。作り直しが終わるまでの
保険としてこれだけを退避する。出典サイトの掲載文 `description` は退避しない。

定期実行しない (`workflow_dispatch` のみ)。同じ中身なら同じキーになるので
何度流しても R2 上のオブジェクトは増えない。
"""

from __future__ import annotations

import logging

from ..cloud_store import yutai
from ..cloud_store.d1 import D1Error, D1Store
from ..cloud_store.guards import GuardError
from ..cloud_store.r2 import R2Store
from ..models import now_jst
from ..rawstore import sha256_bytes
from .runner import JobContext, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "yutai_backup"


def execute(ctx: JobContext) -> None:
    cloud = ctx.cloud
    if cloud is None or not cloud.settings.r2_enabled():
        # 資格情報が無い環境（dry-run / ローカル）では何もしない。margin_weekly と同じ扱い。
        logger.warning("R2 未設定のため退避しない（no-op）")
        return
    settings = cloud.settings
    source_db = settings.kabulab_d1_database_id
    if not source_db or not settings.cf_api_token or not settings.cf_account_id:
        logger.warning(
            "KABULAB_D1_DATABASE_ID / CF_API_TOKEN が未設定のため退避しない（no-op）"
        )
        return

    d1 = D1Store(settings, writer=JOB_NAME, database_id=source_db)
    try:
        rows = yutai.fetch_rows(d1.query)
        source_counts = d1.query(yutai.COUNT_SQL)
    except D1Error as exc:
        ctx.add_failure("d1", f"移行元 D1 を読めない: {exc}")
        return

    counts = yutai.count_rows(rows)
    # 元 DB の COUNT と退避結果がずれたら、ページング途中で取りこぼしている。
    declared = int((source_counts[0] if source_counts else {}).get("rows") or 0)
    if declared and declared != counts["rows"]:
        ctx.add_failure(
            "d1", f"取得件数が元 DB と不一致: D1={declared} 取得={counts['rows']}"
        )
        return
    logger.info(
        "退避対象: %d 行 (推定額あり %d / 要約あり %d)",
        counts["rows"], counts["with_value"], counts["with_summary"],
    )

    # personal-only のため、公開 Worker が bind していないバケットへ隔離する。
    store = R2Store(settings, settings.bucket_supply, writer=JOB_NAME)
    try:
        index, _found = store.get_json(yutai.INDEX_KEY)
    except Exception as exc:  # noqa: BLE001 - 404 以外の失敗で「無かったこと」にしない
        ctx.add_failure("index", f"索引を読めないため退避しない: {exc}")
        return
    previous = yutai.latest_entry(index)

    try:
        yutai.check_floors(
            counts,
            min_ratio=float(getattr(ctx.args, "min_ratio", yutai.DEFAULT_MIN_RATIO)),
            previous=(previous or {}).get("counts") if previous else None,
            allow_shrink=bool(getattr(ctx.args, "allow_shrink", False)),
        )
        payload = yutai.build_snapshot(rows, source_db=source_db, counts=counts)
    except GuardError as exc:
        ctx.add_failure("guard", str(exc))
        return

    body = yutai.serialize(payload)
    digest = sha256_bytes(body)
    key = yutai.snapshot_key(digest)

    if previous and previous.get("sha256") == digest:
        logger.info("前回と同一内容のため退避しない: %s", key)
        ctx.add_success()
        return

    if getattr(ctx.args, "check_only", False):
        logger.info("=== check-only（書き込みなし） ===")
        logger.info("退避先: %s/%s (%d bytes)", settings.bucket_supply, key, len(body))
        logger.info("件数: %s", counts)
        logger.info("前回: %s", (previous or {}).get("counts"))
        ctx.add_success()
        return

    try:
        if store.exists(key):
            logger.info("同一内容が既にある: %s", key)
        else:
            store.put_bytes(key, body, content_type="application/json")
        entry = {
            "taken_at": now_jst().isoformat(),
            "key": key,
            "sha256": digest,
            "size_bytes": len(body),
            "counts": counts,
        }
        store.put_json_guarded(
            yutai.INDEX_KEY,
            yutai.append_index(index, entry),
            contract=yutai.INDEX_CONTRACT,
        )
    except Exception as exc:  # noqa: BLE001 - 失敗は欠測として記録し握りつぶさない
        ctx.add_failure(key, f"R2 へ退避できず: {exc}")
        return
    logger.info("退避完了: %s/%s (%d bytes)", settings.bucket_supply, key, len(body))
    ctx.add_success()


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("⑨優待の LLM 派生値を R2 へ退避 (移行 P3)")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="R2 へ1バイトも書かず、退避対象と件数だけを報告する",
    )
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="前回退避より件数が減っていても退避する（優待廃止などで意図的に減る場合）",
    )
    parser.add_argument(
        "--min-ratio",
        type=float,
        default=yutai.DEFAULT_MIN_RATIO,
        help=f"基準値に対する許容下限比 (既定 {yutai.DEFAULT_MIN_RATIO})",
    )
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
