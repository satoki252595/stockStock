"""normalize（tidy→③レコード / yfinance info→バリュエーション）のテスト。

tidy DataFrame は実XBRL由来フィクスチャが未取得（EDINET APIキー必須）のため、
CONTRACTS.md の列スキーマに準拠した最小構造データで**選択ロジックのみ**を検証
する（構造テストであり、市場データの捏造ではない。実XBRLでの統合検証は
フィクスチャ取得後に tests/test_xbrl_to_csv.py 経由で行われる）。
yfinance info は実フィクスチャを使用する。
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import Provenance, Source, now_jst
from jp_stock_pipeline.transform.normalize import (
    derive_disclosure_type,
    derive_fiscal_period_end,
    parse_numeric,
    tidy_to_financial_record,
    yf_info_to_valuation,
)

from conftest import fixture_path

TIDY_COLUMNS = [
    "code", "doc_id", "element", "context_ref", "period_start",
    "period_end", "instant_date", "consolidated", "unit", "value",
]


def tidy_frame(rows: list[dict]) -> pd.DataFrame:
    base = {c: "" for c in TIDY_COLUMNS}
    return pd.DataFrame([{**base, **r} for r in rows], columns=TIDY_COLUMNS)


def prov() -> Provenance:
    return Provenance(
        source=Source.TDNET,
        license_tag=LicenseTag.FACTUAL_CITE,
        data_date=date(2026, 3, 31),
        fetched_at=now_jst(),
    )


class TestParseNumeric:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1234", 1234.0),
            ("1,234,567", 1234567.0),
            ("-15.5", -15.5),
            ("△123", -123.0),
            ("0.582", 0.582),
            ("", None),
            ("-", None),
            ("非数値", None),
            (None, None),
        ],
    )
    def test_cases(self, text, expected):
        assert parse_numeric(text) == expected


class TestSelectionLogic:
    def test_actual_vs_forecast_contexts(self):
        tidy = tidy_frame([
            {"element": "tse-ed-t:NetSales", "context_ref": "CurrentYearDuration_ConsolidatedMember_ResultMember", "consolidated": "連結", "value": "1000", "period_end": "2026-03-31"},
            {"element": "tse-ed-t:NetSales", "context_ref": "NextYearDuration_ConsolidatedMember_ForecastMember", "consolidated": "連結", "value": "1100"},
            {"element": "jpdei_cor:CurrentPeriodEndDateDEI", "context_ref": "FilingDateInstant", "value": "2026-03-31"},
        ])
        record = tidy_to_financial_record(tidy, "7203", prov())
        assert record is not None
        assert record.net_sales == 1000.0  # 実績は Forecast コンテキストを拾わない
        assert record.forecast_net_sales == 1100.0  # 来期予想

    def test_consolidated_preferred_over_parent(self):
        tidy = tidy_frame([
            {"element": "tse-ed-t:NetSales", "context_ref": "CurrentYearDuration", "consolidated": "単体", "value": "500", "period_end": "2026-03-31"},
            {"element": "tse-ed-t:NetSales", "context_ref": "CurrentYearDuration", "consolidated": "連結", "value": "800", "period_end": "2026-03-31"},
        ])
        record = tidy_to_financial_record(tidy, "7203", prov())
        assert record.net_sales == 800.0
        assert record.consolidated == "連結"

    def test_ratio_decimal_converted_to_pct(self):
        tidy = tidy_frame([
            {"element": "tse-ed-t:CapitalAdequacyRatio", "context_ref": "CurrentYearInstant", "consolidated": "連結", "value": "0.582", "instant_date": "2026-03-31"},
            {"element": "jpdei_cor:CurrentPeriodEndDateDEI", "context_ref": "FilingDateInstant", "value": "2026-03-31"},
        ])
        record = tidy_to_financial_record(tidy, "7203", prov())
        assert record.equity_ratio_pct == pytest.approx(58.2)

    def test_unparseable_value_stays_none(self):
        tidy = tidy_frame([
            {"element": "tse-ed-t:OperatingIncome", "context_ref": "CurrentYearDuration", "consolidated": "連結", "value": "未定", "period_end": "2026-03-31"},
            {"element": "jpdei_cor:CurrentPeriodEndDateDEI", "context_ref": "FilingDateInstant", "value": "2026-03-31"},
        ])
        record = tidy_to_financial_record(tidy, "7203", prov())
        assert record.operating_income is None  # 推定しない §3-1

    def test_no_period_end_returns_none(self):
        tidy = tidy_frame([
            {"element": "tse-ed-t:NetSales", "context_ref": "SomeContext", "value": "1000"},
        ])
        assert tidy_to_financial_record(tidy, "7203", prov()) is None

    def test_empty_returns_none(self):
        assert tidy_to_financial_record(tidy_frame([]), "7203", prov()) is None


class TestDerivations:
    def test_period_end_from_dei(self):
        tidy = tidy_frame([
            {"element": "jpdei_cor:CurrentPeriodEndDateDEI", "context_ref": "FilingDateInstant", "value": "2026-03-31"},
        ])
        assert derive_fiscal_period_end(tidy) == date(2026, 3, 31)

    def test_period_end_fallback_to_context_max(self):
        tidy = tidy_frame([
            {"element": "tse-ed-t:NetSales", "context_ref": "CurrentYearDuration", "period_end": "2026-03-31", "value": "1"},
            {"element": "tse-ed-t:NetSales", "context_ref": "CurrentYearDuration_Q1", "period_end": "2025-06-30", "value": "1"},
        ])
        assert derive_fiscal_period_end(tidy) == date(2026, 3, 31)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("FY", "本決算"), ("1Q", "1Q"), ("2Q", "2Q"), ("3Q", "3Q")],
    )
    def test_disclosure_type(self, value, expected):
        tidy = tidy_frame([
            {"element": "tse-ed-t:TypeOfCurrentPeriodDETAIL", "context_ref": "FilingDateInstant", "value": value},
        ])
        assert derive_disclosure_type(tidy) == expected

    def test_disclosure_type_override(self):
        tidy = tidy_frame([
            {"element": "tse-ed-t:NetSales", "context_ref": "CurrentYearDuration", "consolidated": "連結", "value": "1", "period_end": "2026-03-31"},
        ])
        record = tidy_to_financial_record(tidy, "7203", prov(), disclosure_type="修正")
        assert record.disclosure_type == "修正"


class TestYfValuation:
    def test_real_info_fixture(self):
        """実フィクスチャ (7203.T info, 2026-06-10 取得) で抽出を検証。"""
        info = json.loads(fixture_path("transform/yfinance_7203T_info.json").read_text())
        val = yf_info_to_valuation(info)
        assert val["per"] == pytest.approx(info["trailingPE"])
        assert val["pbr"] == pytest.approx(info["priceToBook"])
        assert val["market_cap"] == info["marketCap"]
        # yfinance 1.x は % 単位 (実フィクスチャで 3.53 = 3.53% を確認)
        assert val["dividend_yield_pct"] == pytest.approx(info["dividendYield"])
        assert 0 < val["dividend_yield_pct"] < 15

    def test_missing_keys_stay_none(self):
        assert yf_info_to_valuation({}) == {
            "per": None, "pbr": None, "market_cap": None, "dividend_yield_pct": None,
        }
