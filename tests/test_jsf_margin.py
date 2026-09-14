"""日本証券金融 (JSF) 貸借取引データのパーサ検証。

フィクスチャは 2026-09-11 に取得した実レスポンスのバイト列（CP932・値は無改変。
行数だけ切り出し）。捏造した入力は使わない (§3-6)。
"""

from __future__ import annotations

from datetime import date

from conftest import fixture_path

from jp_stock_pipeline.collectors import jsf_margin as jsf


def _bytes(name: str) -> bytes:
    return fixture_path(f"jsf/{name}.csv").read_bytes()


class TestZandaka:
    def test_parses_real_rows(self):
        rows = jsf.parse_zandaka(_bytes("zandaka"))
        assert len(rows) == 30
        first = rows[0]
        assert first.code == "1301"
        assert first.name == "極洋"
        assert first.apply_date == date(2026, 9, 10)
        assert first.exchange == "東証およびＰＴＳ"
        assert first.report_type == "確報"
        assert first.loan_bal == 7800
        assert first.stock_bal == 7800

    def test_amounts_and_turn_days_are_read(self):
        rows = {r.code: r for r in jsf.parse_zandaka(_bytes("zandaka"))}
        row = rows["1306"]
        assert row.loan_bal_amount == 198478785
        assert row.stock_bal_amount == 114162816
        assert row.turn_days_total == 3.9

    def test_ratio_is_none_when_denominator_is_zero(self):
        """信用倍率は分母0で無限大を作らない。"""
        row = jsf.ZandakaRow(
            apply_date=date(2026, 9, 10), code="9999", name="x",
            exchange="東証", report_type="確報", loan_bal=1000, stock_bal=0,
        )
        assert row.ratio is None

    def test_ratio_is_computed_when_both_sides_exist(self):
        row = jsf.ZandakaRow(
            apply_date=date(2026, 9, 10), code="9999", name="x",
            exchange="東証", report_type="確報", loan_bal=1000, stock_bal=400,
        )
        assert row.ratio == 2.5

    def test_same_code_can_appear_on_multiple_exchanges(self):
        """主キーは (申込日, コード, 取引所区分名)。コード単独では一意にならない。"""
        rows = jsf.parse_zandaka(_bytes("zandaka"))
        keys = {(r.apply_date, r.code, r.exchange) for r in rows}
        assert len(keys) == len(rows)


class TestShina:
    def test_skips_four_header_rows_and_parses(self):
        rows = jsf.parse_shina(_bytes("shina"))
        assert rows
        first = rows[0]
        assert first.code == "1301"
        assert first.apply_date == date(2026, 9, 10)
        assert first.max_rate == 9.40

    def test_masked_values_become_none_not_zero(self):
        """未確定の `*****` を 0 にすると「逆日歩ゼロ」という誤った事実になる。"""
        rows = {r.code: r for r in jsf.parse_shina(_bytes("shina"))}
        first = rows["1301"]
        assert first.today_rate is None
        assert first.prev_rate is None
        assert first.note == "満額"

    def test_mask_constant_matches_the_real_payload(self):
        assert jsf.MASK == "*****"


class TestMeigara:
    def test_parses_exchange_classes(self):
        rows = jsf.parse_meigara(_bytes("meigara"))
        assert rows
        first = rows[0]
        assert first.code == "1301"
        assert first.apply_date == date(2026, 9, 11)
        assert first.classes["貸借銘柄区分（東証）"] == 1

    def test_unnamed_column_is_dropped(self):
        """列見出し「－」は意味を持たないので取り込まない。"""
        rows = jsf.parse_meigara(_bytes("meigara"))
        assert all("－" not in r.classes for r in rows)


class TestParsingHelpers:
    def test_both_date_formats_are_accepted(self):
        """zandaka は 2026/09/10、shina と meigara は 20260910。"""
        assert jsf._parse_date("2026/09/10") == date(2026, 9, 10)
        assert jsf._parse_date("20260910") == date(2026, 9, 10)
        assert jsf._parse_date("") is None
        assert jsf._parse_date("N/A") is None

    def test_blank_and_mask_are_none(self):
        assert jsf._int("") is None
        assert jsf._int(jsf.MASK) is None
        assert jsf._float(jsf.MASK) is None
        assert jsf._int("1,234") == 1234
        assert jsf._int("0") == 0  # 実値の 0 は 0 のまま


class TestFetchValidation:
    def test_short_response_is_rejected(self, monkeypatch):
        from jp_stock_pipeline import http

        class _Resp:
            content = b"nope"

        monkeypatch.setattr(jsf, "fetch", lambda url, **kw: _Resp())
        try:
            jsf.fetch_csv("zandaka")
        except http.FetchError as exc:
            assert "短すぎる" in str(exc)
        else:
            raise AssertionError("FetchError が送出されなかった")

    def test_non_csv_response_is_rejected(self, monkeypatch):
        from jp_stock_pipeline import http

        class _Resp:
            content = "<html>maintenance</html>".encode("cp932") * 10

        monkeypatch.setattr(jsf, "fetch", lambda url, **kw: _Resp())
        try:
            jsf.fetch_csv("zandaka")
        except http.FetchError as exc:
            assert "CSV でない" in str(exc)
        else:
            raise AssertionError("FetchError が送出されなかった")
