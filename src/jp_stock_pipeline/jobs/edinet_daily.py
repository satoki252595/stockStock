"""edinet_daily: 当日書類一覧 → XBRL/CSV/PDF取得 → ③④⑤ (§8.2, P3。トラックA)。

- 財務系書類 (有報/訂正有報/四半期/半期) は type=5 CSV を優先取得し、
  無ければ type=1 XBRL をパース (§5.2)
- 全取得単位の原本を ⑤ へ必ずアップロード (§8.1-4)
- EDINET は commercial-ok (出典記載 §2.1)
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from ..collectors import edinet
from ..collectors.edinet_codelist import normalize_sec_code
from ..convert import json_to_parquet, xbrl_to_csv
from ..http import FetchError
from ..licensing import source_license
from ..models import ConvertStatus, Provenance, RawArtifact, Source, now_jst
from ..notion import file_upload, upsert
from ..transform import normalize
from .runner import JobContext, apply_limit, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "edinet_daily"

# 財務数値の抽出対象 (§4): 有報 120 / 訂正有報 130 / 四半期 140 / 半期 160
FINANCIAL_DOC_TYPES = frozenset({"120", "130", "140", "160"})


def _fetch_financial_tidy(
    ctx: JobContext, doc_id: str, code: str, data_date: date | None
):
    """type=5 CSV 優先 → 無ければ type=1 XBRL (§5.2)。(artifact, tidy|None) を返す。

    tidy 変換の失敗では原本を失わない: convert_status=失敗 を記録して
    原本はそのまま ⑤ アップロードに進める (§5.2「変換失敗時も原本保存は成立」)。
    """
    try:
        artifact = edinet.fetch_document(
            ctx.settings, doc_id, 5, code=code, data_date=data_date
        )
        parser = xbrl_to_csv.edinet_csv_zip_to_tidy
    except FetchError:
        artifact = edinet.fetch_document(
            ctx.settings, doc_id, 1, code=code, data_date=data_date
        )
        parser = xbrl_to_csv.xbrl_zip_to_tidy

    tidy = None
    try:
        tidy = parser(artifact.local_path.read_bytes(), code, doc_id)
        xbrl_to_csv.write_tidy(tidy, artifact)
    except Exception:
        logger.exception(
            "tidy 変換失敗 (原本は保全し ⑤ へ。③ 反映はスキップ §5.2): doc_id=%s", doc_id
        )
        artifact.convert_status = ConvertStatus.FAILED
    return artifact, tidy


def _process_document(ctx: JobContext, doc: dict, list_page_id: str) -> None:
    doc_id = doc["docID"]
    code = normalize_sec_code(doc.get("secCode")) or ""
    doc_type_code = str(doc.get("docTypeCode") or "")
    submit = doc.get("submitDateTime") or ""
    data_date: date | None = None
    if submit:
        try:
            data_date = datetime.strptime(submit[:10], "%Y-%m-%d").date()
        except ValueError:
            data_date = None

    doc_raw_page: str | None = None
    tidy = None
    tidy_artifact: RawArtifact | None = None

    # 財務系: CSV/XBRL → tidy 変換版付き原本を ⑤ へ (変換失敗でも原本は上げる §5.2)
    if doc_type_code in FINANCIAL_DOC_TYPES:
        tidy_artifact, tidy = _fetch_financial_tidy(ctx, doc_id, code, data_date)
        doc_raw_page = ctx.upload_raw(tidy_artifact)

    # PDF 原本 (§4 書類一覧の対象すべて)。失敗しても書類処理自体は継続
    try:
        pdf_artifact = edinet.fetch_document(
            ctx.settings, doc_id, 2, code=code, data_date=data_date
        )
        json_to_parquet.convert_artifact(pdf_artifact, "pdf")
        pdf_page = ctx.upload_raw(pdf_artifact)
        doc_raw_page = doc_raw_page or pdf_page
    except (FetchError, file_upload.RawUploadError) as exc:
        logger.warning("PDF取得/UL失敗 (書類処理は継続 doc_id=%s): %s", doc_id, exc)

    # ④ 開示書類 upsert (キー=docID)。原本は書類自身 → 無ければ一覧原本
    record = edinet.to_disclosure_record(doc, raw_page_id=doc_raw_page or list_page_id)
    # ① relation 解決(Notionクエリ)。失敗しても relation 無しで本体は書く（degrade）
    try:
        master_id = (
            upsert.find_stock_master_page(ctx.client, ctx.settings, record.code)
            if record.code
            else None
        )
    except Exception as exc:  # noqa: BLE001 - relation 解決失敗は本体を止めない
        master_id = None
        logger.warning(
            "① relation 解決失敗 (master_id=None で続行 doc_id=%s): %s", doc_id, exc
        )
    # ④ を Notion とローカルへ独立に書く（双方向フェールセーフ）
    if not ctx.persist(
        record,
        lambda: upsert.upsert_disclosure(ctx.client, ctx.settings, record, master_id),
        label=f"④{doc_id}",
    ):
        raise RuntimeError(f"④ を Notion/ローカル両系統に書けず: {doc_id}")

    # ③ 財務サマリ (有報は既定で本決算、四半期は tidy の DEI から導出)
    if tidy is not None and tidy_artifact is not None and code:
        prov = Provenance(
            source=Source.EDINET,
            license_tag=source_license(Source.EDINET),  # commercial-ok (§2.1)
            data_date=data_date,
            fetched_at=now_jst(),
            raw_page_id=tidy_artifact.notion_page_id,
        )
        fin = normalize.tidy_to_financial_record(
            tidy, code, prov,
            disclosure_type="本決算" if doc_type_code in ("120", "130") else None,
            disclosed_at=record.disclosed_at,
        )
        if fin is not None and not ctx.persist(
            fin,
            lambda: upsert.upsert_financial_summary(
                ctx.client, ctx.settings, fin, master_id
            ),
            label=f"③{doc_id}",
        ):
            raise RuntimeError(f"③ を Notion/ローカル両系統に書けず: {doc_id}")


def execute(ctx: JobContext) -> None:
    target_date = ctx.args.date or now_jst().date()

    # 1-4. 書類一覧の取得・原本⑤UL (失敗時 RawUploadError → 構造化書き込みなし §8.1-4)
    list_artifact, docs = edinet.list_documents(ctx.settings, target_date)
    json_to_parquet.convert_artifact(list_artifact, "json")
    list_page_id = ctx.upload_raw(list_artifact)

    targets = [d for d in docs if edinet.is_target_document(d) and edinet.has_sec_code(d)]
    targets = apply_limit(targets, ctx.args.limit)
    logger.info("対象書類 %d / 一覧 %d 件", len(targets), len(docs))

    for doc in targets:
        try:
            _process_document(ctx, doc, list_page_id)
            ctx.add_success()
        except Exception as exc:
            ctx.add_failure(doc.get("docID", "?"), f"書類処理失敗: {exc}")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("EDINET当日書類 → ③④⑤ (毎営業日21:00 JST)")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
