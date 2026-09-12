"""鮮度 SLO とジョブ結果記録のテスト。

「壊れても音が鳴らない」が本システム最大の欠陥だった。実測:
  EDINET 11 営業日連続 processed=0 で「成功」/ JPX 33 日停止 / 優待 81.5 日停止
  jss_job_runs も jss_dataset_freshness も 0 行（writer 不在）
ここが機能しなくなったら気づけるようにする。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from jp_stock_pipeline.cloud_store import ops, slo


class _FakeStore:
    """D1Store の代わりにローカル sqlite へ本物の SQL を流す。"""

    def __init__(self) -> None:
        from jp_stock_pipeline.cloud_store.schema import SCHEMA_STATEMENTS

        self.con = sqlite3.connect(":memory:")
        for stmt in SCHEMA_STATEMENTS:
            self.con.execute(stmt)
        self.con.commit()

    def query(self, sql: str, params: list | None = None):
        cur = self.con.execute(sql, params or [])
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        self.con.commit()
        return rows


NOW = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)


def _epoch_days_ago(days: float) -> int:
    return int((NOW - timedelta(days=days)).timestamp())


class TestFreshnessSlo:
    def test_全データセットに閾値がある(self) -> None:
        """閾値が無いと「33日古い」が異常か判定できない。"""
        assert len(slo.SLOS) >= 7
        for s in slo.SLOS:
            assert s.green_hours < s.yellow_hours, s.dataset
            assert s.note, s.dataset

    def test_未定義のデータセットは緑にしない(self) -> None:
        """知らないものを「問題なし」に倒さない。"""
        assert slo.judge("unknown_dataset", _epoch_days_ago(0), now=NOW) == "unknown"

    def test_記録が無ければ緑にしない(self) -> None:
        assert slo.judge("prices_daily", None, now=NOW) == "unknown"
        assert slo.judge("prices_daily", 0, now=NOW) == "unknown"

    @pytest.mark.parametrize(
        ("dataset", "days", "expected"),
        [
            # 実測値そのもの
            ("prices_daily", 0.1, "green"),      # 2.4h
            ("jsf_supply", 0.5, "green"),
            ("core_stocks", 33.0, "green"),       # 33.0日 = まだ緑（あと7日で黄）
            ("core_stocks", 41.0, "yellow"),
            ("core_stocks", 46.0, "red"),
            ("yutai_benefits", 81.5, "red"),      # 81.5日 = 赤
            ("edinet_documents", 0.5, "green"),
            ("edinet_documents", 1.5, "yellow"),  # 36h
            ("edinet_documents", 3.0, "red"),
        ],
    )
    def test_実測値の判定(self, dataset: str, days: float, expected: str) -> None:
        assert slo.judge(dataset, _epoch_days_ago(days), now=NOW) == expected

    def test_境界ちょうどは緑(self) -> None:
        s = slo.SLO_BY_DATASET["prices_daily"]
        assert s.judge(s.green_hours) == "green"
        assert s.judge(s.green_hours + 0.01) == "yellow"
        assert s.judge(s.yellow_hours) == "yellow"
        assert s.judge(s.yellow_hours + 0.01) == "red"


class TestRecordJobRun:
    def test_実行結果を残す(self) -> None:
        store = _FakeStore()
        ops.record_job_run(
            store, job_name="edinet_daily", status="成功", processed=0, failed=0,
            failed_codes=[], run_url="https://example/1", duration_secs=70.0,
            finished_at=1_700_000_000,
        )
        rows = store.query("SELECT * FROM jss_job_runs")
        assert len(rows) == 1
        assert rows[0]["job_name"] == "edinet_daily"
        assert rows[0]["processed"] == 0

    def test_失敗コードは上限で切る(self) -> None:
        store = _FakeStore()
        codes = [f"{i:04d}" for i in range(200)]
        ops.record_job_run(
            store, job_name="j", status="一部失敗", processed=1, failed=200,
            failed_codes=codes, run_url=None, duration_secs=None, finished_at=1,
        )
        saved = json.loads(store.query("SELECT failed_codes FROM jss_job_runs")[0]["failed_codes"])
        assert len(saved) == ops.MAX_FAILED_CODES

    def test_処理ゼロの成功も残る(self) -> None:
        """EDINET が 11 営業日「成功 processed=0」だったのを後から見つけられるように。"""
        store = _FakeStore()
        for day in range(11):
            ops.record_job_run(
                store, job_name="edinet_daily", status="成功", processed=0, failed=0,
                failed_codes=[], run_url=None, duration_secs=70.0, finished_at=day,
            )
        rows = store.query(
            "SELECT COUNT(*) AS n FROM jss_job_runs"
            " WHERE job_name='edinet_daily' AND status='成功' AND processed=0"
        )
        assert rows[0]["n"] == 11

    def test_記録に失敗してもジョブを落とさない(self) -> None:
        class Broken:
            def query(self, sql, params=None):
                from jp_stock_pipeline.cloud_store.d1 import D1Error

                raise D1Error("boom")

        assert ops.safe_record_job_run(Broken(), job_name="j", status="成功", processed=1,
                                       failed=0, failed_codes=[], run_url=None,
                                       duration_secs=None, finished_at=1) is False

    def test_D1_未設定なら記録しないが落ちない(self) -> None:
        assert ops.safe_record_job_run(None, job_name="j", status="成功", processed=1,
                                       failed=0, failed_codes=[], run_url=None,
                                       duration_secs=None, finished_at=1) is False


class TestRecordFreshness:
    def test_1データセット1行で上書きする(self) -> None:
        store = _FakeStore()
        for n, ts in ((10, 100), (20, 200)):
            ops.record_freshness(
                store, dataset="prices_daily", store_name="D1", location="core_stock_financials",
                writer="prices_daily", latest_data_date="2026-09-11",
                row_or_object_count=n, bytes_=None, license_tag="personal-only", updated_at=ts,
            )
        rows = store.query("SELECT * FROM jss_dataset_freshness")
        assert len(rows) == 1
        assert rows[0]["row_or_object_count"] == 20
        assert rows[0]["updated_at"] == 200

    def test_date型を受け付ける(self) -> None:
        from datetime import date

        store = _FakeStore()
        ops.record_freshness(
            store, dataset="d", store_name="D1", location="t", writer="w",
            latest_data_date=date(2026, 9, 11), row_or_object_count=1, bytes_=None,
            license_tag=None, updated_at=1,
        )
        assert store.query("SELECT latest_data_date FROM jss_dataset_freshness")[0][
            "latest_data_date"
        ] == "2026-09-11"
