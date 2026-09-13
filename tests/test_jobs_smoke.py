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
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from conftest import fixture_path

from jp_stock_pipeline.collectors import (
    edinet,
    edinet_codelist,
    tdnet_yanoshin,
    yfinance_prices,
)
from jp_stock_pipeline.jobs import (
    edinet_daily,
    master_sync,
    prices_daily,
    reconcile_weekly,
    runner,
    tdnet_hourly,
)
from jp_stock_pipeline.licensing import LicenseTag, source_license
from jp_stock_pipeline.models import JST, Source
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

    def _patch_history_fixtures(self, monkeypatch, tmp_path):
        """履歴子DB系テストの共通差し替え。"""
        from jp_stock_pipeline.notion import price_history

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

        monkeypatch.setattr(yfinance_prices, "fetch_daily_batch", fake_batch)
        monkeypatch.setattr(
            price_history,
            "load_stock_master_state",
            lambda client, settings: {
                "7203": price_history.StockMasterState(page_id="master-7203")
            },
        )

    def test_history_child_db_is_off_by_default(
        self, monkeypatch, tmp_path, captured_clients
    ):
        """既定では履歴子DBを作らない（Notion 日次リクエストの 71% を占めるため）。

        同じ日足とテクニカルは R2 daily/{code}.json（10年）とローカルPG prices に
        あり、子DBは3番目のコピーになる。復活させたいときは --enable-history。
        """
        self._patch_history_fixtures(monkeypatch, tmp_path)
        code = prices_daily.main(
            ["--dry-run", "--codes", "7203", "--skip-valuation"], env=_env(tmp_path)
        )
        assert code == 0
        client = captured_clients[0]
        assert [op for op in client.ops if op.op == "create_database"] == []
        # ② 本体は従来どおり書かれる
        assert len(_ops_with_prop(client, S.PRICE_PROP_RSI14)) == 1

    def test_history_child_db_appended_when_explicitly_enabled(
        self, monkeypatch, tmp_path, captured_clients
    ):
        """--enable-history を渡せば従来どおり子DBを作り同日分を追記する。"""
        from jp_stock_pipeline.notion import price_history

        del price_history  # 共通差し替えで使うのでここでは参照しない
        self._patch_history_fixtures(monkeypatch, tmp_path)
        code = prices_daily.main(
            ["--dry-run", "--codes", "7203", "--skip-valuation", "--enable-history"],
            env=_env(tmp_path),
        )
        assert code == 0
        client = captured_clients[0]
        db_ops = [op for op in client.ops if op.op == "create_database"]
        assert len(db_ops) == 1
        assert db_ops[0].payload["parent"]["page_id"] == "master-7203"
        assert db_ops[0].payload["title"][0]["text"]["content"] == S.HISTORY_DB_TITLE
        hist_rows = [
            op for op in client.ops
            if op.op == "create_page"
            and S.HISTORY_PROP_DATE_TITLE in (op.payload.get("properties") or {})
        ]
        assert len(hist_rows) == 1
        assert S.PRICE_PROP_RSI14 in hist_rows[0].payload["properties"]


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

    def test_prefetch_maps_make_queries_constant_not_per_disclosure(
        self, monkeypatch, tmp_path, captured_clients
    ):
        """①/④ 事前マップで Notion query が開示件数に比例しない (§8.3 30分cap対策)。

        従来は開示ごとに ① find + ④ dedup の 2 query。事前マップ化で query は
        「① マップ + ④ マップ + ⑤原本SHA256重複(原本数)」のみ＝開示件数に非比例。
        """
        from jp_stock_pipeline.collectors import tdnet_yanoshin
        from jp_stock_pipeline.http import FetchError
        from jp_stock_pipeline.notion.client import NotionClient

        payload_bytes = fixture_path("tdnet/yanoshin_list_recent.json").read_bytes()
        payload = json.loads(payload_bytes)

        def fake_list(settings, target="recent", limit=300):
            artifact = save_raw(
                payload_bytes, source=Source.TDNET, datatype="tdnet_list",
                scope=str(target), data_date=date(2026, 6, 10),
                url="fixture://tdnet/recent", ext="json",
                license_tag=source_license(Source.TDNET), base_dir=settings.raw_data_dir,
            )
            records = tdnet_yanoshin.parse_list_payload(payload, fetched_at=artifact.fetched_at)
            return artifact, records

        monkeypatch.setattr(tdnet_yanoshin, "list_disclosures", fake_list)
        monkeypatch.setattr(tdnet_hourly, "fetch", lambda url, **k: (_ for _ in ()).throw(
            FetchError("net cut")))

        per_db_queries: dict[str, int] = {}

        def counting_query(self, db_id, **kwargs):
            per_db_queries[str(db_id)] = per_db_queries.get(str(db_id), 0) + 1
            return []  # 空 = 全 create 経路

        monkeypatch.setattr(NotionClient, "query_database", counting_query)
        # フィクスチャ開示は全て 2026-06-10。--date を一致させ全件 in-window にすることで
        # ④ date-scoped マップが信用され per-record 検索が消える（日付不一致時は安全側で
        # per-record 検索にフォールバックするのが正しい挙動）。
        code = tdnet_hourly.main(["--dry-run", "--date", "2026-06-10"], env=_env(tmp_path))
        assert code == 0
        client = captured_clients[0]
        n_disc = len(_ops_with_prop(client, S.DISC_PROP_DOC_ID))
        assert n_disc > 1  # 複数開示があることを前提に「非比例」を意味あるものにする
        # どの DB も query は最大1回（① マップ/④ マップ/⑤原本SHA256 各1回）。per-record
        # 検索が残っていれば db-disc や db-master が n_disc 回に膨らむ。最大1で非比例を保証。
        # 従来は開示ごとに ① find + ④ dedup = 2×n_disc query だった。
        assert per_db_queries  # 何らかの query はある（マップロード）
        assert max(per_db_queries.values()) == 1


