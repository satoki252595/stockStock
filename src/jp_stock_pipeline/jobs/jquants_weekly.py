"""jquants_weekly: J-Quants確定値による突合検証+財務バックフィル (§8.2, §3-5。トラックB)。

- 無料版は12週遅延の確定値。既定対象日 = today - 12週 から直前の平日へ繰り下げ
- ② の終値と突合し、乖離閾値超の行に「要確認」フラグを付与
  （値の自動書き換えはしない §3-5。プロパティ更新は データ品質 のみ）
- statements で ③ をバックフィル (personal-only §2.1。公開導線に流さない)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

from ..collectors import jquants, yfinance_prices
from ..convert import convert_artifact
from ..licensing import source_license
from ..models import (
    FinancialSummaryRecord,
    PriceTechnicalRecord,
    Provenance,
    Source,
    now_jst,
)
from ..notion import file_upload, upsert
from ..notion import schema as S
from ..notion.client import NotionClient
from ..transform import reconcile
from ..transform.normalize import PERIOD_TYPE_TO_DISCLOSURE, parse_numeric
from .runner import JobContext, apply_limit, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "jquants_weekly"


def default_target_date(today: date | None = None) -> date:
    """12週前から直前の平日へ繰り下げ (週末には市場データが無い)。"""
    target = (today or now_jst().date()) - timedelta(weeks=12)
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target


# ---------------------------------------------------------------------------
# ② ページ → (page_id, code, close) 抽出
# ---------------------------------------------------------------------------


def _prop_number(page: dict, name: str) -> float | None:
    return page.get("properties", {}).get(name, {}).get("number")


def _prop_title(page: dict, name: str) -> str:
    items = page.get("properties", {}).get(name, {}).get("title", [])
    return items[0].get("plain_text", "").strip() if items else ""


def load_price_snapshot(client: NotionClient, settings) -> list[tuple[str, str, float]]:
    """② の全行から (page_id, 銘柄コード, 終値) を取り出す。終値欠損行は除外。"""
    pages = client.query_database(settings.db_id("prices"))
    out: list[tuple[str, str, float]] = []
    for page in pages:
        code = _prop_title(page, S.PRICE_PROP_CODE)
        close = _prop_number(page, S.PRICE_PROP_CLOSE)
        if code and close is not None:
            out.append((page["id"], code, float(close)))
    return out


# ---------------------------------------------------------------------------
# statements 行 → ③ レコード (J-Quants /fins/statements の列名)
# ---------------------------------------------------------------------------

_STATEMENT_FIELDS: dict[str, str] = {
    "net_sales": "NetSales",
    "operating_income": "OperatingProfit",
    "ordinary_income": "OrdinaryProfit",
    "net_income": "Profit",
    "eps": "EarningsPerShare",
    "bps": "BookValuePerShare",
    "equity_ratio_pct": "EquityToAssetRatio",
    "cf_operating": "CashFlowsFromOperatingActivities",
    "cf_investing": "CashFlowsFromInvestingActivities",
    "cf_financing": "CashFlowsFromFinancingActivities",
    "dps_actual": "ResultDividendPerShareAnnual",
    "dps_forecast": "ForecastDividendPerShareAnnual",
    "forecast_net_sales": "ForecastNetSales",
    "forecast_operating_income": "ForecastOperatingProfit",
    "forecast_ordinary_income": "ForecastOrdinaryProfit",
    "forecast_net_income": "ForecastProfit",
    "forecast_eps": "ForecastEarningsPerShare",
}


def statement_to_record(row: dict, raw_page_id: str | None) -> FinancialSummaryRecord | None:
    """statements の1行を ③ レコードへ変換 (値の数値化のみ。欠損は None §3-1)。"""
    code = reconcile._normalize_jq_code(row.get("LocalCode") or "")
    period_end_text = str(row.get("CurrentPeriodEndDate") or "")
    if not code or not period_end_text:
        return None
    try:
        period_end = datetime.strptime(period_end_text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    period_type = str(row.get("TypeOfCurrentPeriod") or "")
    disclosure_type = PERIOD_TYPE_TO_DISCLOSURE.get(period_type)
    if disclosure_type is None:
        return None

    disclosed_text = str(row.get("DisclosedDate") or "")
    disclosed_at = None
    if disclosed_text:
        try:
            disclosed_at = datetime.strptime(disclosed_text[:10], "%Y-%m-%d")
        except ValueError:
            disclosed_at = None

    values = {
        field: parse_numeric(row.get(col)) for field, col in _STATEMENT_FIELDS.items()
    }
    # EquityToAssetRatio は小数表記 → % (確定的な単位変換 §3-4)
    ratio = values.get("equity_ratio_pct")
    if ratio is not None and abs(ratio) <= 1.0:
        values["equity_ratio_pct"] = ratio * 100.0

    doc_type = str(row.get("TypeOfDocument") or "")
    consolidated = None
    if "_Consolidated_" in doc_type or doc_type.endswith("Consolidated"):
        consolidated = "連結"
    elif "NonConsolidated" in doc_type:
        consolidated = "単体"

    return FinancialSummaryRecord(
        code=code,
        fiscal_period_end=period_end,
        disclosure_type=disclosure_type,
        provenance=Provenance(
            source=Source.JQUANTS,
            license_tag=source_license(Source.JQUANTS),  # personal-only 厳守 (§2.1)
            data_date=period_end,
            fetched_at=now_jst(),
            raw_page_id=raw_page_id,
        ),
        consolidated=consolidated,
        disclosed_at=disclosed_at,
        **values,
    )


def _same_date_records(
    ctx: JobContext, codes: list[str], target: date
) -> list["PriceTechnicalRecord"]:
    """突合の自側値: 対象日(12週前)の終値を実データで再取得して組む (§3-5)。

    ② は「最新スナップショット」しか持たないため、対象日と同一基準日の値は
    価格履歴の実取得で得る。未調整終値同士 (yfinance auto_adjust=False の Close
    vs J-Quants の Close) を比較する。対象日の行が無い銘柄は比較しない
    （実在しない日付×値の組を作らない §3-3）。
    """
    if not codes:
        return []
    hist_artifact, frames, missing = yfinance_prices.fetch_daily_batch(
        ctx.settings, codes, period="6mo"
    )
    file_upload.upload_raw_artifact(ctx.client, ctx.settings, hist_artifact)
    if missing:
        logger.info("突合用履歴を取得できない銘柄 %d 件は比較対象外 (§3-1)", len(missing))
    records: list[PriceTechnicalRecord] = []
    for code, df in frames.items():
        cols = {str(c).lower(): c for c in df.columns}
        date_col, close_col = cols.get("date"), cols.get("close")
        if date_col is None or close_col is None:
            continue
        rows = df[pd.to_datetime(df[date_col]).dt.date == target]
        if rows.empty or pd.isna(rows.iloc[-1][close_col]):
            continue
        records.append(
            PriceTechnicalRecord(
                code=code,
                close=float(rows.iloc[-1][close_col]),
                provenance=Provenance(
                    source=Source.YFINANCE,
                    license_tag=source_license(Source.YFINANCE),
                    data_date=target,
                    fetched_at=now_jst(),
                    raw_page_id=hist_artifact.notion_page_id,
                ),
            )
        )
    return records


def execute(ctx: JobContext) -> None:
    target = ctx.args.date or default_target_date()
    client_jq = jquants.JQuantsClient(ctx.settings)

    # --- 突合検証 (§3-5) ---
    artifact, jq_df = jquants.daily_quotes(ctx.settings, target_date=target, client=client_jq)
    convert_artifact(artifact, "jsonl")
    file_upload.upload_raw_artifact(ctx.client, ctx.settings, artifact)

    snapshot = load_price_snapshot(ctx.client, ctx.settings)
    page_by_code = {code: page_id for page_id, code, _close in snapshot}
    codes = apply_limit(sorted(page_by_code), ctx.args.limit)
    records = _same_date_records(ctx, codes, target)
    # 未調整終値同士の同一日比較 (調整済 AdjustmentClose とは比較しない)
    discrepancies = reconcile.reconcile_prices(jq_df, records, close_col="Close")
    logger.info("突合: %d 銘柄比較, 乖離 %d 件 (閾値 %.1f%%)",
                len(records), len(discrepancies), reconcile.DEFAULT_THRESHOLD_PCT)
    for d in discrepancies:
        # 値は書き換えず データ品質=要確認 のみ更新 (§3-5)
        page_id = page_by_code.get(d.code)
        if not page_id:
            continue
        try:
            ctx.client.update_page(
                page_id, {S.PROP_QUALITY: {"select": {"name": "要確認"}}}
            )
            logger.warning(
                "要確認: %s ours=%.1f theirs=%.1f (%.2f%%)",
                d.code, d.ours, d.theirs, d.deviation_pct,
            )
        except Exception as exc:
            ctx.add_failure(d.code, f"要確認フラグ付与失敗: {exc}")
    ctx.add_success(len(records))

    # --- 財務バックフィル (③, personal-only) ---
    if getattr(ctx.args, "skip_statements", False):
        return
    st_artifact, st_df = jquants.statements(
        ctx.settings, target_date=target, client=client_jq
    )
    convert_artifact(st_artifact, "jsonl")
    st_page_id = file_upload.upload_raw_artifact(ctx.client, ctx.settings, st_artifact)
    rows = apply_limit(st_df.to_dict("records"), ctx.args.limit)
    for row in rows:
        rec = statement_to_record(row, st_page_id)
        if rec is None:
            continue
        try:
            master_id = upsert.find_stock_master_page(ctx.client, ctx.settings, rec.code)
            upsert.upsert_financial_summary(ctx.client, ctx.settings, rec, master_id)
            ctx.add_success()
        except Exception as exc:
            ctx.add_failure(rec.code, f"③バックフィル失敗: {exc}")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("J-Quants確定値の突合検証+③バックフィル (週1土曜)")
    parser.add_argument("--skip-statements", action="store_true", help="③バックフィルを省略")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
