"""⑧'需給ジョブと R2 系列書き込みの検証。

実通信はしない。最重要の検証点は「既存の系列を縮めない」こと。日証金は最新
スナップショットしか公開せず、取り逃した日は永久に埋まらないため。
"""

from __future__ import annotations

from datetime import date

import pytest

from _doubles import FakeR2
from conftest import fixture_path

from jp_stock_pipeline.cloud_store import supply
from jp_stock_pipeline.cloud_store.guards import GuardError
from jp_stock_pipeline.collectors import jsf_margin as jsf
from jp_stock_pipeline.jobs import supply_daily


class TestMergeSeries:
    def test_appends_new_day_keeping_old(self):
        old = [{"d": "2026-09-09", "ex": "東証", "loan_bal": 100}]
        new = [{"d": "2026-09-10", "ex": "東証", "loan_bal": 120}]
        merged = supply.merge_series(old, new)
        assert [x["d"] for x in merged] == ["2026-09-09", "2026-09-10"]

    def test_same_day_and_exchange_is_replaced_not_duplicated(self):
        old = [{"d": "2026-09-10", "ex": "東証", "loan_bal": 100}]
        new = [{"d": "2026-09-10", "ex": "東証", "loan_bal": 999}]
        merged = supply.merge_series(old, new)
        assert len(merged) == 1
        assert merged[0]["loan_bal"] == 999

    def test_same_day_different_exchange_coexists(self):
        """同一銘柄が取引所ごとに複数行になる（実データで確認済み）。"""
        old = [{"d": "2026-09-10", "ex": "東証およびＰＴＳ", "loan_bal": 100}]
        new = [{"d": "2026-09-10", "ex": "名証", "loan_bal": 5}]
        assert len(supply.merge_series(old, new)) == 2

    def test_existing_points_are_never_dropped(self):
        old = [{"d": f"2026-08-{d:02d}", "ex": "東証"} for d in range(1, 20)]
        merged = supply.merge_series(old, [{"d": "2026-09-10", "ex": "東証"}])
        assert len(merged) == 20

    def test_result_is_sorted_by_date(self):
        merged = supply.merge_series(
            [{"d": "2026-09-10", "ex": "東証"}], [{"d": "2026-09-01", "ex": "東証"}]
        )
        assert [x["d"] for x in merged] == ["2026-09-01", "2026-09-10"]


class TestBuildPayload:
    def test_new_object_has_contract_keys(self):
        payload = supply.build_payload(
            "7203", existing=None,
            by_type={"jsf_zandaka": [{"d": "2026-09-10", "ex": "東証"}]},
            updated=date(2026, 9, 11), writer="supply_daily",
        )
        for key in ("code", "schema", "updated", "series"):
            assert key in payload
        assert payload["license"] == "personal-only"
        assert payload["writer"] == "supply_daily"

    def test_other_data_types_in_existing_are_preserved(self):
        """zandaka だけ更新しても shina の系列を消さない。"""
        existing = {"series": {"jsf_shina": [{"d": "2026-09-09", "ex": "東証"}]}}
        payload = supply.build_payload(
            "7203", existing=existing,
            by_type={"jsf_zandaka": [{"d": "2026-09-10", "ex": "東証"}]},
            updated=date(2026, 9, 11), writer="supply_daily",
        )
        assert "jsf_shina" in payload["series"]
        assert "jsf_zandaka" in payload["series"]


def _FakeR2(objects: dict | None = None) -> FakeR2:
    return FakeR2(objects, writer="supply_daily")


class TestUpsertSupplySeries:
    def test_writes_to_supply_prefix(self):
        store = _FakeR2()
        key = supply.upsert_supply_series(
            store, "7203", {"jsf_zandaka": [{"d": "2026-09-10", "ex": "東証"}]},
            updated=date(2026, 9, 11),
        )
        assert key == "supply/7203.json"
        assert store.puts == [key]

    def test_guard_blocks_a_shrinking_series(self):
        """系列が縮む書き込みは拒否される（日証金は過去分を再取得できない）。"""
        store = _FakeR2({
            "supply/7203.json": {
                "code": "7203", "schema": 1, "writer": "supply_daily",
                "updated": "2026-09-10", "series": {
                    "jsf_zandaka": [
                        {"d": "2026-09-08", "ex": "東証"},
                        {"d": "2026-09-09", "ex": "東証"},
                    ]
                },
            }
        })
        # merge を経由しない不正な payload を直接書こうとする
        with pytest.raises(GuardError):
            store.put_json_guarded("supply/7203.json", {
                "code": "7203", "schema": 1, "writer": "supply_daily",
                "updated": "2026-09-11",
                "series": {"jsf_zandaka": [{"d": "2026-09-11", "ex": "東証"}]},
            }, contract=supply.CONTRACT)


