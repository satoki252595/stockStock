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
from jp_stock_pipeline.jobs import master_sync, prices_daily, reconcile_weekly, runner, tdnet_hourly
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

    def test_prefetch_map_eliminates_per_record_queries(
        self, monkeypatch, tmp_path, captured_clients
    ):
        """① マップ一括取得で per-record 検索を排除する (§8.3 ops削減=60分timeout対策)。

        従来は 1銘柄あたり _find_page(query) + create/update の2callだった。マップを
        先頭で1回ロードし page_resolved=True で渡すことで query は「マップ取得1回」のみ
        に減る（per-record 検索ゼロ）ことを query 呼び出し回数で保証する。
        """
        from jp_stock_pipeline.notion.client import NotionClient

        self._patch_fetch(monkeypatch, tmp_path)
        calls = {"n": 0}

        def counting_query(self, db_id, **kwargs):
            calls["n"] += 1
            return []  # 空 ① = 全件 create 経路

        monkeypatch.setattr(NotionClient, "query_database", counting_query)
        code = master_sync.main(["--dry-run", "--limit", "5"], env=_env(tmp_path))
        assert code == 0
        client = captured_clients[0]
        assert len(_ops_with_prop(client, S.MASTER_PROP_NAME)) == 5  # ① 5件
        # query は固定2回のみ = ⑤原本SHA256重複チェック1回 + ①マップ一括取得1回。
        # per-record 検索(本来は銘柄数=5回)はゼロ。レコード数に比例しないのが要点
        # (従来は 1[⑤] + 5[per-record] = 6 だった)。
        assert calls["n"] == 2

    def test_dedup_by_code_keeps_last_occurrence(self):
        """同一コードの重複は最後を採用（事前マップ運用での二重 create を防ぐ）。"""
        from jp_stock_pipeline.jobs.master_sync import _dedup_by_code

        class _R:
            def __init__(self, code, name):
                self.code = code
                self.name = name

        out = _dedup_by_code([_R("7203", "a"), _R("6758", "b"), _R("7203", "c")])
        assert [r.code for r in out] == ["7203", "6758"]  # 初出順を維持
        assert {r.code: r.name for r in out}["7203"] == "c"  # 値は最後の出現

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

    def test_delisting_detection_marks_absent(self, monkeypatch, tmp_path, captured_clients):
        """コードリストから消えた銘柄を listed=False にする (§ Phase3)。
        状態=上場廃止 の確定は一次開示に一本化し、消失検知では状態を倒さない。"""
        from jp_stock_pipeline.licensing import LicenseTag
        from jp_stock_pipeline.models import Provenance, StockMasterRecord, now_jst
        from jp_stock_pipeline.notion.client import NotionClient

        self._patch_fetch(monkeypatch, tmp_path)
        # コードリストの現役は 7203 のみ
        rec = StockMasterRecord(
            code="7203", name="トヨタ自動車", listed=True, status="上場",
            provenance=Provenance(
                source=Source.EDINET, license_tag=LicenseTag.COMMERCIAL_OK,
                data_date=date(2026, 6, 10), fetched_at=now_jst(),
            ),
        )
        monkeypatch.setattr(edinet_codelist, "parse_codelist", lambda *a, **k: [rec])
        # ① には 7203 と 9999(コードリストから消えた) が既存
        def fake_query(self, db_id, **kwargs):
            return [
                {"id": "p-7203",
                 "properties": {S.MASTER_PROP_CODE: {"rich_text": [{"plain_text": "7203"}]}}},
                {"id": "p-9999",
                 "properties": {S.MASTER_PROP_CODE: {"rich_text": [{"plain_text": "9999"}]}}},
            ]
        monkeypatch.setattr(NotionClient, "query_database", fake_query)

        code = master_sync.main(["--dry-run"], env=_env(tmp_path))  # --limit 無し=全件
        assert code == 0
        client = captured_clients[0]
        delisted = [
            o for o in client.ops
            if o.op == "update_page" and o.payload["page_id"] == "p-9999"
        ]
        assert len(delisted) == 1
        props = delisted[0].payload["properties"]
        assert props[S.MASTER_PROP_LISTED]["checkbox"] is False
        # 消失検知は listed=False のみ。状態=上場廃止 は一次開示由来に一本化 (§3-1/§3-7)
        assert S.MASTER_PROP_STATUS not in props

    def test_blast_radius_guard_blocks_mass_delisting(self, monkeypatch, tmp_path, captured_clients):
        """コードリストが既存の50%未満なら一括上場廃止せず中止する (§ Phase3 安全弁)。"""
        from jp_stock_pipeline.licensing import LicenseTag
        from jp_stock_pipeline.models import Provenance, StockMasterRecord, now_jst
        from jp_stock_pipeline.notion.client import NotionClient

        self._patch_fetch(monkeypatch, tmp_path)
        # 取得は 1 銘柄のみ（異常取得を模す）
        rec = StockMasterRecord(
            code="7203", name="トヨタ自動車", listed=True,
            provenance=Provenance(
                source=Source.EDINET, license_tag=LicenseTag.COMMERCIAL_OK,
                data_date=date(2026, 6, 10), fetched_at=now_jst(),
            ),
        )
        monkeypatch.setattr(edinet_codelist, "parse_codelist", lambda *a, **k: [rec])
        # ① には 4 銘柄が既存 → 取得1件は 50%(=2)未満なので安全弁が作動
        def fake_query(self, db_id, **kwargs):
            return [
                {"id": f"p-{c}",
                 "properties": {S.MASTER_PROP_CODE: {"rich_text": [{"plain_text": c}]}}}
                for c in ("7203", "9001", "9002", "9003")
            ]
        monkeypatch.setattr(NotionClient, "query_database", fake_query)

        master_sync.main(["--dry-run"], env=_env(tmp_path))
        client = captured_clients[0]
        # 上場廃止マーク (listed=False) が一切発行されない
        mass_delist = [
            o for o in client.ops
            if o.op == "update_page"
            and o.payload["properties"].get(S.MASTER_PROP_LISTED) == {"checkbox": False}
        ]
        assert mass_delist == []
        # ⑦ に失敗が記録される（黙って中止せず可視化 §3-2）
        logs = _ops_with_prop(client, S.JOB_PROP_NAME)
        assert logs[0].payload["properties"][S.JOB_PROP_FAILED]["number"] >= 1


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

    def test_split_jump_flags_needs_review(self, monkeypatch, tmp_path, captured_clients):
        """直近に分割/併合の不連続がある銘柄は ② データ品質=要確認 (§ Phase1)。"""
        dates = pd.date_range("2025-01-01", periods=60, freq="D")
        series = [3000.0] * 30 + [1000.0] * 30  # 1→3分割相当の不連続
        df = pd.DataFrame(
            {"Date": dates, "Open": series, "High": series, "Low": series,
             "Close": series, "Volume": [1_000_000.0] * 60}
        )
        csv_bytes = df.to_csv(index=False).encode()

        def fake_batch(settings, codes, period="2y", **kwargs):
            artifact = save_raw(
                csv_bytes, source=Source.YFINANCE, datatype="daily_prices_batch",
                scope="ALL", data_date=date(2025, 3, 1), url="fixture://split",
                ext="csv", license_tag=source_license(Source.YFINANCE),
                base_dir=settings.raw_data_dir,
            )
            return artifact, {"7203": df}, []

        monkeypatch.setattr(yfinance_prices, "fetch_daily_batch", fake_batch)
        code = prices_daily.main(
            ["--dry-run", "--codes", "7203", "--skip-valuation"], env=_env(tmp_path)
        )
        assert code == 0
        client = captured_clients[0]
        price_ops = _ops_with_prop(client, S.PRICE_PROP_CLOSE)
        assert len(price_ops) == 1
        quality = price_ops[0].payload["properties"][S.PROP_QUALITY]["select"]["name"]
        assert quality == "要確認"  # 自動調整はせず人間判断に委ねる (§3-5)

    def test_notion_read_failure_degrades_without_crash(
        self, monkeypatch, tmp_path, captured_clients
    ):
        """① relation read(load_stock_master_map)が Notion 障害で失敗しても、
        ジョブは crash せず ② を書き切る（双方向フェールセーフの read 側 §3-2）。
        Notion 断でもローカル PG へ ② を残せるようにするための degrade。"""
        csv_bytes = fixture_path("transform/yfinance_7203T_daily.csv").read_bytes()
        df = pd.read_csv(fixture_path("transform/yfinance_7203T_daily.csv"))

        def fake_batch(settings, codes, period="2y", **kwargs):
            artifact = save_raw(
                csv_bytes, source=Source.YFINANCE, datatype="daily_prices_batch",
                scope="ALL", data_date=date(2026, 6, 10), url="fixture://prices/7203",
                ext="csv", license_tag=source_license(Source.YFINANCE),
                base_dir=settings.raw_data_dir,
            )
            return artifact, {"7203": df}, []

        def selective_query(self, db_id, *args, **kwargs):
            # ① stock_master への relation read のみ Notion 障害を模す
            # (⑤ SHA256 重複クエリ等は正常＝空リストで返す)
            if "stock_master" in str(db_id):
                raise RuntimeError("テスト: ① への Notion read 全断")
            return []

        monkeypatch.setattr(yfinance_prices, "fetch_daily_batch", fake_batch)
        monkeypatch.setattr(NotionClient, "query_database", selective_query)
        code = prices_daily.main(
            ["--dry-run", "--codes", "7203", "--skip-valuation"], env=_env(tmp_path)
        )
        assert code == 0  # ① read 失敗でも crash しない（degrade して続行）
        client = captured_clients[0]
        # master_id 解決不能でも ② は書かれる（relation 空で継続）
        assert len(_ops_with_prop(client, S.PRICE_PROP_RSI14)) == 1


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


