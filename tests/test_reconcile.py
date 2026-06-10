"""突合検証 (§3-5) のテスト: 閾値判定・要確認フラグ・値を書き換えないこと。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import (
    DataQuality,
    PriceTechnicalRecord,
    Provenance,
    Source,
    now_jst,
)
from jp_stock_pipeline.transform.reconcile import (
    adopt_confirmed_value,
    apply_flags,
    reconcile_prices,
)


def record(code: str, close: float | None) -> PriceTechnicalRecord:
    return PriceTechnicalRecord(
        code=code,
        close=close,
        provenance=Provenance(
            source=Source.YFINANCE,
            license_tag=LicenseTag.PERSONAL_ONLY,
            data_date=date(2026, 6, 10),
            fetched_at=now_jst(),
        ),
    )


def jq_frame(rows: list[tuple[str, float | None]]) -> pd.DataFrame:
    return pd.DataFrame([{"Code": c, "AdjustmentClose": v} for c, v in rows])


class TestReconcile:
    def test_deviation_over_threshold_detected(self):
        records = [record("7203", 3000.0)]
        jq = jq_frame([("72030", 3100.0)])  # 約3.2%乖離
        result = reconcile_prices(jq, records, threshold_pct=1.0)
        assert len(result) == 1
        d = result[0]
        assert d.code == "7203"
        assert d.ours == 3000.0
        assert d.theirs == 3100.0
        assert d.deviation_pct > 3.0

    def test_within_threshold_not_flagged(self):
        records = [record("7203", 3099.0)]
        jq = jq_frame([("72030", 3100.0)])  # 0.03%
        assert reconcile_prices(jq, records, threshold_pct=1.0) == []

    def test_missing_value_not_compared(self):
        """欠損は欠損のまま (§3-1)。乖離扱いもしない。"""
        records = [record("7203", None), record("9999", 100.0)]
        jq = jq_frame([("72030", 3100.0), ("99990", None)])
        assert reconcile_prices(jq, records, threshold_pct=1.0) == []

    def test_threshold_configurable(self):
        records = [record("7203", 3050.0)]
        jq = jq_frame([("72030", 3100.0)])  # 約1.6%
        assert len(reconcile_prices(jq, records, threshold_pct=1.0)) == 1
        assert reconcile_prices(jq, records, threshold_pct=2.0) == []

    def test_empty_jq(self):
        assert reconcile_prices(pd.DataFrame(), [record("7203", 100.0)]) == []


class TestApplyFlags:
    def test_flag_set_and_values_untouched(self):
        records = [record("7203", 3000.0), record("6758", 2000.0)]
        jq = jq_frame([("72030", 3100.0), ("67580", 2001.0)])
        discrepancies = reconcile_prices(jq, records, threshold_pct=1.0)
        flagged = apply_flags(records, discrepancies)

        assert [r.code for r in flagged] == ["7203"]
        assert records[0].provenance.quality is DataQuality.NEEDS_REVIEW
        assert records[0].close == 3000.0  # 値は書き換えない (§3-5)
        assert records[1].provenance.quality is DataQuality.OK
        assert records[1].close == 2000.0


class TestAdoptConfirmedValue:
    def test_explicit_adoption_updates_source(self):
        """明示的採用時のみ値が変わり、ソース名が更新される (§3-5)。"""
        rec = record("7203", 3000.0)
        rec.provenance.quality = DataQuality.NEEDS_REVIEW
        adopt_confirmed_value(rec, 3100.0)
        assert rec.close == 3100.0
        assert rec.provenance.source is Source.JQUANTS
        assert rec.provenance.quality is DataQuality.OK