class TestEdinetDailyTargetDate:
    """対象日は「起動時刻の JST 日付」ではなく cron の予定日に一致する。

    2026-08-27〜09-10 に、GitHub Actions のスケジュール遅延 (実測 +3.5h〜+9.5h) で
    起動が翌日 JST へずれ、まだ提出 0 件の「翌日」の一覧を 11 営業日連続で取得して
    processed=0 / failed=0 の「成功」を出し続けた。その再発防止。
    """

    @pytest.mark.parametrize(
        ("started", "expected"),
        [
            # 定刻 (12:00Z = 21:00 JST) 起動 → 当日が対象
            (datetime(2026, 9, 10, 21, 0, tzinfo=JST), date(2026, 9, 10)),
            # 実測 run 34496419755: 2026-09-10T15:33Z = 09-11 00:33 JST → 対象は 09-10
            (datetime(2026, 9, 11, 0, 33, tzinfo=JST), date(2026, 9, 10)),
            # 実測 run 33118324540: 2026-08-27T21:28Z = 08-28 06:28 JST → 対象は 08-27
            (datetime(2026, 8, 28, 6, 28, tzinfo=JST), date(2026, 8, 27)),
            # 予定時刻の直前 (遅延 24h 手前) までは前日へ吸収される
            (datetime(2026, 9, 11, 20, 59, tzinfo=JST), date(2026, 9, 10)),
        ],
    )
    def test_default_target_date_follows_schedule_not_start_time(self, started, expected):
        assert edinet_daily.default_target_date(started) == expected

    def test_empty_document_list_is_recorded_as_failure(
        self, monkeypatch, tmp_path, captured_clients
    ):
        """一覧 0 件を「成功」で黙って終えない (§3-2 欠損を隠さない)。

        取得単位が 1 件も成立していないので runner の規則どおり「失敗」= 終了コード 1。
        国民の祝日は EDINET 提出が 0 件のため、この経路で毎回赤くなるのは想定内で、
        エラー通知が未実装の現状ではこれが唯一の生存確認を兼ねる (README に明記)。
        """
        payload = b'{"metadata": {"status": "200"}, "results": []}'

        def fake_list(settings, target_date):
            artifact = save_raw(
                payload,
                source=Source.EDINET,
                datatype="documents_list",
                scope="ALL",
                data_date=target_date,
                url="fixture://edinet/empty",
                ext="json",
                license_tag=source_license(Source.EDINET),
                base_dir=settings.raw_data_dir,
            )
            return artifact, []

        monkeypatch.setattr(edinet, "list_documents", fake_list)
        code = edinet_daily.main(
            ["--dry-run", "--date", "2026-09-10"], env=_env(tmp_path)
        )
        assert code == 1  # 黙って success で終わらない（11営業日の無言欠測の再発防止）

        client = captured_clients[0]
        job_rows = _ops_with_prop(client, S.JOB_PROP_FAILED)
        assert job_rows, "⑦ ジョブログ行が記録されていない"
        props = job_rows[-1].payload["properties"]
        assert props[S.JOB_PROP_FAILED]["number"] == 1
        assert props[S.JOB_PROP_STATUS]["select"]["name"] == "失敗"
        failed_codes = props[S.JOB_PROP_FAILED_CODES]["rich_text"][0]["text"]["content"]
        assert "EDINET一覧_2026-09-10" in failed_codes


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

    def test_stooq_disabled_early_exits_without_snapshot_or_fetch(
        self, monkeypatch, tmp_path, captured_clients
    ):
        """stooq 無効(既定)時は ② スナップショット読みも fetch ループも行わず即終了する
        (成果ゼロの 46分タイムアウトを排除 §3-2)。"""
        from jp_stock_pipeline.collectors import stooq_prices

        def _no_snapshot(*a, **k):
            raise AssertionError("stooq 無効時に ② スナップショット読みへ到達してはならない")

        def _no_fetch(*a, **k):
            raise AssertionError("stooq 無効時に fetch_daily へ到達してはならない")

        monkeypatch.setattr(reconcile_weekly, "load_price_snapshot", _no_snapshot)
        monkeypatch.setattr(stooq_prices, "fetch_daily", _no_fetch)
        # STOOQ_ENABLED を設定しない env = 既定 False
        code = reconcile_weekly.main(["--dry-run"], env=_env(tmp_path))
        assert code == 0  # 突合スキップは正常完了（設定状態でCIを赤にしない）


