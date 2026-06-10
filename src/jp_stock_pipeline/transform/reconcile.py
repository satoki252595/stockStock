"""J-Quants 確定値との突合検証 (DESIGN.md §3-5)。

- 乖離閾値（既定: 終値±1%）を超えた行に「要確認」フラグを付与する
- **値の自動書き換えは絶対にしない**
- より信頼できるソース（調整済確定値）で更新する場合はソース名を更新する
  （adopt_confirmed_value。呼ぶかどうかはジョブ側の明示的な判断）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from ..models import DataQuality, PriceTechnicalRecord, Source

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_PCT = 1.0


@dataclass
class Discrepancy:
    """② の値と J-Quants 確定値の乖離。"""

    code: str
    ours: float  # ② に入っている終値
    theirs: float  # J-Quants 調整済終値
    deviation_pct: float


def _normalize_jq_code(raw: str) -> str:
    """J-Quants の銘柄コード (5桁 例 '72030') → 4桁基準へ正規化。"""
    text = str(raw).strip()
    if len(text) == 5:
        return text[:4]
    return text


def reconcile_prices(
    jq_df: pd.DataFrame,
    current_records: list[PriceTechnicalRecord],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    code_col: str = "Code",
    close_col: str = "AdjustmentClose",
) -> list[Discrepancy]:
    """J-Quants daily_quotes と ② の終値を銘柄毎に突合する (§3-5)。

    threshold_pct を超える乖離のみ Discrepancy として返す。
    どちらかが欠損の銘柄は比較しない（欠損は欠損 §3-1。乖離扱いもしない）。
    """
    if jq_df.empty:
        return []
    theirs_by_code: dict[str, float] = {}
    for _, row in jq_df.iterrows():
        close = row.get(close_col)
        if close is None or pd.isna(close):
            continue
        theirs_by_code[_normalize_jq_code(row[code_col])] = float(close)

    discrepancies: list[Discrepancy] = []
    for record in current_records:
        theirs = theirs_by_code.get(record.code)
        if theirs is None or theirs == 0 or record.close is None:
            continue
        deviation = abs(record.close - theirs) / theirs * 100.0
        if deviation > threshold_pct:
            discrepancies.append(
                Discrepancy(
                    code=record.code,
                    ours=record.close,
                    theirs=theirs,
                    deviation_pct=deviation,
                )
            )
    return discrepancies


def apply_flags(
    records: list[PriceTechnicalRecord], discrepancies: list[Discrepancy]
) -> list[PriceTechnicalRecord]:
    """乖離銘柄の provenance.quality を「要確認」に設定する (§3-5)。

    値（close 等）は一切書き換えない。フラグ付与された record のリストを返す。
    """
    flagged_codes = {d.code for d in discrepancies}
    flagged: list[PriceTechnicalRecord] = []
    for record in records:
        if record.code in flagged_codes:
            record.provenance.quality = DataQuality.NEEDS_REVIEW
            flagged.append(record)
    return flagged


def adopt_confirmed_value(
    record: PriceTechnicalRecord, confirmed_close: float
) -> PriceTechnicalRecord:
    """確定値（J-Quants 調整済終値）の明示的な採用 (§3-5 の更新規則)。

    自動では呼ばれない。ジョブ側が「より信頼できるソースで更新する」と
    判断した場合のみ使い、ソース名を J-Quants に更新し品質を正常へ戻す。
    """
    record.close = confirmed_close
    record.provenance.source = Source.JQUANTS
    record.provenance.quality = DataQuality.OK
    return record