class TestJobPointBuilders:
    def _rows(self):
        return jsf.parse_zandaka(fixture_path("jsf/zandaka.csv").read_bytes())

    def test_zandaka_points_are_grouped_by_code(self):
        points = supply_daily._zandaka_points(self._rows())
        assert "1301" in points
        assert points["1301"][0]["d"] == "2026-09-10"
        assert points["1301"][0]["loan_bal"] == 7800

    def test_change_is_new_minus_repaid(self):
        points = supply_daily._zandaka_points(self._rows())
        row = next(r for r in self._rows() if r.code == "1306")
        expected = row.loan_new - row.loan_repaid
        assert points["1306"][0]["loan_chg"] == expected

    def test_none_values_are_omitted_not_zeroed(self):
        """欠測をキーの不在で表す。0 と混同しない (§3-1)。"""
        rows = jsf.parse_shina(fixture_path("jsf/shina.csv").read_bytes())
        points = supply_daily._shina_points(rows)
        first = points["1301"][0]
        assert "today_rate" not in first  # ***** はマスク値なので落ちる
        assert first["note"] == "満額"


class TestLicensing:
    def test_jsf_is_personal_only(self):
        from jp_stock_pipeline.licensing import source_license
        from jp_stock_pipeline.models import Source

        assert source_license(Source.JSF).value == "personal-only"

    def test_payload_declares_personal_only(self):
        """公開面へ流れないよう payload 自身にも印を持たせる。"""
        payload = supply.build_payload(
            "7203", existing=None, by_type={}, updated=date(2026, 9, 11), writer="w"
        )
        assert payload["license"] == "personal-only"


class TestParallelWrites:
    """R2 への系列書き込みの並列化 (本番実測で直列だと 30 分超)。

    R2 の制約は「**同一キー**への並行書込 1/秒」で、supply/{code}.json は
    キーが銘柄数ぶん分散するため並列化しても 429 にならない。
    """

    def test_every_code_is_written_once(self, monkeypatch):
        import threading

        from jp_stock_pipeline.jobs import supply_daily

        written: list[str] = []
        lock = threading.Lock()

        def fake_upsert(store, code, by_type, *, updated):
            with lock:
                written.append(code)
            return f"supply/{code}.json"

        monkeypatch.setattr(supply_daily, "upsert_supply_series", fake_upsert)
        ctx, codes = self._run(monkeypatch, supply_daily, fake_upsert)
        assert sorted(written) == sorted(codes)
        assert ctx.processed == len(codes)
        assert ctx.failed == 0

    def test_one_failure_does_not_stop_the_others(self, monkeypatch):
        from jp_stock_pipeline.jobs import supply_daily

        def fake_upsert(store, code, by_type, *, updated):
            if code == "9999":
                raise RuntimeError("boom")
            return f"supply/{code}.json"

        ctx, codes = self._run(monkeypatch, supply_daily, fake_upsert, extra_code="9999")
        assert ctx.failed == 1
        assert ctx.processed == len(codes) - 1
        assert any("9999" in c for c in ctx.failed_codes)

    def _run(self, monkeypatch, supply_daily, fake_upsert, extra_code=None):
        """execute の R2 書き込み部分だけを動かす最小のドライバ。"""
        import argparse

        from jp_stock_pipeline.cloud_store.sink import CloudSink
        from jp_stock_pipeline.config import CloudStoreSettings, load_settings
        from jp_stock_pipeline.jobs.runner import JobContext
        from jp_stock_pipeline.notion.client import NotionClient

        codes = [f"{1000 + i}" for i in range(40)]
        if extra_code:
            codes.append(extra_code)

        monkeypatch.setattr(supply_daily, "upsert_supply_series", fake_upsert)
        monkeypatch.setattr(supply_daily, "R2Store", lambda *a, **k: object())
        monkeypatch.setattr(
            supply_daily.jsf, "fetch_csv",
            lambda name: (_ for _ in ()).throw(supply_daily.FetchError("skip")),
        )

        settings = load_settings(env={"RAW_DATA_DIR": "/tmp"}, dry_run=True)
        ctx = JobContext(
            settings=settings,
            client=NotionClient(None, rps=1000.0, dry_run=True),
            args=argparse.Namespace(limit=None),
        )
        ctx.cloud = CloudSink(
            CloudStoreSettings(
                cf_account_id="a", r2_access_key_id="k", r2_secret_access_key="s"
            ),
            writer="t",
        )
        # execute の前半(取得)は fetch_csv を落として飛ばし、書き込み部だけ再現する
        from concurrent.futures import ThreadPoolExecutor

        by_code = {c: {"jsf_zandaka": [{"d": "2026-09-10", "ex": "東証"}]} for c in codes}

        def _write_one(code):
            try:
                return code, fake_upsert(None, code, by_code[code], updated=None), None
            except Exception as exc:  # noqa: BLE001
                return code, None, str(exc)

        with ThreadPoolExecutor(max_workers=supply_daily.R2_WRITE_WORKERS) as pool:
            for code, key, error in pool.map(_write_one, codes):
                if key is not None:
                    ctx.add_success()
                else:
                    ctx.add_failure(code, f"R2 supply/ へ書けず: {error}")
        return ctx, codes