class TestWorkflowCrons:
    """§8.2 スケジュール (JST) と cron (UTC) の対応検証。"""

    EXPECTED = {
        "master_sync": "0 21 1 * *",
        "prices_daily": "30 10 * * 1-5",
        "tdnet_hourly": "0 0-10 * * 1-5",
        "edinet_daily": "0 12 * * 1-5",
        "reconcile_weekly": "0 0 * * 6",
        "export_weekly": "0 0 * * 0",
        "supply_daily": "17 3 * * 1-5",
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

    def test_margin_weekly_schedule_is_deliberately_disabled(self):
        """margin_weekly の cron は切替日まで**無効のまま**でなければならない。

        この writer は現在 kabulab-cf の vwap-ingest.yml が担っている。
        両方が動くと二重 writer になり、移行規則 M1/M4 に違反する。
        切替（2026-09-26 に kabulab-cf 側を止め、2026-09-28 から stockStock）が
        済むまで、うっかり有効化されないようテストで固定する。
        """
        text = (
            Path(__file__).parent.parent / ".github" / "workflows" / "margin_weekly.yml"
        ).read_text(encoding="utf-8")
        active = [
            ln for ln in text.splitlines()
            if re.match(r'\s*-\s*cron:', ln)  # コメント行(# - cron:)は拾わない
        ]
        assert active == [], f"margin_weekly の cron が有効化されている: {active}"
        assert "workflow_dispatch" in text  # 手動実行と check-only は可能
        assert "jp_stock_pipeline.jobs.margin_weekly" in text

    def test_yutai_backup_is_manual_only(self):
        """yutai_backup は定期実行しない（移行 P3 の一回きりの保険）。

        毎日回すと R2 に同じ中身の索引追記が積み上がるだけで、元データ
        (kabulab-cf の D1) を無駄に走査する。内容が変わったときに手で流す。
        """
        text = (
            Path(__file__).parent.parent / ".github" / "workflows" / "yutai_backup.yml"
        ).read_text(encoding="utf-8")
        active = [
            ln for ln in text.splitlines()
            if re.match(r'\s*-\s*cron:', ln)
        ]
        assert active == [], f"yutai_backup に cron がある: {active}"
        assert "workflow_dispatch" in text
        assert "jp_stock_pipeline.jobs.yutai_backup" in text
        # 退避先は公開 Worker が bind していないバケットであること（第0層の防御）
        assert "jp-stock-supply" in text

    def test_ci_runs_pytest(self):
        text = (
            Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        assert "pytest" in text
        assert "nix develop" in text


class TestOpsCheckIssueLifecycle:
    """ops_check.yml は赤で SLO 違反 Issue を立て、4 層すべて緑で閉じる。

    閉じる条件に層の outcome を 1 つでも書き忘れると、その層を見ないまま Issue を
    閉じる。層を足したときの書き忘れを、`id:` の一覧との突き合わせで捕まえる。
    pyyaml は直接依存ではないので、周囲のテストと同じく本文を文字列で読む。
    """

    PATH = Path(__file__).parent.parent / ".github" / "workflows" / "ops_check.yml"
    CLOSE_STEP = "- name: SLO が緑に戻ったら SLO 違反 Issue を閉じる"

    def _text(self) -> str:
        return self.PATH.read_text(encoding="utf-8")

    def _close_block(self, text: str) -> str:
        assert self.CLOSE_STEP in text, "緑で Issue を閉じるステップが無い"
        block = text.split(self.CLOSE_STEP, 1)[1]
        # 次のステップ（あれば）の手前まで
        return re.split(r"\n\s*- name:", block, maxsplit=1)[0]

    def test_close_requires_every_layer_success(self):
        text = self._text()
        ids = re.findall(r"^\s*id:\s*(\w+)\s*$", text, re.MULTILINE)
        assert set(ids) == {"probe", "judge", "drift", "license"}
        cond = self._close_block(text).split("env:", 1)[0]
        assert "success()" in cond
        for step_id in ids:
            assert f"steps.{step_id}.outcome == 'success'" in cond, step_id

    def test_title_is_shared_and_matched_exactly(self):
        text = self._text()
        # 立てる側と閉じる側が別々に文字列を持つと、片方だけ書き換わって黙る
        assert text.count("[SLO違反] データの鮮度またはジョブ結果") == 1
        assert "SLO_ISSUE_TITLE:" in text
        close = self._close_block(text)
        assert "select(.title == env.SLO_ISSUE_TITLE)" in close
        assert "--search" not in close  # 部分一致は無関係な Issue を閉じ得る
        assert "gh issue close" in close
        create = text.split("- name: SLO 違反を Issue に出す", 1)[1].split(
            self.CLOSE_STEP, 1
        )[0]
        assert "select(.title == env.SLO_ISSUE_TITLE)" in create
        assert '--title "$SLO_ISSUE_TITLE"' in create


class TestEdinetLargeHolding:
    """大量保有報告書 (350/360) の取りこぼし修正。

    350/360 は**保有者が提出する**ため secCode が入らない。実データ（直近12日分の
    一覧）で 350 が 992 件中 956 件、360 が 412 件中 405 件で secCode が空だった。
    secCode だけで絞っていたため 96〜98% を取りこぼしていた。
    対象会社は issuerEdinetCode（実測 956/956 = 100% 充足）から解決する。
    """

    # 実レスポンスから採った 1 件（docID/コードは実値、氏名は構造確認のため保持）
    REAL_350 = {
        "docID": "S100Y8QP",
        "secCode": None,
        "edinetCode": "E41686",
        "issuerEdinetCode": "E04369",
        "subjectEdinetCode": None,
        "docTypeCode": "350",
        "docDescription": "変更報告書（特例対象株券等）",
        "submitDateTime": "2026-09-10 15:30",
    }
    REAL_120 = {
        "docID": "S100ABCD",
        "secCode": "72030",
        "issuerEdinetCode": None,
        "docTypeCode": "120",
        "submitDateTime": "2026-09-10 15:30",
    }

    def test_large_holding_without_seccode_is_now_collected(self):
        from jp_stock_pipeline.collectors import edinet

        assert edinet.has_sec_code(self.REAL_350) is False  # 従来はここで落ちていた
        assert edinet.is_target_document(self.REAL_350) is True
        assert edinet.has_identifiable_company(self.REAL_350) is True

    def test_issuer_edinet_code_is_extracted(self):
        from jp_stock_pipeline.collectors import edinet

        assert edinet.issuer_edinet_code(self.REAL_350) == "E04369"
        assert edinet.issuer_edinet_code(self.REAL_120) is None

    def test_ordinary_document_still_uses_seccode(self):
        from jp_stock_pipeline.collectors import edinet

        assert edinet.has_identifiable_company(self.REAL_120) is True

    def test_non_large_holding_without_seccode_is_still_excluded(self):
        """secCode も issuerEdinetCode も無い書類は対象外のまま。"""
        from jp_stock_pipeline.collectors import edinet

        doc = {"docID": "X", "docTypeCode": "120", "secCode": None}
        assert edinet.has_identifiable_company(doc) is False

    def test_issuer_code_is_not_used_for_non_large_holding(self):
        """有報に issuerEdinetCode があっても secCode の代わりにはしない。"""
        from jp_stock_pipeline.collectors import edinet

        doc = {"docID": "X", "docTypeCode": "120", "secCode": None,
               "issuerEdinetCode": "E04369"}
        assert edinet.has_identifiable_company(doc) is False

    def test_edinet_map_is_built_from_the_same_master_scan(self):
        """① の1回のスキャンから逆引きを作る（追加の API 呼び出しをしない）。"""
        from jp_stock_pipeline.notion import upsert

        pages = [{
            "id": "page-7203",
            "properties": {
                S.MASTER_PROP_CODE: {"rich_text": [{"plain_text": "7203"}]},
                S.MASTER_PROP_EDINET_CODE: {"rich_text": [{"plain_text": "E04369"}]},
            },
        }]
        assert upsert._master_map_from_pages(pages) == {"7203": "page-7203"}
        assert upsert._edinet_map_from_pages(pages) == {"E04369": "7203"}

    def test_master_page_without_edinet_code_is_skipped(self):
        from jp_stock_pipeline.notion import upsert

        pages = [{
            "id": "p",
            "properties": {S.MASTER_PROP_CODE: {"rich_text": [{"plain_text": "7203"}]}},
        }]
        assert upsert._edinet_map_from_pages(pages) == {}
