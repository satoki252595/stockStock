"""master_sync: EDINETコードリスト → ① 銘柄マスタ同期 (DESIGN.md §8.2, P1)。

フロー (§8.1): fetch → save raw → convert → ⑤UL(必須) → parse → ① upsert → ⑦記録。
原本アップロード失敗時は構造化データを一切書き込まず異常終了する (§8.1-4)。
"""

from __future__ import annotations

import logging

from ..collectors import edinet_codelist
from ..notion import file_upload, upsert
from .runner import JobContext, apply_limit, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "master_sync"


def execute(ctx: JobContext) -> None:
    # 1-3. Fetch / Save raw / Convert
    artifact = edinet_codelist.fetch_codelist(ctx.settings)
    artifact = edinet_codelist.convert_codelist(artifact)

    # 4. ⑤へ原本+変換版を必ずアップロード。RawUploadError はそのまま伝播し
    #    ジョブ失敗となる（構造化書き込みはこの後なので一切行われない §8.1-4）
    raw_page_id = file_upload.upload_raw_artifact(ctx.client, ctx.settings, artifact)

    # 5. Transform
    records = edinet_codelist.parse_codelist(
        artifact.local_path.read_bytes(), raw_page_id=raw_page_id
    )
    records = apply_limit(records, ctx.args.limit)
    logger.info("コードリスト: %d 銘柄を ① へ upsert", len(records))

    # 6. Upsert (冪等キー=銘柄コード)
    for record in records:
        try:
            upsert.upsert_stock_master(ctx.client, ctx.settings, record)
            ctx.add_success()
        except Exception as exc:
            ctx.add_failure(record.code, f"①upsert失敗: {exc}")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("EDINETコードリスト → ① 銘柄マスタ同期 (月1)")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
