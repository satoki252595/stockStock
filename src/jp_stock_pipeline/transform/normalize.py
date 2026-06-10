"""XBRL tidy 形式 → FinancialSummaryRecord / yfinance info → バリュエーションの正規化。

入力 tidy 形式は docs/CONTRACTS.md の列定義
(code, doc_id, element, context_ref, period_start, period_end, instant_date,
 consolidated, unit, value[文字列]) に従う。

不変条件 (§3-1): 値の数値化に失敗した項目・存在しない項目は None のまま。
推定・補間はしない。

実績/予想の判別は決算短信サマリ XBRL (tse-ed-t) の contextRef 規則で行う:
- contextRef に "ForecastMember" を含む → 会社予想
- "NextYear...ForecastMember" → 来期予想 (forecast_* フィールド)
- それ以外の CurrentYear/CurrentQuarter/CurrentAccumulated 系 → 当期実績
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import pandas as pd

from ..models import FinancialSummaryRecord, Provenance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 要素マッピング (element のローカル名サフィックスで照合。先頭ほど優先)
# jppfs_cor = EDINET 財務諸表本表 / tse-ed-t = 短信サマリ / jpcrp = 有報表紙等
# ---------------------------------------------------------------------------

ELEMENT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "net_sales": (
        "NetSales",
        "OperatingRevenues",
        "OperatingRevenue",
        "Revenue",
        "RevenuesIFRS",
        "RevenueIFRS",
        "NetSalesIFRS",
        "SalesIFRS",
    ),
    "operating_income": (
        "OperatingIncome",
        "OperatingProfit",
        "OperatingIncomeIFRS",
        "OperatingProfitIFRS",
        "OperatingProfitLossIFRS",
    ),
    "ordinary_income": (
        "OrdinaryIncome",
        "OrdinaryProfit",
        "OrdinaryProfitLoss",
        "ProfitBeforeTaxIFRS",
    ),
    "net_income": (
        "ProfitAttributableToOwnersOfParent",
        "ProfitAttributableToOwnersOfParentIFRS",
        "NetIncome",
        "ProfitLossAttributableToOwnersOfParent",
        "ProfitLoss",
    ),
    "eps": (
        "NetIncomePerShare",
        "BasicEarningsPerShareIFRS",
        "BasicEarningsLossPerShare",
        "BasicEarningsPerShare",
        "BasicNetIncomePerShare",
    ),
    "bps": (
        "NetAssetsPerShare",
        "BookValuePerShare",
        "EquityAttributableToOwnersOfParentPerShareIFRS",
    ),
    "equity_ratio_pct": (
        "CapitalAdequacyRatio",
        "EquityToAssetRatio",
        "RatioOfOwnersEquityToTotalAssets",
        "EquityAttributableToOwnersOfParentToTotalAssetsRatioIFRS",
    ),
    "cf_operating": (
        "CashFlowsFromOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivities",
        "CashFlowsFromUsedInOperatingActivitiesIFRS",
    ),
    "cf_investing": (
        "CashFlowsFromInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivities",
        "CashFlowsFromUsedInInvestingActivitiesIFRS",
    ),
    "cf_financing": (
        "CashFlowsFromFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivities",
        "CashFlowsFromUsedInFinancingActivitiesIFRS",
    ),
    "dps": ("DividendPerShare", "DistributionsPerUnit"),
}

# 比率系要素は XBRL 上は小数 (例 0.582 = 58.2%)。% への換算は単位の確定的変換
# であり推定ではない (§3-4。docstring とカタログに明示)
_RATIO_FIELDS = frozenset({"equity_ratio_pct"})

# 来期予想として forecast_* フィールドへ写すもの
_FORECAST_FIELDS: dict[str, str] = {
    "net_sales": "forecast_net_sales",
    "operating_income": "forecast_operating_income",
    "ordinary_income": "forecast_ordinary_income",
    "net_income": "forecast_net_income",
    "eps": "forecast_eps",
}

# 開示種別 (③ select) — tse-ed-t TypeOfCurrentPeriodDETAIL / jpdei
# TypeOfCurrentPeriodDEI (Q1/Q2/Q3/HY/FY) の値から導出
PERIOD_TYPE_TO_DISCLOSURE: dict[str, str] = {
    "FY": "本決算",
    "1Q": "1Q",
    "2Q": "2Q",
    "HY": "2Q",
    "3Q": "3Q",
    "Q1": "1Q",
    "Q2": "2Q",
    "Q3": "3Q",
}

# 会計基準 (AccountingStandardsDEI 等の値 → ③ select。schema.py の選択肢と一致させる)
ACCOUNTING_STANDARD_MAP: dict[str, str] = {
    "Japan GAAP": "日本基準",
    "JapanGAAP": "日本基準",
    "IFRS": "IFRS",
    "US GAAP": "US-GAAP",
    "USGAAP": "US-GAAP",
}


def parse_numeric(value: str | None) -> float | None:
    """tidy の文字列値を数値化。失敗は None (§3-1。推定しない)。"""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "－", "―"):
        return None
    # XBRL の三角表記 (△=負値) に対応
    negative = text.startswith(("△", "▲", "(")) and not text.startswith("(注")
    text = text.strip("△▲()")
    try:
        num = float(text)
    except ValueError:
        return None
    return -num if negative else num


def _local_name(element: str) -> str:
    return element.rsplit(":", 1)[-1]


def _is_forecast(context_ref: str) -> bool:
    return "Forecast" in context_ref


def _is_next_year(context_ref: str) -> bool:
    return "NextYear" in context_ref or "NextAccumulated" in context_ref


def _is_current(context_ref: str) -> bool:
    return context_ref.startswith("Current") or "CurrentYear" in context_ref


def _pick_value(
    df: pd.DataFrame, candidates: tuple[str, ...], *, forecast: bool, next_year: bool = False
) -> float | None:
    """候補要素から優先順に値を選ぶ。連結優先・当期コンテキスト優先 (§4)。"""
    local = df["element"].map(_local_name)
    for cand in candidates:
        rows = df[local == cand]
        if rows.empty:
            continue
        mask_fc = rows["context_ref"].map(_is_forecast)
        mask_ny = rows["context_ref"].map(_is_next_year)
        if forecast:
            rows = rows[mask_fc & (mask_ny if next_year else ~mask_ny)]
        else:
            rows = rows[~mask_fc & rows["context_ref"].map(_is_current)]
        if rows.empty:
            continue
        # 連結優先 ("連結" > "" > "単体")
        for consolidated in ("連結", "", "単体"):
            sel = rows[rows["consolidated"] == consolidated]
            for _, row in sel.iterrows():
                num = parse_numeric(row["value"])
                if num is not None:
                    return num
    return None


def _pick_text(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    local = df["element"].map(_local_name)
    for cand in candidates:
        rows = df[local == cand]
        for _, row in rows.iterrows():
            val = str(row["value"]).strip()
            if val:
                return val
    return None


def _pick_date(df: pd.DataFrame, candidates: tuple[str, ...]) -> date | None:
    text = _pick_text(df, candidates)
    if not text:
        return None
    try:
        return datetime.strptime(text.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def derive_fiscal_period_end(tidy: pd.DataFrame) -> date | None:
    """決算期末の導出: DEI 要素 → 無ければ当期 Duration コンテキストの最大 period_end。"""
    explicit = _pick_date(
        tidy,
        (
            "CurrentPeriodEndDateDEI",
            "CurrentFiscalYearEndDateDEI",
            "FiscalYearEnd",
            "FiscalYearEndDEI",
        ),
    )
    if explicit:
        return explicit
    current = tidy[
        tidy["context_ref"].map(lambda c: "CurrentYear" in c and not _is_forecast(c))
    ]
    ends = pd.to_datetime(current["period_end"], errors="coerce").dropna()
    if ends.empty:
        return None
    return ends.max().date()


def derive_disclosure_type(tidy: pd.DataFrame) -> str | None:
    """開示種別の導出: tse-ed-t TypeOfCurrentPeriodDETAIL (FY/1Q/...) /
    jpdei TypeOfCurrentPeriodDEI (Q1/Q2/Q3/HY/FY) / QuarterlyPeriodDEI (1/2/3)。"""
    text = _pick_text(
        tidy,
        (
            "TypeOfCurrentPeriodDETAIL",
            "TypeOfCurrentPeriodDEI",
            "QuarterlyPeriodDEI",
            "QuarterlyPeriod",
            "TypeOfCurrentPeriod",
        ),
    )
    if text is None:
        return None
    text = text.strip()
    if text in PERIOD_TYPE_TO_DISCLOSURE:
        return PERIOD_TYPE_TO_DISCLOSURE[text]
    if text in ("1", "2", "3"):  # jpdei QuarterlyPeriodDEI は四半期番号
        return f"{text}Q"
    return None


def derive_accounting_standard(tidy: pd.DataFrame) -> str | None:
    text = _pick_text(tidy, ("AccountingStandardsDEI", "AccountingStandards"))
    if text is None:
        return None
    return ACCOUNTING_STANDARD_MAP.get(text.strip(), text.strip())


def tidy_to_financial_record(
    tidy: pd.DataFrame,
    code: str,
    provenance: Provenance,
    *,
    disclosure_type: str | None = None,
    disclosed_at: datetime | None = None,
) -> FinancialSummaryRecord | None:
    """tidy DataFrame から ③ 財務サマリの1レコードを構築する。

    決算期末が導出できない場合は None を返す（複合キーが成立しないため。
    値を推定して埋めることはしない §3-1）。
    disclosure_type 引数は呼び出し側が開示種別を確定できる場合の上書き
    （例: ④ の classify 結果が「修正」）。
    """
    if tidy.empty:
        return None
    fiscal_period_end = derive_fiscal_period_end(tidy)
    if fiscal_period_end is None:
        logger.warning("決算期末を導出できないため③レコードを生成しない (code=%s)", code)
        return None
    dtype = disclosure_type or derive_disclosure_type(tidy) or "本決算"

    values: dict[str, float | None] = {}
    for field, candidates in ELEMENT_CANDIDATES.items():
        if field == "dps":
            continue
        num = _pick_value(tidy, candidates, forecast=False)
        if num is not None and field in _RATIO_FIELDS and abs(num) <= 1.0:
            num *= 100.0  # 小数表記の比率 → % (確定的な単位変換 §3-4)
        values[field] = num

    # 来期予想 (ForecastMember + NextYear)。当期予想しか無い短信では None のまま
    for src_field, dst_field in _FORECAST_FIELDS.items():
        values[dst_field] = _pick_value(
            tidy, ELEMENT_CANDIDATES[src_field], forecast=True, next_year=True
        ) or _pick_value(tidy, ELEMENT_CANDIDATES[src_field], forecast=True)

    dps_actual = _pick_value(tidy, ELEMENT_CANDIDATES["dps"], forecast=False)
    dps_forecast = _pick_value(tidy, ELEMENT_CANDIDATES["dps"], forecast=True)

    # 連結/単体: 実績値が連結コンテキストから取れたか
    consolidated = None
    if not tidy.empty:
        cons_values = set(tidy["consolidated"].unique())
        if "連結" in cons_values:
            consolidated = "連結"
        elif cons_values == {"単体"}:
            consolidated = "単体"

    return FinancialSummaryRecord(
        code=code,
        fiscal_period_end=fiscal_period_end,
        disclosure_type=dtype,
        provenance=provenance,
        consolidated=consolidated,
        accounting_standard=derive_accounting_standard(tidy),
        net_sales=values["net_sales"],
        operating_income=values["operating_income"],
        ordinary_income=values["ordinary_income"],
        net_income=values["net_income"],
        eps=values["eps"],
        bps=values["bps"],
        roe_pct=None,  # 短信サマリに直接出る場合のみ将来対応。計算で補わない (§3-1)
        roa_pct=None,
        equity_ratio_pct=values["equity_ratio_pct"],
        cf_operating=values["cf_operating"],
        cf_investing=values["cf_investing"],
        cf_financing=values["cf_financing"],
        dps_actual=dps_actual,
        dps_forecast=dps_forecast,
        forecast_net_sales=values["forecast_net_sales"],
        forecast_operating_income=values["forecast_operating_income"],
        forecast_ordinary_income=values["forecast_ordinary_income"],
        forecast_net_income=values["forecast_net_income"],
        forecast_eps=values["forecast_eps"],
        disclosed_at=disclosed_at,
    )


# ---------------------------------------------------------------------------
# yfinance info → バリュエーション (§4 トラックB, personal-only)
# ---------------------------------------------------------------------------


def yf_info_to_valuation(info: dict) -> dict:
    """yfinance の info dict から PER/PBR/時価総額/配当利回り% を取り出す。

    キーが無い項目は None のまま（推定しない §3-1）。
    dividendYield は yfinance 1.x 系では % 単位で返る
    （実フィクスチャ tests/fixtures/transform/yfinance_7203T_info.json で
    3.53 = 3.53% を確認済み）。換算はしない。
    """
    return {
        "per": info.get("trailingPE"),
        "pbr": info.get("priceToBook"),
        "market_cap": info.get("marketCap"),
        "dividend_yield_pct": info.get("dividendYield"),
    }
