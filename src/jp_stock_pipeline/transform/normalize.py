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
    # EDINET の有報/四半期報告は「主要な経営指標等の推移」の要素に
    # `...SummaryOfBusinessResults` 接尾辞が付く。これを候補に入れていなかったため
    # eps / bps / equity_ratio_pct / dps が EDINET 経由で**1件も取れていなかった**
    # （実データ 60 書類で 0%。2026-09-12 実測）。
    # 接尾辞つきは Current/Prior1〜4 の5年度ぶんが並ぶが、`_pick_value` が
    # `_is_current` で当期だけに絞り、`consolidated` 列で連結を優先するので
    # 年度・連結単体の取り違えは起きない（コンテキストを実測して確認済み）。
    "eps": (
        "NetIncomePerShare",
        "BasicEarningsPerShareIFRS",
        "BasicEarningsLossPerShare",
        "BasicEarningsPerShare",
        "BasicNetIncomePerShare",
        "BasicEarningsLossPerShareSummaryOfBusinessResults",
        "BasicEarningsLossPerShareIFRSSummaryOfBusinessResults",
        "BasicEarningsLossPerShareIFRS",
    ),
    "bps": (
        "NetAssetsPerShare",
        "BookValuePerShare",
        "EquityAttributableToOwnersOfParentPerShareIFRS",
        "NetAssetsPerShareSummaryOfBusinessResults",
        "EquityAttributableToOwnersOfParentPerShareIFRSSummaryOfBusinessResults",
    ),
    "equity_ratio_pct": (
        "CapitalAdequacyRatio",
        "EquityToAssetRatio",
        "RatioOfOwnersEquityToTotalAssets",
        "EquityAttributableToOwnersOfParentToTotalAssetsRatioIFRS",
        "EquityToAssetRatioSummaryOfBusinessResults",
        "EquityToAssetRatioIFRSSummaryOfBusinessResults",
    ),
    "cf_operating": (
        "CashFlowsFromOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivities",
        "CashFlowsFromUsedInOperatingActivitiesIFRS",
    ),
    "cf_investing": (
        "CashFlowsFromInvestingActivities",
        # EDINET タクソノミの実名は Investing ではなく **Investment**。
        # 営業CF・財務CF が 78% 取れているのに投資CF だけ 0% だった原因
        # （2026-09-12 に実データ 60 書類で実測）。
        "NetCashProvidedByUsedInInvestmentActivities",
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesSummaryOfBusinessResults",
        "CashFlowsFromUsedInInvestingActivitiesIFRS",
        "CashFlowsFromUsedInInvestingActivitiesIFRSSummaryOfBusinessResults",
    ),
    "cf_financing": (
        "CashFlowsFromFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivities",
        "CashFlowsFromUsedInFinancingActivitiesIFRS",
    ),
    "dps": (
        "DividendPerShare",
        "DistributionsPerUnit",
        "DividendPaidPerShareSummaryOfBusinessResults",
    ),
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
# TypeOfCurrentPeriodDEI (Q1/Q2/Q3/HY/FY) の値から導出。
# HY も Q2 と同じ「2Q」に寄せ、「中間」への読み替えは決算期末で行う
# (interim_disclosure_type。DEI では半期報告書かどうかを判定できない)。
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

# 半期報告制度 (2024-04-01 施行) で「2Q」と「中間」を切り分ける。
#
# 2024 年 4 月の金商法改正で四半期報告書 (docTypeCode 140/150) が廃止され、
# 上場会社も半期報告書 (160/170) を出すようになった。経過措置は四半期会計期間の
# 単位で、2024-04-01 より前に始まった四半期会計期間までは四半期報告書のまま。
# 第2四半期は期末の 3 か月前の翌日に始まるので、「開始日が 2024-04-01 以後」と
# 「期末が 2024-06-30 以後」は同じ意味になる（20 日締めでも 2024-06-20 期末は
# 2024-03-21 開始で四半期報告書、2024-07-20 期末は 2024-04-21 開始で半期報告書）。
#
# DEI の値 (HY / Q2) では切り分けない。ローカルの EDINET 書類一覧 258,351 件と
# 変換済み CSV を docID で突き合わせたところ (2026-09-13)、2024 年以後の
# 半期報告書 (160) の DEI は Q2 が 4,659 件、HY が 3,052 件に割れていた。
# 2024 年より前の四半期報告書 (140) にも HY が 92 件あった（銀行などの特定事業会社）。
# DEI で分けると、同じ期の EDINET 行と TDnet 行が「2Q」と「中間」に割れて
# 二重計上になる。
#
# 期末で決めれば、Notion ③ の既存行の移行 (2026-09 実施済み) にも同じ規則が
# 使える。既存行には書類種別が残っていないので、書類種別で決める規則は移行できない。
# 規則が同じなら、移行した行と後から同じ期を取り直した書き込みが必ず同じキーに
# 着地する。
DISCLOSURE_TYPE_SECOND_QUARTER = "2Q"
DISCLOSURE_TYPE_INTERIM = "中間"
INTERIM_FIRST_PERIOD_END = date(2024, 6, 30)


def interim_disclosure_type(disclosure_type: str, fiscal_period_end: date) -> str:
    """第2四半期のうち半期報告制度の期を「中間」へ読み替える。それ以外はそのまま返す。"""
    if (
        disclosure_type == DISCLOSURE_TYPE_SECOND_QUARTER
        and fiscal_period_end >= INTERIM_FIRST_PERIOD_END
    ):
        return DISCLOSURE_TYPE_INTERIM
    return disclosure_type

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
    dtype = interim_disclosure_type(
        disclosure_type or derive_disclosure_type(tidy) or "本決算", fiscal_period_end
    )

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


# yfinance info → バリュエーションは L-13 で削除した（prices_daily 廃止で
# 呼び出し元なし。日足断面は kabulab-cf の daily.ts が D1 へ書く）。