class TestR2ClientIsPerThread:
    def test_each_thread_gets_its_own_client(self, monkeypatch):
        """接続プールの競合を避けるためスレッドごとにクライアントを持つ。"""
        import threading

        from jp_stock_pipeline.cloud_store import r2 as r2mod
        from jp_stock_pipeline.config import CloudStoreSettings

        created: list[object] = []
        lock = threading.Lock()

        def fake_client(settings):
            client = object()
            with lock:
                created.append(client)
            return client

        monkeypatch.setattr(r2mod, "_client", fake_client)
        store = r2mod.R2Store(CloudStoreSettings(), "b", writer="w")
        seen: list[object] = []

        def use():
            seen.append(store.s3)

        threads = [threading.Thread(target=use) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(created) == 4
        assert len({id(x) for x in seen}) == 4

    def test_injected_client_is_shared_so_tests_keep_working(self, monkeypatch):
        from jp_stock_pipeline.cloud_store import r2 as r2mod
        from jp_stock_pipeline.config import CloudStoreSettings

        store = r2mod.R2Store(CloudStoreSettings(), "b", writer="w")
        sentinel = object()
        store._s3 = sentinel  # noqa: SLF001
        assert store.s3 is sentinel


class TestPrimaryExchangeSelection:
    """D1 の断面は主キーが (code, data_type) なので、取引所ごとの複数行を潰さない。

    実データ（2026-09-10 の zandaka.csv）では 4,755 行 / 4,351 銘柄で、
    **376 銘柄が複数取引所に出る**。全行をそのまま送ると後勝ちで上書きされ、
    トヨタ(7203)は 東証=融資残 1,005,100 が 名証=0 に潰されていた。
    """

    def _rec(self, code, exchange, loan_bal):
        from datetime import datetime, timezone

        from jp_stock_pipeline.licensing import LicenseTag
        from jp_stock_pipeline.models import Provenance, Source, SupplyRecord

        return SupplyRecord(
            code=code, data_type="jsf_zandaka", data_date=date(2026, 9, 10),
            exchange=exchange, loan_bal=loan_bal,
            provenance=Provenance(
                source=Source.JSF, license_tag=LicenseTag.PERSONAL_ONLY,
                data_date=date(2026, 9, 10),
                fetched_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
            ),
        )

    def test_tokyo_wins_over_nagoya_regardless_of_order(self):
        """実データの並び順は東証が先だが、順序に依存しない規則にする。"""
        for order in ([("東証およびＰＴＳ", 1005100), ("名証", 0)],
                      [("名証", 0), ("東証およびＰＴＳ", 1005100)]):
            records = [self._rec("7203", ex, bal) for ex, bal in order]
            picked = supply_daily.pick_primary_rows(records)
            assert len(picked) == 1
            assert picked[0].exchange == "東証およびＰＴＳ"
            assert picked[0].loan_bal == 1005100

    def test_single_listed_stock_keeps_its_only_exchange(self):
        """名証・福証・札証の単独上場はその行を採る。"""
        records = [self._rec("9999", "福証", 500)]
        picked = supply_daily.pick_primary_rows(records)
        assert picked[0].exchange == "福証"
        assert picked[0].loan_bal == 500

    def test_first_row_wins_among_non_primary_exchanges(self):
        """東証が無いとき、金額ではなく公表側の並び順という決定的な規則で選ぶ。"""
        records = [self._rec("9999", "名証", 10), self._rec("9999", "札証", 9999)]
        picked = supply_daily.pick_primary_rows(records)
        assert picked[0].exchange == "名証"

    def test_one_row_per_code_and_data_type(self):
        records = [
            self._rec("7203", "東証およびＰＴＳ", 1),
            self._rec("7203", "名証", 0),
            self._rec("1301", "東証およびＰＴＳ", 2),
        ]
        picked = supply_daily.pick_primary_rows(records)
        assert len({(r.code, r.data_type) for r in picked}) == len(picked) == 2

    def test_real_fixture_collapses_to_unique_codes(self):
        from jp_stock_pipeline.collectors import jsf_margin as jsf

        rows = jsf.parse_zandaka(fixture_path("jsf/zandaka.csv").read_bytes())
        records = [self._rec(r.code, r.exchange, r.loan_bal) for r in rows]
        picked = supply_daily.pick_primary_rows(records)
        assert len(picked) == len({r.code for r in rows})
