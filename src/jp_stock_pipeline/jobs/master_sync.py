"""master_sync: EDINETコードリスト → ① 銘柄マスタ同期 (DESIGN.md §8.2, P1)。

フロー (§8.1): fetch → save raw → convert → ⑤UL(必須) → parse → ① upsert → ⑦記録。
原本アップロード失敗時は構造化データを一切書き込まず異常終了する (§8.1-4)。
"""

from __future__ import annotations

import logging

from ..collectors import edinet_codelist
from ..notion import upsert
from .runner import JobContext, apply_limit, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "master_sync"

# 上場廃止検知の安全弁: 既存①に対しコードリストが極端に縮んだ取得は異常とみなし、
# 一括上場廃止を防ぐ (§ Phase3 blast-radius)。取得コード数が既存の50%未満なら中止。
MIN_CODELIST_COVERAGE = 0.5


def execute(ctx: JobContext) -> None:
    # 1-3. Fetch / Save raw / Convert
    artifact = edinet_codelist.fetch_codelist(ctx.settings)
    artifact = edinet_codelist.convert_codelist(artifact)

    # 4. ⑤へ原本+変換版を必ずアップロード。RawUploadError はそのまま伝播し
    #    ジョブ失敗となる（構造化書き込みはこの後なので一切行われない §8.1-4）
    raw_page_id = ctx.upload_raw(artifact)

    # 5. Transform（全件。上場廃止検知のため limit 前の全コードを保持）
    records = edinet_codelist.parse_codelist(
        artifact.local_path.read_bytes(), raw_page_id=raw_page_id
    )
    fetched_codes = {r.code for r in records}
    upsert_records = apply_limit(records, ctx.args.limit)
    logger.info("コードリスト: %d 銘柄を ① へ upsert", len(upsert_records))

    # 6. Upsert (冪等キー=銘柄コード)。状態/上場日/上場廃止日 は開示・消失が所有する
    #    ため codelist 同期では書かない (include_lifecycle=False § Phase3 二重所有回避)。
    for record in upsert_records:
        # Notion とローカルへ独立に書く（双方向フェールセーフ）。状態/上場日/廃止日は
        # 開示・消失が所有するため include_lifecycle=False で両系統とも書かない。
        if ctx.persist(
            record,
            lambda rec=record: upsert.upsert_stock_master(
                ctx.client, ctx.settings, rec, include_lifecycle=False
            ),
            label=f"①{record.code}",
            include_lifecycle=False,
        ):
            ctx.add_success()
        else:
            ctx.add_failure(record.code, "①: Notion/ローカル両系統に書けず")

    # 7. 上場廃止検知 (§ Phase3): コードリストから消えた銘柄を listed=False にする
    #    (状態=上場廃止 の確定は一次開示が所有)。--limit 指定時は部分取得のため
    #    誤判定回避でスキップする。
    if ctx.args.limit:
        logger.info("--limit 指定のため上場廃止検知はスキップ (部分取得 § Phase3)")
        return
    _detect_delistings(ctx, fetched_codes)


def _detect_delistings(ctx: JobContext, fetched_codes: set[str]) -> None:
    """① にあってコードリストから消えた銘柄を listed=False にする (§ Phase3)。

    EDINET 上場区分が非上場へ変わった＝取得停止の信号。listed のみ更新し、
    状態=上場廃止 の確定は一次開示 (apply_disclosure_lifecycle) に一本化する
    (コードリストの一時的揺らぎで誤った権威的状態を書かない §3-1/§3-7)。
    再上場時は次回 upsert が listed=True へ自己修復する。
    dry-run は ① クエリが空のため no-op。
    """
    existing = upsert.load_stock_master_map(ctx.client, ctx.settings)  # {code: page_id}
    # 安全弁: 取得コードが既存に対し極端に少ない＝異常取得とみなし一括廃止を防ぐ。
    # 1件でも誤って全銘柄を listed=False にすると prices_daily が全停止するため (§3-2)。
    if existing and len(fetched_codes) < MIN_CODELIST_COVERAGE * len(existing):
        ctx.add_failure(
            "codelist",
            f"取得 {len(fetched_codes)} 件が既存 {len(existing)} 件の "
            f"{MIN_CODELIST_COVERAGE:.0%} 未満。異常取得とみなし上場廃止検知を中止 (§ Phase3)",
        )
        return
    absent = [(code, pid) for code, pid in existing.items() if code not in fetched_codes]
    if not absent:
        return
    logger.warning(
        "コードリストから消えた %d 銘柄を取得停止(listed=False)にする (§ Phase3)", len(absent)
    )
    for code, page_id in absent:
        # listed=False を Notion とローカルへ独立に反映（双方向フェールセーフ）
        if ctx.persist_mark_absent(
            code,
            lambda pid=page_id: upsert.mark_master_absent_from_codelist(
                ctx.client, ctx.settings, pid
            ),
            label=f"①absent:{code}",
        ):
            ctx.add_success()
        else:
            ctx.add_failure(code, "listed=False: Notion/ローカル両系統に書けず")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("EDINETコードリスト → ① 銘柄マスタ同期 (月1)")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
