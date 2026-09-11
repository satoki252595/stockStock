"""JPX 信用残 PDF パーサのテスト (移行 P2)。

R2 `vwap-data/margin/{date}.json` の writer を kabulab-cf から stockStock へ
移管するため、**出力が1バイトも変わらないこと**が最重要。期待値は
kabulab-cf の TypeScript 実装の実出力から生成している（自己参照を避ける）。
実PDF 5週分での完全一致は tests/fixtures/jpx/README.md に記録した。
"""

from __future__ import annotations

import json

from conftest import fixture_path

from jp_stock_pipeline.collectors import jpx_margin as jm


def _fixture_text() -> str:
    return fixture_path("jpx/syumatsu_weekly_20260904.txt").read_text(encoding="utf-8")


def _expected() -> dict:
    return json.loads(
        fixture_path("jpx/expected_weekly_20260904.json").read_text(encoding="utf-8")
    )


class TestParseMarginText:
    def test_matches_kabulab_cf_output_exactly(self):
        """kabulab-cf の TS 実装の出力と完全一致すること（移行ゲート G-margin-1）。"""
        assert jm.parse_margin_text(_fixture_text()).to_dict() == _expected()

    def test_week_is_derived_from_application_date(self):
        assert jm.parse_margin_text(_fixture_text()).week == "2026-09-04"

    def test_code_is_first_four_of_five_digit_code(self):
        """PDF は5桁コード（末尾は予備桁）。先頭4桁が銘柄コード。"""
        rows = jm.parse_margin_text(_fixture_text()).rows
        assert rows[0].code == "1301"
        assert all(len(r.code) == 4 for r in rows)

    def test_negative_change_uses_black_triangle(self):
        """「▲ 400」は -400。▲ は負号であって欠損ではない。"""
        rows = {r.code: r for r in jm.parse_margin_text(_fixture_text()).rows}
        assert rows["1301"].sell_chg == -400
        assert rows["1301"].buy_chg == -1600

    def test_contract_keys_are_exactly_five(self):
        """rows[] のキー構成は公開 API の契約。増減させない。"""
        row = jm.parse_margin_text(_fixture_text()).rows[0].to_dict()
        assert list(row) == ["code", "sell", "sell_chg", "buy", "buy_chg"]

    def test_empty_text_yields_empty_result_not_crash(self):
        data = jm.parse_margin_text("")
        assert data.week == ""
        assert data.rows == []


class TestToInt:
    def test_strips_separators_and_converts_triangle(self):
        assert jm.to_int("156,700") == 156700
        assert jm.to_int("▲ 1,600") == -1600
        assert jm.to_int("▲1,600") == -1600

    def test_unparsable_falls_back_to_zero_for_compatibility(self):
        """kabulab-cf の `|| 0` と同じ挙動。ここを None にすると契約を壊す。"""
        assert jm.to_int("") == 0
        assert jm.to_int("---") == 0


class TestDetectLayout:
    def test_weekly_layout_from_real_text(self):
        assert jm.detect_layout(_fixture_text()) == jm.Layout.WEEKLY

    def test_daily_layout_detected_by_previous_day_label(self):
        """2026-09-28 以降の日次様式は「前日比」を持つ。"""
        assert jm.detect_layout("銘柄別信用取引残高 前日比 上場比") == jm.Layout.DAILY

    def test_ambiguous_text_is_unknown_not_guessed(self):
        """判定できないテキストは推測せず UNKNOWN（§3-1）。"""
        assert jm.detect_layout("") == jm.Layout.UNKNOWN
        assert jm.detect_layout("前週比 と 前日比 が両方ある") == jm.Layout.UNKNOWN


class TestPdfUrlDiscovery:
    HTML = """
    <a href="/markets/statistics-equities/margin/tvdivq0000001rnl-att/syumatsu2026082800.pdf">8/28</a>
    <a href="/markets/statistics-equities/margin/tvdivq0000001rnl-att/syumatsu2026090400.pdf">9/4</a>
    <a href="/markets/statistics-equities/margin/tvdivq0000001rnl-att/syumatsu2026082100.pdf">8/21</a>
    """

    def test_latest_is_the_newest_timestamp_not_document_order(self):
        url = jm.latest_pdf_url(self.HTML)
        assert url.endswith("syumatsu2026090400.pdf")
        assert url.startswith("https://www.jpx.co.jp/")

    def test_all_urls_returned_oldest_first(self):
        urls = jm.list_pdf_urls(self.HTML)
        assert [u[-14:-4] for u in urls] == ["2026082100", "2026082800", "2026090400"]

    def test_missing_link_raises_instead_of_returning_empty(self):
        import pytest

        with pytest.raises(ValueError, match="リンクが一覧ページに無い"):
            jm.latest_pdf_url("<html>no pdf here</html>")
