"""ジョブ層のスモークテスト (§10: dry-run 必須 / §8.1-4: 原本必須の保証)。

- 全て dry-run + 実フィクスチャで実行 (Notion へは一切書き込まない §3-6)
- コレクターの fetch 系のみ実フィクスチャのバイト列を返す関数へ差し替える
- 検証点:
  (a) 例外なく完走し終了コードが正しい
  (b) 構造化データ書き込みより前に原本 (⑤) 系の操作が記録される
  (c) RawUploadError 時に構造化 upsert が一切記録されない (§8.1-4)
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from conftest import fixture_path

from jp_stock_pipeline.collectors import edinet_codelist, tdnet_yanoshin, yfinance_prices
from jp_stock_pipeline.jobs import jquants_weekly, master_sync, prices_daily, runner, tdnet_hourly
from jp_stock_pipeline.licensing import LicenseTag, source_license
from jp_stock_pipeline.models import Source
from jp_stock_pipeline.notion import file_upload
from jp_stock_pipeline.notion import schema as S
from jp_stock_pipeline.notion.client import NotionClient
from jp_stock_pipeline.rawstore import save_raw


@pytest.fixture
def captured_clients(monkeypatch) -> list[NotionClient]:
    """run_job 内で生成される NotionClient を捕捉する (ops 検証用)。"""
    created: list[NotionClient] = []

    def factory(token, *, rps, dry_run):
        client = NotionClient(token, rps=1000.0, dry_run=dry_run)
        created.append(client)
        return client

    monkeypatch.setattr(runner, "NotionClient", factory)
    return created


def _env(tmp_path: Path) -> dict[str, str]:
    return {"RAW_DATA_DIR": str(tmp_path / "raw")}


def _ops_with_prop(client: NotionClient, prop_name: str) -> list:
    return [
        op for op in client.ops
        if prop_name in (op.payload.get("properties") or {})
    ]


class TestMasterSync:
    def _patch_fetch(self, monkeypatch, tmp_path):
        zip_bytes = fixture_path("edinet/Edinetcode.zip").read_bytes()

        def fake_fetch(settings):
            return save_raw(
                zip_bytes,
                source=Source.EDINET,
                datatype="codelist",
                scope="ALL",
                data_date=date(2026, 6, 10),
                url="fixture://edinet/Edinetcode.zip",
                ext="zip",
                license_tag=source_license(Source.EDINET),
                base_dir=settings.raw_data_dir,
            )

        monkeypatch.setattr(edinet_codelist, "fetch_codelist", fake_fetch)

    def test_dry_run_completes_and_raw_before_upserts(
        self, monkeypatch, tmp_path, captured_clients
    ):
        self._patch_fetch(monkeypatch, tmp_path)
        code = master_sync.main(["--dry-run", "--limit", "5"], env=_env(tmp_path))
        assert code == 0

        client = captured_clients[0]
        assert client.dry_run
        # (b) 原本 (SHA256 プロパティを持つ ⑤ 行) が ① (銘柄名) より先に記録される
        ops = client.ops
        raw_idx = next(
            i for i, op in enumerate(ops)
            if S.RAW_PROP_SHA256 in (op.payload.get("properties") or {})
        )
        master_idx = next(
            i for i, op in enumerate(ops)
            if S.MASTER_PROP_NAME in (op.payload.get("properties") or {})
        )
        assert raw_idx < master_idx
        # --limit 5 で ① の書き込みは5件
        assert len(_ops_with_prop(client, S.MASTER_PROP_NAME)) == 5
        # 全 ① 行にライセンスタグ commercial-ok が付与される (§6.3, §2.1)
        for op in _ops_with_prop(client, S.MASTER_PROP_NAME):
            tag = op.payload["properties"][S.PROP_LICENSE_TAG]["select"]["name"]
            assert tag == LicenseTag.COMMERCIAL_OK.value

    def test_raw_upload_failure_aborts_structured_writes(
        self, monkeypatch, tmp_path, captured_clients
    ):
        """(c) §8.1-4: 原本UL失敗 → 構造化データを書かず異常終了。"""
        self._patch_fetch(monkeypatch, tmp_path)

        def boom(client, settings, artifact):
            raise file_upload.RawUploadError("テスト: アップロード失敗")

        monkeypatch.setattr(file_upload, "upload_raw_artifact", boom)
        code = master_sync.main(["--dry-run", "--limit", "5"], env=_env(tmp_path))
        assert code == 1  # 失敗

        client = captured_clients[0]
        assert _ops_with_prop(client, S.MASTER_PROP_NAME) == []  # ① への書き込みなし
        # ⑦ ジョブログには 失敗 が記録される
        logs = _ops_with_prop(client, S.JOB_PROP_NAME)
        assert len(logs) == 1
        assert logs[0].payload["properties"][S.JOB_PROP_STATUS]["select"]["name"] == "失敗"


class TestPricesDaily:
    def test_dry_run_with_fixture_frames(self, monkeypatch, tmp_path, captured_clients):
        csv_bytes = fixture_path("transform/yfinance_7203T_daily.csv").read_bytes()
        df = pd.read_csv(fixture_path("transform/yfinance_7203T_daily.csv"))

        def fake_batch(settings, codes, period="2y", **kwargs):
            artifact = save_raw(
                csv_bytes,
                source=Source.YFINANCE,
                datatype="daily_prices_batch",
                scope="ALL",
                data_date=date(2026, 6, 10),
                url="fixture://prices/7203",
                ext="csv",
                license_tag=source_license(Source.YFINANCE),
                base_dir=settings.raw_data_dir,
            )
            return artifact, {"7203": df}, []

        monkeypatch.setattr(yfinance_prices, "fetch_daily_batch", fake_batch)
        code = prices_daily.main(
            ["--dry-run", "--codes", "7203", "--skip-valuation"], env=_env(tmp_path)
        )
        assert code == 0

        client = captured_clients[0]
        price_ops = _ops_with_prop(client, S.PRICE_PROP_RSI14)
        assert len(price_ops) == 1
        props = price_ops[0].payload["properties"]
        # 計算値が実データから算出され、ライセンスは personal-only を継承 (§2.2)
        assert props[S.PROP_LICENSE_TAG]["select"]["name"] == LicenseTag.PERSONAL_ONLY.value
        assert 0 <= props[S.PRICE_PROP_RSI14]["number"] <= 100
        assert props[S.PRICE_PROP_CLOSE]["number"] == pytest.approx(float(df["Close"].iloc[-1]))
        # 原本 (⑤) は ② より先
        raw_idx = next(
            i for i, op in enumerate(client.ops)
            if S.RAW_PROP_SHA256 in (op.payload.get("properties") or {})
        )
        price_idx = client.ops.index(price_ops[0])
        assert raw_idx < price_idx

    def test_all_sources_failed_is_recorded_as_failure(
        self, monkeypatch, tmp_path, captured_clients
    ):
        """yfinance も stooq も失敗 → 欠損として記録、ダミーを書かない (§3-1/3-2)。"""
        from jp_stock_pipeline.collectors import stooq_prices
        from jp_stock_pipeline.http import FetchError

        def empty_batch(settings, codes, period="2y", **kwargs):
            artifact = save_raw(
                b"code,date\n",  # 取得ゼロでも「取得単位」の原本は実在 (空応答の事実)
                source=Source.YFINANCE,
                datatype="daily_prices_batch",
                scope="ALL",
                data_date=date(2026, 6, 10),
                url="fixture://prices/empty",
                ext="csv",
                license_tag=source_license(Source.YFINANCE),
                base_dir=settings.raw_data_dir,
            )
            return artifact, {}, list(codes)

        def stooq_fail(settings, code, **kwargs):
            raise FetchError("テスト: stooq 取得失敗")

        monkeypatch.setattr(yfinance_prices, "fetch_daily_batch", empty_batch)
        monkeypatch.setattr(stooq_prices, "fetch_daily", stooq_fail)
        code = prices_daily.main(
            ["--dry-run", "--codes", "7203", "--skip-valuation"], env=_env(tmp_path)
        )
        assert code == 1  # processed=0 failed=1 → 失敗

        client = captured_clients[0]
        assert _ops_with_prop(client, S.PRICE_PROP_RSI14) == []  # ② に何も書かれない
        logs = _ops_with_prop(client, S.JOB_PROP_NAME)
        props = logs[0].payload["properties"]
        assert props[S.JOB_PROP_FAILED]["number"] == 1
        assert "7203" in props[S.JOB_PROP_FAILED_CODES]["rich_text"][0]["text"]["content"]


class TestTdnetHourly:
    def test_dry_run_with_fixture(self, monkeypatch, tmp_path, captured_clients):
        payload_bytes = fixture_path("tdnet/yanoshin_list_recent.json").read_bytes()
        payload = json.loads(payload_bytes)

        def fake_list(settings, target="recent", limit=300):
            artifact = save_raw(
                payload_bytes,
                source=Source.TDNET,
                datatype="tdnet_list",
                scope=str(target),
                data_date=date(2026, 6, 10),
                url="fixture://tdnet/recent",
                ext="json",
                license_tag=source_license(Source.TDNET),
                base_dir=settings.raw_data_dir,
            )
            records = tdnet_yanoshin.parse_list_payload(
                payload, fetched_at=artifact.fetched_at
            )
            return artifact, records

        monkeypatch.setattr(tdnet_yanoshin, "list_disclosures", fake_list)
        # XBRL 取得はネットワークのため遮断 (失敗経路 = 欠損として記録 §3-2)
        from jp_stock_pipeline.http import FetchError

        def no_network(url, **kwargs):
            raise FetchError(f"テスト: ネットワーク遮断 {url}")

        monkeypatch.setattr(tdnet_hourly, "fetch", no_network)

        code = tdnet_hourly.main(["--dry-run"], env=_env(tmp_path))
        assert code == 0  # ④ は成立する (XBRL→③ の失敗があっても一部失敗まで)

        client = captured_clients[0]
        disc_ops = _ops_with_prop(client, S.DISC_PROP_DOC_ID)
        assert len(disc_ops) > 0
        # ④ のライセンスタグは factual-cite (§2.1)
        for op in disc_ops:
            tag = op.payload["properties"][S.PROP_LICENSE_TAG]["select"]["name"]
            assert tag == LicenseTag.FACTUAL_CITE.value


class TestJquantsWeekly:
    def test_default_target_date_is_weekday(self):
        target = jquants_weekly.default_target_date(date(2026, 6, 10))
        assert target.weekday() < 5
        assert (date(2026, 6, 10) - target).days >= 84  # 12週遅延 (§4)

    def test_statement_to_record_real_columns(self):
        """J-Quants statements の列名契約で ③ レコードが組めること (構造検証)。"""
        row = {
            "LocalCode": "72030",
            "DisclosedDate": "2026-05-08",
            "TypeOfDocument": "FYFinancialStatements_Consolidated_IFRS",
            "TypeOfCurrentPeriod": "FY",
            "CurrentPeriodEndDate": "2026-03-31",
            "NetSales": "48036704000000",
            "OperatingProfit": "4795586000000",
            "Profit": "4765086000000",
            "EarningsPerShare": "365.94",
        }
        rec = jquants_weekly.statement_to_record(row, raw_page_id=None)
        assert rec is not None
        assert rec.code == "7203"
        assert rec.disclosure_type == "本決算"
        assert rec.consolidated == "連結"
        assert rec.net_sales == 48036704000000.0
        assert rec.provenance.license_tag is LicenseTag.PERSONAL_ONLY  # §2.1 厳守
        assert rec.bps is None  # 無い列は None のまま (§3-1)

    def test_statement_missing_key_fields_returns_none(self):
        assert jquants_weekly.statement_to_record({}, raw_page_id=None) is None


class TestWorkflowCrons:
    """§8.2 スケジュール (JST) と cron (UTC) の対応検証。"""

    EXPECTED = {
        "master_sync": "0 21 1 * *",
        "prices_daily": "30 10 * * 1-5",
        "tdnet_hourly": "0 0-10 * * 1-5",
        "edinet_daily": "0 12 * * 1-5",
        "jquants_weekly": "0 0 * * 6",
        "export_weekly": "0 0 * * 0",
    }

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_cron(self, name):
        path = Path(__file__).parent.parent / ".github" / "workflows" / f"{name}.yml"
        text = path.read_text(encoding="utf-8")
        m = re.search(r'cron:\s*"([^"]+)"', text)
        assert m, f"{name}.yml に cron が無い"
        assert m.group(1) == self.EXPECTED[name]
        assert "workflow_dispatch" in text  # 手動実行可
        assert f"jp_stock_pipeline.jobs.{name}" in text

    def test_ci_runs_pytest(self):
        text = (
            Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        assert "pytest" in text
        assert "nix develop" in text
