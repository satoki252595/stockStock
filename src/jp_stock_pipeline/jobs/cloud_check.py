"""cloud_check: Cloudflare 正本への疎通確認（読み取りのみ・何も書かない）。

資格情報が正しいかを本番へ書き込む前に確かめるための診断ジョブ。

**破壊的な操作も書き込みも一切しない。** R2 は存在しないキーへの head_object で
判定する（認証が通っていれば 404、権限が無ければ 403 が返る）。D1 は SELECT のみ。
R2 に delete_object が無い設計なので、疎通のためにプローブ用オブジェクトを書くと
消せないゴミが残る。それを避けるための読み取り判定である。

秘密情報は一切ログに出さない（有無と長さのみ）。
"""

from __future__ import annotations

import logging

from ..cloud_store.d1 import D1Error, D1Store
from ..cloud_store.r2 import R2Error, R2Store
from .runner import JobContext, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "cloud_check"

# 認証が通っていれば 404、権限不足なら 403 が返る。読み取りだけで判定できる。
PROBE_KEY = "_cloud_check/never-written"


def _mask(value: str | None) -> str:
    return f"設定あり(長さ{len(value)})" if value else "未設定"


def execute(ctx: JobContext) -> None:
    settings = ctx.settings.cloud_store
    logger.info(
        "資格情報: CF_ACCOUNT_ID=%s / R2_ACCESS_KEY_ID=%s / R2_SECRET_ACCESS_KEY=%s"
        " / CF_API_TOKEN=%s / CF_D1_DATABASE_ID=%s",
        _mask(settings.cf_account_id), _mask(settings.r2_access_key_id),
        _mask(settings.r2_secret_access_key), _mask(settings.cf_api_token),
        _mask(settings.d1_database_id),
    )

    if not settings.r2_enabled():
        ctx.add_failure("R2", "資格情報が揃っていない (CF_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY)")
    else:
        for bucket in (settings.bucket_raw, settings.bucket_supply):
            store = R2Store(settings, bucket, writer=JOB_NAME)
            try:
                found = store.exists(PROBE_KEY)
            except R2Error as exc:
                ctx.add_failure(f"R2:{bucket}", f"到達不能または権限不足: {exc}")
                continue
            logger.info("R2 %s: 疎通OK (プローブキーの存在=%s)", bucket, found)
            ctx.add_success()

    if not settings.d1_enabled():
        ctx.add_failure("D1", "資格情報が揃っていない (CF_ACCOUNT_ID / CF_API_TOKEN / CF_D1_DATABASE_ID)")
        return
    store = D1Store(settings, writer=JOB_NAME)
    try:
        rows = store.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'jss_%' ORDER BY name"
        )
    except D1Error as exc:
        ctx.add_failure("D1", f"到達不能または権限不足: {exc}")
        return
    names = [r.get("name") for r in rows]
    logger.info("D1: 疎通OK jss_ テーブル %d 件: %s", len(names), ", ".join(names))
    if len(names) < 10:
        ctx.add_failure("D1", f"jss_ テーブルが {len(names)} 件しかない (期待 10 件)")
        return
    ctx.add_success()


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("Cloudflare 正本への疎通確認 (読み取りのみ)")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
