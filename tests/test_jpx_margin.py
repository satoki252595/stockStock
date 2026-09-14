"""JPX 信用残 PDF パーサのテスト (移行 P2)。

R2 `vwap-data/margin/{date}.json` の writer を kabulab-cf から stockStock へ
移管するため、**出力が1バイトも変わらないこと**が最重要。期待値は
kabulab-cf の TypeScript 実装の実出力から生成している（自己参照を避ける）。
実PDF 5週分での完全一致は tests/fixtures/jpx/README.md に記録した。
"""

from __future__ import annotations

import json

import pytest
from conftest import FIXTURES_DIR, fixture_path

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

    def test_code_is_first_four_when_check_char_is_zero(self):
        """PDF は5桁コード。末尾 "0" の行は先頭4桁が銘柄コード。

        2026-09-13 まで「全行が4桁」を検査していたが、種類株 (末尾 "1"-"9") を
        普通株のコードへ潰す取り違えを固定していたので、末尾 "0" の行に限定した。
        このフィクスチャ (先頭50行) は全行が末尾 "0" なので結果は変わらない。
        """
        text = _fixture_text()
        rows = jm.parse_margin_text(text).rows
        code5 = [mm.group(1) for mm in jm._ROW_RE.finditer(text)]
        assert rows[0].code == "1301"
        assert [r.code for r in rows] == [
            c[:4] if c.endswith("0") else c for c in code5
        ]

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


# 合成テキスト。行の形 (5桁コード + ISIN + 数値4列以上) だけを実 PDF に合わせ、
# 銘柄名・ISIN・数値は架空。種類株が普通株の直後に並ぶ並びは実 PDF と同じ
# (2026-08-28 / 09-04 の実測では衝突 6 組すべてで普通株が先)。
# kabulab-cf `services/vwap-analysis/lib/margin.test.ts` と同じ入力・同じ期待値。
SYNTHETIC_TEXT = """2026/9/4 申込み現在 End-of-week outstanding margin trading by issue
B 合成食品　普通株式 25930 JP0000000011 1,000 ▲ 100 2,000 200 0 0 1,000 ▲ 100 0 0 2,000 200
B 株式会社合成食品第１種優先株式 25935 JP0000000029 10 0 20 ▲ 5 0 0 10 0 0 0 20 ▲ 5
B 合成通信　普通株式 94340 JP0000000037 3,000 300 4,000 ▲ 400 0 0 3,000 300 0 0 4,000 ▲ 400
B 合成通信株式会社第１回社債型種類株式 94345 JP0000000045 0 0 30 3 0 0 0 0 0 0 30 3
B 合成通信株式会社第２回社債型種類株式 94346 JP0000000052 0 0 0 0 0 0 0 0 0 0 0 0
J 合成ＴＯＰＩＸ連動型上場投信　受益証券12020 JP0000000060 5,000 50 6,000 60 0 0 5,000 50 0 0 6,000 60
B 合成新興　普通株式 130A0 JP0000000078 7 ▲ 1 8 1 0 0 7 ▲ 1 0 0 8 1
"""

SYNTHETIC_EXPECTED = [
    {"code": "2593", "sell": 1000, "sell_chg": -100, "buy": 2000, "buy_chg": 200},
    {"code": "25935", "sell": 10, "sell_chg": 0, "buy": 20, "buy_chg": -5},
    {"code": "9434", "sell": 3000, "sell_chg": 300, "buy": 4000, "buy_chg": -400},
    {"code": "94345", "sell": 0, "sell_chg": 0, "buy": 30, "buy_chg": 3},
    {"code": "94346", "sell": 0, "sell_chg": 0, "buy": 0, "buy_chg": 0},
    {"code": "1202", "sell": 5000, "sell_chg": 50, "buy": 6000, "buy_chg": 60},
    {"code": "130A", "sell": 7, "sell_chg": -1, "buy": 8, "buy_chg": 1},
]


class TestFiveCharCodeToKey:
    """5桁コード → rows[].code。フィクスチャ不要で常時走る (回帰検知の本体)。"""

    def test_synthetic_rows_match_expected_exactly(self):
        data = jm.parse_margin_text(SYNTHETIC_TEXT)
        assert data.week == "2026-09-04"
        assert [r.to_dict() for r in data.rows] == SYNTHETIC_EXPECTED

    def test_class_shares_do_not_collapse_onto_common_stock(self):
        """旧実装は 25930/25935 を両方 "2593" にしていた (取り違え)。"""
        codes = [r.code for r in jm.parse_margin_text(SYNTHETIC_TEXT).rows]
        assert len(set(codes)) == len(codes)

    def test_no_row_is_dropped(self):
        """source_code_to_ticker だと種類株 3 行を落とす。落とさないこと。"""
        text = SYNTHETIC_TEXT
        assert len(jm.parse_margin_text(text).rows) == len(list(jm._ROW_RE.finditer(text)))

    def test_reader_lookup_by_four_char_code_still_returns_common_stock(self):
        """`/api/margin` は 4 文字コードで rows を find する。その答えは変えない。"""
        rows = [r.to_dict() for r in jm.parse_margin_text(SYNTHETIC_TEXT).rows]
        first = {c: next(r for r in rows if r["code"] == c) for c in ("2593", "9434")}
        assert first["2593"]["sell"] == 1000
        assert first["9434"]["sell"] == 3000

    def test_etf_glued_to_name_keeps_four_char_code(self):
        """銘柄名とコードの間に空白が無い行 (実 PDF で毎週 49 行) も拾う。"""
        rows = {r.code: r for r in jm.parse_margin_text(SYNTHETIC_TEXT).rows}
        assert rows["1202"].buy == 6000


def _real_pdf_paths():
    return sorted((FIXTURES_DIR / "jpx").glob("syumatsu*.pdf"))


class TestRealPdfCodes:
    """取得済みの実 PDF (gitignore 済み・personal-only) があるときだけ走る。

    取得: `jobs/margin_weekly` と同じ `collectors.jpx_margin.list_pdf_urls` で
    一覧から直近の PDF を tests/fixtures/jpx/ へ置く。無ければ skip。
    """

    @pytest.fixture(params=_real_pdf_paths() or [None], ids=lambda p: p.name if p else "none")
    def real_text(self, request):
        if request.param is None:
            pytest.skip("実PDF未取得: tests/fixtures/jpx/syumatsu*.pdf (commit 禁止・手元のみ)")
        return jm.extract_pdf_text(request.param.read_bytes())

    def test_every_row_code_follows_the_margin_key_rule(self, real_text):
        code5 = [mm.group(1) for mm in jm._ROW_RE.finditer(real_text)]
        rows = jm.parse_margin_text(real_text).rows
        assert len(rows) == len(code5) > 4000
        assert [r.code for r in rows] == [
            c[:4] if c.endswith("0") else c for c in code5
        ]

    def test_no_two_securities_share_a_code(self, real_text):
        codes = [r.code for r in jm.parse_margin_text(real_text).rows]
        dup = sorted({c for c in codes if codes.count(c) > 1})
        assert dup == []

    def test_five_char_keys_are_class_shares_only(self, real_text):
        """5 文字で残るのは種類株だけ (ETF/REIT は全件末尾 "0" だった)。"""
        import re

        five = [r.code for r in jm.parse_margin_text(real_text).rows if len(r.code) == 5]
        assert 0 < len(five) < 50
        for code in five:
            line = next(ln for ln in real_text.splitlines() if re.search(rf"{code}\s+JP", ln))
            assert "種類株式" in line or "優先株式" in line, line


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
