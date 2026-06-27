"""export_weekly: 全履歴 Parquet/CSV を ⑥ 時系列エクスポートへ更新 (§8.2, §7)。

- トラックA (commercial-ok / factual-cite) と トラックB (personal-only) を
  **別ファイル** に分離する (§8.2)。公開可否はライセンスタグで構造的に判定
  (licensing.is_publishable / is_metadata_publishable §2.2)
- トラックB: 株価全履歴 (yfinance 取得、Parquet+CSV)
- トラックA: ③ 財務サマリ・④ 開示メタデータの全件エクスポート
  （Notion 上の commercial-ok / factual-cite 行のみ）
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from ..collectors import yfinance_prices
from ..licensing import LicenseTag, inherit, is_metadata_publishable
from ..models import Provenance, Source, now_jst
from ..notion import file_upload, upsert
from ..notion import schema as S
from .prices_daily import resolve_codes
from .runner import JobContext, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "export_weekly"
EXPORT_DIR = Path("data/export")

DATASET_PRICES_B = "株価全履歴（トラックB・personal-only）"
DATASET_FINANCIALS_A = "財務サマリ全件（トラックA）"
DATASET_DISCLOSURES_A = "開示メタデータ全件（トラックA）"


def _write_dataset(df: pd.DataFrame, stem: str) -> list[Path]:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    paths = [EXPORT_DIR / f"{stem}.parquet", EXPORT_DIR / f"{stem}.csv"]
    df.to_parquet(paths[0], index=False)
    df.to_csv(paths[1], index=False)
    return paths


def _upload_dataset(
    ctx: JobContext,
    df: pd.DataFrame,
    *,
    dataset_name: str,
    stem: str,
    schema_desc: str,
    license_tag: LicenseTag,
    source: Source,
    period: str | None,
    raw_page_id: str | None = None,
) -> None:
    paths = _write_dataset(df, stem)
    uploads = [(file_upload.upload_file(ctx.client, p), p.name) for p in paths]
    upsert.create_export_row(
        ctx.client,
        ctx.settings,
        dataset_name,
        period=period,
        row_count=len(df),
        schema_desc=schema_desc,
        provenance=Provenance(
            source=source,
            license_tag=license_tag,
            data_date=now_jst().date(),
            fetched_at=now_jst(),
            raw_page_id=raw_page_id,
        ),
        file_uploads=uploads,
    )
    ctx.add_success()
    logger.info("⑥ 更新: %s (%d 行, %s)", dataset_name, len(df), [p.name for p in paths])


# ---------------------------------------------------------------------------
# トラックB: 株価全履歴
# ---------------------------------------------------------------------------


def export_prices_track_b(ctx: JobContext) -> None:
    codes = resolve_codes(ctx)
    if not codes:
        logger.warning("対象銘柄なし。株価エクスポートをスキップ")
        return
    artifact, frames, missing = yfinance_prices.fetch_daily_batch(
        ctx.settings, codes, period=ctx.args.period
    )
    ctx.upload_raw(artifact)
    for code in missing:
        ctx.add_failure(code, "株価履歴取得失敗 (エクスポートから欠落)")
    if not frames:
        logger.warning("取得できた株価がないためトラックBエクスポートを生成しない (§3-1)")
        return

    long_parts = []
    for code, df in frames.items():
        part = df.copy()
        part.columns = [str(c).lower() for c in part.columns]
        if "date" not in part.columns:
            part = part.reset_index()
            part.columns = [str(c).lower() for c in part.columns]
        part.insert(0, "code", code)
        long_parts.append(part)
    long_df = pd.concat(long_parts, ignore_index=True)
    dates = pd.to_datetime(long_df["date"])
    period = f"{dates.min().date()}..{dates.max().date()}"

    _upload_dataset(
        ctx,
        long_df,
        dataset_name=DATASET_PRICES_B,
        stem=f"prices_all_{now_jst().strftime('%Y%m%d')}",
        schema_desc=(
            "code, date, open, high, low, close, volume(, adj close)。"
            "ソース=yfinance (personal-only §2.1。公開・商用利用不可)"
        ),
        license_tag=inherit([LicenseTag.PERSONAL_ONLY]),
        source=Source.YFINANCE,
        period=period,
        raw_page_id=artifact.notion_page_id,  # 由来する取得単位の原本 (§3-3)
    )


# ---------------------------------------------------------------------------
# トラックA: Notion ③④ のエクスポート (commercial-ok / factual-cite のみ)
# ---------------------------------------------------------------------------


def _flatten_page(page: dict) -> dict:
    """Notion ページのプロパティをフラットな dict にする（値不変・型のみ変換）。"""
    out: dict = {}
    for name, prop in page.get("properties", {}).items():
        ptype = prop.get("type")
        if ptype == "title":
            out[name] = "".join(i.get("plain_text", "") for i in prop.get("title", []))
        elif ptype == "rich_text":
            out[name] = "".join(i.get("plain_text", "") for i in prop.get("rich_text", []))
        elif ptype == "number":
            out[name] = prop.get("number")
        elif ptype == "select":
            sel = prop.get("select")
            out[name] = sel.get("name") if sel else None
        elif ptype == "date":
            d = prop.get("date")
            out[name] = d.get("start") if d else None
        elif ptype == "checkbox":
            out[name] = prop.get("checkbox")
        elif ptype == "url":
            out[name] = prop.get("url")
    return out


def _track_a_filter() -> dict:
    """公開可タグ (commercial-ok / factual-cite) のみ §2.2。"""
    publishable = [t for t in LicenseTag if is_metadata_publishable(t)]
    return {
        "or": [
            {"property": S.PROP_LICENSE_TAG, "select": {"equals": tag.value}}
            for tag in publishable
        ]
    }


def export_notion_db_track_a(
    ctx: JobContext, db_key: str, dataset_name: str, stem: str, schema_desc: str
) -> None:
    pages = ctx.client.query_database(ctx.settings.db_id(db_key), filter=_track_a_filter())
    if not pages:
        logger.info("%s: 対象行なし (トークン無し dry-run か未投入)。スキップ", dataset_name)
        return
    rows = [_flatten_page(p) for p in pages]
    df = pd.DataFrame(rows)
    tags = [
        LicenseTag(p["properties"][S.PROP_LICENSE_TAG]["select"]["name"])
        for p in pages
        if p.get("properties", {}).get(S.PROP_LICENSE_TAG, {}).get("select")
    ]
    _upload_dataset(
        ctx,
        df,
        dataset_name=dataset_name,
        stem=f"{stem}_{now_jst().strftime('%Y%m%d')}",
        schema_desc=schema_desc,
        # 含まれる行の最も厳しいタグを継承 (§2.2)。
        # 複数ソース行の集約データセットのためソースは「計算」とし、
        # 出典 (EDINET/TDnet) は schema_desc に明記する (§3-4)
        license_tag=inherit(tags) if tags else LicenseTag.FACTUAL_CITE,
        source=Source.CALC,
        period=None,
    )


def execute(ctx: JobContext) -> None:
    export_prices_track_b(ctx)
    export_notion_db_track_a(
        ctx, "financials", DATASET_FINANCIALS_A, "financials_all",
        "③ 財務サマリの全プロパティ (commercial-ok/factual-cite のみ。出典: EDINET（金融庁）/TDnet)",
    )
    export_notion_db_track_a(
        ctx, "disclosures", DATASET_DISCLOSURES_A, "disclosures_all",
        "④ 開示メタデータ (タイトル・開示日時・種別・原文URL。原文PDFは含まない §2.1)",
    )


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("全履歴エクスポート → ⑥ (週1日曜。A/B別ファイル §8.2)")
    parser.add_argument("--period", default="5y", help="株価履歴の取得期間 (yfinance period)")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
