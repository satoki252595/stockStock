"""tdnet_hourly: やのしん当日分 → ④ + 原本。短信XBRL検出時は ③ へ反映 (§8.2, P3)。

- やのしんAPI失敗時は公式TDnetページパースへフォールバック (§11。実データのみ §3-2)
- 原本(一覧JSON/HTML)を ⑤ へ必ずアップロードしてから ④ を書く (§8.1-4)
- 短信 (classify=短信 かつ XBRL あり) は XBRL zip を取得 → tidy 変換 → ③ upsert
"""

from __future__ import annotations

import json
import logging
from datetime import date

from ..collectors import tdnet_official_fallback, tdnet_yanoshin
from ..convert import convert_artifact, xbrl_to_csv
from ..http import FetchError, fetch
from ..licensing import source_license
from ..models import DisclosureRecord, Provenance, RawArtifact, Source, now_jst
from ..notion import file_upload, upsert
from ..rawstore import save_raw
from ..transform import normalize
from .runner import JobContext, apply_limit, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "tdnet_hourly"

# 繁忙日（決算集中日）は1日1000件超もあるため上限を大きく取り、
# 上限到達時は公式ページ全件で補完する（無警告の取りこぼし防止）
YANOSHIN_LIMIT = 1000


def _collect(ctx: JobContext, target_date: date) -> list[tuple[RawArtifact, list[DisclosureRecord], dict[str, str]]]:
    """一覧取得。やのしん → 失敗時は公式ページ (§11)、上限到達時は公式で補完。

    返り値: [(原本, records, doc_id→XBRL URL), ...]（公式は1ページ=1原本 §5.1）
    """
    target = target_date.strftime("%Y%m%d")
    try:
        artifact, records = tdnet_yanoshin.list_disclosures(
            ctx.settings, target, limit=YANOSHIN_LIMIT
        )
        payload = json.loads(artifact.local_path.read_bytes())
        convert_artifact(artifact, "json")  # §5.2 ペア保存 (JSON→CSV+Parquet)
        batches = [(artifact, records, tdnet_yanoshin.xbrl_url_map(payload))]
        if len(records) >= YANOSHIN_LIMIT:
            logger.warning(
                "やのしん一覧が上限 %d 件に到達。公式TDnetページで補完する (§11)",
                YANOSHIN_LIMIT,
            )
            seen = {r.doc_id for r in records}
            for page_artifact, page_records in tdnet_official_fallback.fetch_list_pages(
                ctx.settings, target_date
            ):
                extra = [r for r in page_records if r.doc_id not in seen]
                seen |= {r.doc_id for r in extra}
                batches.append((page_artifact, extra, {}))
        return batches
    except (FetchError, ValueError) as exc:
        logger.warning("やのしんAPI失敗 → 公式TDnetページへフォールバック (§11): %s", exc)
        pages = tdnet_official_fallback.fetch_list_pages(ctx.settings, target_date)
        # 公式一覧から XBRL URL は取得しない（リンク有無のみ）。③反映はEDINET日次で補完
        return [(artifact, records, {}) for artifact, records in pages]


def _process_financial_xbrl(
    ctx: JobContext, record: DisclosureRecord, xbrl_url: str
) -> None:
    """短信 XBRL → 原本⑤ → tidy → ③ upsert (§8.2)。"""
    resp = fetch(xbrl_url)
    artifact = save_raw(
        resp.content,
        source=Source.TDNET,
        datatype="tdnet_xbrl",
        scope=record.code or record.doc_id,
        data_date=record.disclosed_at.date(),
        url=xbrl_url,
        ext="zip",
        license_tag=source_license(Source.TDNET),
        base_dir=ctx.settings.raw_data_dir,
    )
    tidy = xbrl_to_csv.xbrl_zip_to_tidy(
        artifact.local_path.read_bytes(), record.code or "", record.doc_id
    )
    xbrl_to_csv.write_tidy(tidy, artifact)
    raw_page_id = file_upload.upload_raw_artifact(ctx.client, ctx.settings, artifact)

    prov = Provenance(
        source=Source.TDNET,
        license_tag=source_license(Source.TDNET),  # factual-cite (§2.1)
        data_date=record.disclosed_at.date(),
        fetched_at=now_jst(),
        raw_page_id=raw_page_id,
    )
    fin = normalize.tidy_to_financial_record(
        tidy, record.code or "", prov, disclosed_at=record.disclosed_at
    )
    if fin is None:
        logger.info("③レコード生成不可 (決算期末を導出できず): %s", record.doc_id)
        return
    master_id = (
        upsert.find_stock_master_page(ctx.client, ctx.settings, fin.code)
        if fin.code
        else None
    )
    upsert.upsert_financial_summary(ctx.client, ctx.settings, fin, master_id)


def execute(ctx: JobContext) -> None:
    target_date = ctx.args.date or now_jst().date()
    batches = _collect(ctx, target_date)

    for artifact, records, xbrl_urls in batches:
        # 原本必須: 失敗時はこの取得単位の構造化書き込みをしない (§8.1-4)
        raw_page_id = file_upload.upload_raw_artifact(ctx.client, ctx.settings, artifact)

        for record in apply_limit(records, ctx.args.limit):
            record.provenance.raw_page_id = raw_page_id
            try:
                master_id = (
                    upsert.find_stock_master_page(ctx.client, ctx.settings, record.code)
                    if record.code
                    else None
                )
                upsert.upsert_disclosure(ctx.client, ctx.settings, record, master_id)
                ctx.add_success()
            except Exception as exc:
                ctx.add_failure(record.doc_id, f"④upsert失敗: {exc}")
                continue

            # 上場廃止/新規上場 開示は ① のライフサイクル状態へ反映 (§ Phase3)
            if record.doc_type in upsert.LIFECYCLE_DOC_TYPES:
                try:
                    upsert.apply_disclosure_lifecycle(ctx.client, ctx.settings, record)
                except Exception as exc:
                    ctx.add_failure(record.doc_id, f"①ライフサイクル更新失敗: {exc}")

            if record.doc_type == "短信" and record.has_xbrl and record.doc_id in xbrl_urls:
                try:
                    _process_financial_xbrl(ctx, record, xbrl_urls[record.doc_id])
                except Exception as exc:
                    # ③ 反映失敗は欠損として記録 (④ は成立済み。ダミーで埋めない §3-1)
                    ctx.add_failure(record.doc_id, f"短信XBRL→③失敗: {exc}")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("TDnet適時開示 → ④ (+短信XBRL→③) (平日9-19時毎時)")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
