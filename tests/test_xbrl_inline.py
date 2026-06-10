"""TDnet 短信 zip (インラインXBRLのみ) → tidy → ③ レコードの実フィクスチャテスト。

実フィクスチャ: tests/fixtures/tdnet/tanshin_xbrl_2751_20260610.zip
(2751 JALUX 2026年4月期決算短信。www.release.tdnet.info/inbs/081220260610567076.zip
 を 2026-06-11 に実取得。TDnet 短信 zip は *.xbrl を含まず iXBRL 1.0 (2008ns) のみ)
ゴールデン値は同短信の公表数値（売上高534億円等）。
"""

from __future__ import annotations

from datetime import date

import pytest

from conftest import fixture_path

from jp_stock_pipeline.convert.xbrl_to_csv import (
    TIDY_COLUMNS,
    _decode_ix_value,
    xbrl_zip_to_tidy,
)
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import Provenance, Source, now_jst
from jp_stock_pipeline.transform.normalize import tidy_to_financial_record

DOC_ID = "081220260610567076"


@pytest.fixture(scope="module")
def tidy():
    zip_bytes = fixture_path("tdnet/tanshin_xbrl_2751_20260610.zip").read_bytes()
    return xbrl_zip_to_tidy(zip_bytes, "2751", DOC_ID)


class TestInlineXbrlParse:
    def test_facts_extracted(self, tidy):
        assert len(tidy) > 500
        assert list(tidy.columns) == TIDY_COLUMNS

    def test_scale_decoded_to_true_yen(self, tidy):
        """ix:nonFraction の scale=6 (百万円表示) が円単位へ復号される。"""
        rows = tidy[
            (tidy["element"] == "jppfs_cor:NetSales")
            & (tidy["context_ref"] == "CurrentYearDuration")
        ]
        assert not rows.empty
        assert rows.iloc[0]["value"] == "53408000000"  # 534.08億円 (短信公表値)
        assert rows.iloc[0]["consolidated"] == "連結"

    def test_non_numeric_text_kept_raw(self, tidy):
        rows = tidy[tidy["element"].str.endswith("AccountingStandardsDEI", na=False)]
        if rows.empty:
            rows = tidy[tidy["value"] == "Japan GAAP"]
        assert not rows.empty  # DEI テキストファクトが原文のまま入る


class TestFinancialRecord:
    def test_record_golden_values(self, tidy):
        prov = Provenance(
            source=Source.TDNET,
            license_tag=LicenseTag.FACTUAL_CITE,
            data_date=date(2026, 6, 10),
            fetched_at=now_jst(),
        )
        rec = tidy_to_financial_record(tidy, "2751", prov)
        assert rec is not None
        assert rec.fiscal_period_end == date(2026, 4, 30)
        assert rec.disclosure_type == "本決算"
        assert rec.consolidated == "連結"
        assert rec.accounting_standard == "日本基準"
        assert rec.net_sales == pytest.approx(53_408_000_000)
        assert rec.operating_income == pytest.approx(2_890_000_000)
        assert rec.net_income == pytest.approx(1_894_000_000)
        assert rec.eps == pytest.approx(157.22)
        assert rec.equity_ratio_pct == pytest.approx(60.4)
        assert rec.forecast_net_sales == pytest.approx(29_660_000_000)
        assert rec.forecast_eps == pytest.approx(101.38)


class TestDecodeIxValue:
    @pytest.mark.parametrize(
        ("text", "sign", "scale", "expected"),
        [
            ("47,055", None, "6", "47055000000"),
            ("157.22", None, "0", "157.22"),
            ("0.604", None, "0", "0.604"),
            ("123", "-", "3", "-123000"),
            ("－", None, "6", ""),  # 欠損表示は欠損のまま (§3-1)
            ("", None, None, ""),
            ("注記", None, "0", ""),  # 数値化不能は欠損
        ],
    )
    def test_decode(self, text, sign, scale, expected):
        assert _decode_ix_value(text, sign, scale) == expected