class TestReconcileWeekly:
    """第2ソース(stooq)による②終値突合の純粋ロジック検証 (§3-5)。"""

    def test_stooq_close_on_matches_date(self):
        df = pd.DataFrame(
            {"Date": [date(2026, 3, 10), date(2026, 3, 11)], "Close": [3000.0, 3100.0]}
        )
        assert reconcile_weekly.stooq_close_on(df, date(2026, 3, 11)) == 3100.0

    def test_stooq_close_on_absent_date_returns_none(self):
        df = pd.DataFrame({"Date": [date(2026, 3, 10)], "Close": [3000.0]})
        assert reconcile_weekly.stooq_close_on(df, date(2026, 3, 11)) is None
        # 空フレーム・基準日 None も None（捏造しない §3-1）
        assert reconcile_weekly.stooq_close_on(pd.DataFrame(), date(2026, 3, 11)) is None

    def test_build_reconcile_inputs_flags_only_deviation(self):
        from jp_stock_pipeline.transform import reconcile

        # ② スナップショット: (page_id, code, close, data_date)
        snapshot = [
            ("p1", "7203", 3000.0, date(2026, 3, 11)),  # stooq 3100 → 乖離
            ("p2", "6758", 2000.0, date(2026, 3, 11)),  # stooq 2001 → 閾内
            ("p3", "9999", 100.0, date(2026, 3, 11)),   # stooq 無し → 対象外
        ]
        stooq_by_code = {
            "7203": pd.DataFrame({"Date": [date(2026, 3, 11)], "Close": [3100.0]}),
            "6758": pd.DataFrame({"Date": [date(2026, 3, 11)], "Close": [2001.0]}),
        }
        ours, theirs_df, skipped = reconcile_weekly.build_reconcile_inputs(
            snapshot, stooq_by_code
        )
        assert skipped == ["9999"]  # stooq 未取得は突合対象外 (§3-1)
        discrepancies = reconcile.reconcile_prices(theirs_df, ours, close_col="Close")
        assert [d.code for d in discrepancies] == ["7203"]

    def test_build_reconcile_inputs_date_mismatch_skipped(self):
        """② 基準日と同一日の stooq 行が無ければ突合しない (§3-3 実在しない対を作らない)。"""
        snapshot = [("p1", "7203", 3000.0, date(2026, 3, 11))]
        stooq_by_code = {
            "7203": pd.DataFrame({"Date": [date(2026, 3, 10)], "Close": [3100.0]})
        }
        ours, theirs_df, skipped = reconcile_weekly.build_reconcile_inputs(
            snapshot, stooq_by_code
        )
        assert skipped == ["7203"]
        assert ours == []

    def test_dry_run_completes_with_empty_snapshot(self, tmp_path):
        """dry-run の合成DB IDでは②クエリが空 → 突合対象0で正常完走 (§10・ネットワーク非依存)。"""
        code = reconcile_weekly.main(["--dry-run"], env=_env(tmp_path))
        assert code == 0


class TestWorkflowCrons:
    """§8.2 スケジュール (JST) と cron (UTC) の対応検証。"""

    EXPECTED = {
        "master_sync": "0 21 1 * *",
        "prices_daily": "30 10 * * 1-5",
        "tdnet_hourly": "0 0-10 * * 1-5",
        "edinet_daily": "0 12 * * 1-5",
        "reconcile_weekly": "0 0 * * 6",
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
