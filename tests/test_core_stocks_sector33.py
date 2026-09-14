"""EDINET「提出者業種」で D1 `core_stocks.sector33` を埋める経路のテスト。

守るべき不変条件（どれか 1 つでも崩れると本番で気づけない壊れ方をする）:
- **`updated_at` を SET しない。** `core_stocks` の鮮度は `MAX(updated_at)` で
  測っており、月次の充填が進めると kabulab-cf の universe sync が死んでも SLO が
  鳴らなくなる
- **サロゲートキー `id` と既存列を SET しない**（G-core-1。14 子表が `id` を参照）
- **コードリストに居ない銘柄を触らない**（一時的な欠落で既存値を NULL に潰さない）
- **差分が無ければ 1 文も書かない**（月次の書き込みを通常 0 にする）
- `--dry-run` / `--limit` では書かない
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from _doubles import SqliteD1
from conftest import fixture_path

from jp_stock_pipeline.cloud_store import core_stocks as cs
from jp_stock_pipeline.cloud_store.d1 import MAX_BOUND_PARAMS
from jp_stock_pipeline.collectors import edinet_codelist
from jp_stock_pipeline.contracts.sector33 import TSE_SECTOR33_NAMES, normalize_sector33
from jp_stock_pipeline.jobs import master_sync
from jp_stock_pipeline.licensing import source_license
from jp_stock_pipeline.models import Source
from jp_stock_pipeline.rawstore import save_raw

from test_core_stocks_migrate import APPLIED_DDL, PROD_DDL, set_targets

_D1_ENV = {
    "CF_ACCOUNT_ID": "acct",
    "CF_API_TOKEN": "token",
    "CF_D1_DATABASE_ID": "db",
}


class TestNormalizeSector33:
    def test_東証33業種はちょうど33件で重複しない(self) -> None:
        assert len(TSE_SECTOR33_NAMES) == 33
        assert len(set(TSE_SECTOR33_NAMES)) == 33

    @pytest.mark.parametrize("name", TSE_SECTOR33_NAMES)
    def test_33業種の名称はそのまま通す(self, name: str) -> None:
        assert normalize_sector33(name) == name

    def test_倉庫の表記ゆれを東証へ揃える(self) -> None:
        """EDINET は `倉庫・運輸関連`、東証は `倉庫・運輸関連業`（34 社。実測）。"""
        assert normalize_sector33("倉庫・運輸関連") == "倉庫・運輸関連業"

    def test_外国法人_組合は_None(self) -> None:
        """33業種ではない。素通しすると公開面に語彙外の業種が 1 件湧く。"""
        assert normalize_sector33("外国法人・組合") is None

    @pytest.mark.parametrize("raw", ["  情報・通信業", "情報・通信業　", "\t情報・通信業\n"])
    def test_前後の空白を落とす(self, raw: str) -> None:
        assert normalize_sector33(raw) == "情報・通信業"

    @pytest.mark.parametrize("raw", [None, "", "   ", "情報通信業", "その他"])
    def test_未知や欠損は_None(self, raw) -> None:
        """近い業種へ推測で寄せない（§3-1）。"""
        assert normalize_sector33(raw) is None

    def test_実フィクスチャの業種は33業種か外国法人_組合だけ(self) -> None:
        """実測の前提（表記ゆれは倉庫の 1 件だけ）が崩れたら気づけるようにする。"""
        records = edinet_codelist.parse_codelist(
            fixture_path("edinet/Edinetcode.zip").read_bytes(), raw_page_id=None
        )
        dropped = {r.sector33 for r in records if normalize_sector33(r.sector33) is None}
        assert dropped <= {"外国法人・組合"}, dropped


class TestBuildSector33Updates:
    def _changes(self, n: int) -> dict[str, str | None]:
        names = list(TSE_SECTOR33_NAMES) + [None]
        return {f"{1000 + i:04d}": names[i % len(names)] for i in range(n)}

    def test_updated_at_を_SET_しない(self) -> None:
        """鮮度 `MAX(updated_at)` を進めるのは kabulab-cf の universe sync だけにする。"""
        statements = cs.build_sector33_updates(self._changes(500))
        assert statements
        for sql, _ in statements:
            assert "updated_at" not in sql.lower(), sql

    def test_SET_するのは_sector33_だけ(self) -> None:
        """G-core-1: サロゲートキーも既存列も SET 句に現れない。"""
        for sql, _ in cs.build_sector33_updates(self._changes(500)):
            targets = set_targets(sql)
            assert targets == {"sector33"}, sql
            assert not (targets & cs.PROTECTED_COLUMNS)
            assert "id" not in targets

    def test_1文のバインド数が上限内(self) -> None:
        statements = cs.build_sector33_updates(self._changes(3818))
        for sql, params in statements:
            assert len(params) <= MAX_BOUND_PARAMS
            assert sql.count("?") == len(params)

    def test_全行が漏れも重複もなく1回ずつ現れる(self) -> None:
        changes = self._changes(3818)
        seen: dict[str, str | None] = {}
        for _, params in cs.build_sector33_updates(changes):
            value, *codes = params
            for code in codes:
                assert code not in seen
                seen[code] = value
        assert seen == changes

    def test_差分が無ければ0文(self) -> None:
        assert cs.build_sector33_updates({}) == []


class TestPlanSector33Updates:
    def test_変わる行だけを返す(self) -> None:
        current = [
            {"code": "7203", "sector33": "輸送用機器"},  # 同じ → 書かない
            {"code": "9301", "sector33": None},  # 倉庫 → 正規化して書く
            {"code": "6758", "sector33": "サービス業"},  # 違う → 書く
        ]
        codelist = [
            ("72030", "輸送用機器"),
            ("93010", "倉庫・運輸関連"),
            ("67580", "電気機器"),
        ]
        assert cs.plan_sector33_updates(current, codelist) == {
            "9301": "倉庫・運輸関連業",
            "6758": "電気機器",
        }

    def test_コードリストに無い銘柄を触らない(self) -> None:
        """REIT やコードリストの一時的な欠落で既存値を NULL に潰さない。"""
        current = [
            {"code": "1201", "sector33": "不動産業"},  # コードリストに居ない
            {"code": "7203", "sector33": None},
        ]
        changes = cs.plan_sector33_updates(current, [("72030", "輸送用機器")])
        assert changes == {"7203": "輸送用機器"}

    def test_コードリストに居て業種が33業種外なら_NULL_を書く(self) -> None:
        current = [{"code": "9999", "sector33": "サービス業"}]
        assert cs.plan_sector33_updates(current, [("99990", "外国法人・組合")]) == {
            "9999": None
        }

    def test_差分が無ければ空(self) -> None:
        current = [{"code": "7203", "sector33": "輸送用機器"}]
        assert cs.plan_sector33_updates(current, [("72030", "輸送用機器")]) == {}

    def test_末尾が0でない5桁コードは別証券なので使わない(self) -> None:
        """`25935`（伊藤園優先株）を `2593`（普通株）へ付け替えない。"""
        current = [{"code": "2593", "sector33": None}]
        assert cs.plan_sector33_updates(current, [("25935", "食料品")]) == {}


# --- ジョブ配線 -------------------------------------------------------------


class _FakeD1(SqliteD1):
    """sqlite 裏打ちの `D1Store` + 本番形 `core_stocks` と sector33 seed。"""

    def __init__(self, rows: list[tuple[str, str | None]], *, fail_on_update: bool = False):
        super().__init__(fail_on_prefix=("UPDATE",) if fail_on_update else ())
        self.con.executescript(PROD_DDL)
        for stmt in APPLIED_DDL:
            self.con.execute(stmt)
        for code, sector33 in rows:
            self.con.execute(
                "INSERT INTO core_stocks (code, name, market, sector, updated_at, sector33)"
                " VALUES (?, ?, 'プライム（内国株式）', NULL, 1000, ?)",
                [code, f"銘柄{code}", sector33],
            )
        self.con.commit()

    def values(self) -> dict[str, tuple[str | None, int]]:
        return {
            code: (sector33, updated_at)
            for code, sector33, updated_at in self.con.execute(
                "SELECT code, sector33, updated_at FROM core_stocks"
            )
        }


def _record(code: str, sector33: str | None):
    return SimpleNamespace(code=code, sector33=sector33)


def _ctx(*, dry_run: bool, d1: bool = True):
    from jp_stock_pipeline.config import CloudStoreSettings

    cloud = CloudStoreSettings(
        cf_account_id="acct" if d1 else None,
        cf_api_token="token" if d1 else None,
        d1_database_id="db" if d1 else None,
    )
    failures: list[tuple[str, str]] = []
    return SimpleNamespace(
        settings=SimpleNamespace(cloud_store=cloud, dry_run=dry_run),
        args=SimpleNamespace(limit=None),
        failures=failures,
        add_failure=lambda code, reason="": failures.append((code, reason)),
        add_success=lambda n=1: None,
    )


@pytest.fixture
def fake_d1(monkeypatch):
    holder: dict[str, _FakeD1] = {}

    def install(rows, **kwargs) -> _FakeD1:
        store = _FakeD1(rows, **kwargs)
        holder["store"] = store
        monkeypatch.setattr(master_sync, "D1Store", lambda *a, **k: store)
        return store

    return install


class TestSyncSector33:
    def test_差分だけ書き_updated_at_は動かない(self, fake_d1) -> None:
        store = fake_d1([("7203", "輸送用機器"), ("9301", None), ("1201", "不動産業")])
        ctx = _ctx(dry_run=False)
        master_sync._sync_sector33(
            ctx,
            [_record("7203", "輸送用機器"), _record("9301", "倉庫・運輸関連")],
        )
        assert ctx.failures == []
        assert store.values() == {
            "7203": ("輸送用機器", 1000),
            "9301": ("倉庫・運輸関連業", 1000),
            "1201": ("不動産業", 1000),  # コードリストに居ないので触らない
        }
        assert len(store.write_sql) == 1

    def test_2回目は0文(self, fake_d1) -> None:
        store = fake_d1([("9301", None)])
        records = [_record("9301", "倉庫・運輸関連")]
        master_sync._sync_sector33(_ctx(dry_run=False), records)
        before = len(store.write_sql)
        master_sync._sync_sector33(_ctx(dry_run=False), records)
        assert len(store.write_sql) == before

    def test_dry_run_は書かない(self, fake_d1) -> None:
        store = fake_d1([("9301", None)])
        ctx = _ctx(dry_run=True)
        master_sync._sync_sector33(ctx, [_record("9301", "倉庫・運輸関連")])
        assert store.write_sql == []
        assert store.sql_log == [cs.SECTOR33_SNAPSHOT_SQL]
        assert store.values()["9301"] == (None, 1000)

    def test_D1_未設定なら触らず失敗にもしない(self, monkeypatch) -> None:
        """主目的は Notion ① / ローカル ① の同期。D1 が無い環境でも従来どおり成功させる。"""

        def boom(*a, **k):
            raise AssertionError("D1 未設定なのに D1Store を作った")

        monkeypatch.setattr(master_sync, "D1Store", boom)
        ctx = _ctx(dry_run=False, d1=False)
        master_sync._sync_sector33(ctx, [_record("9301", "倉庫・運輸関連")])
        assert ctx.failures == []

    def test_D1_の失敗は記録して例外を投げない(self, fake_d1) -> None:
        """Notion / ローカルの同期（この後の処理とジョブログ）を止めない。"""
        fake_d1([("9301", None)], fail_on_update=True)
        ctx = _ctx(dry_run=False)
        master_sync._sync_sector33(ctx, [_record("9301", "倉庫・運輸関連")])
        assert ctx.failures and ctx.failures[0][0] == "core_stocks.sector33"


class TestMasterSyncWiring:
    """`run_job` 経由で dry-run / --limit の分岐を通す（D1 資格情報あり）。"""

    def _patch_fetch(self, monkeypatch) -> None:
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

    def _env(self, tmp_path) -> dict[str, str]:
        return {"RAW_DATA_DIR": str(tmp_path / "raw"), **_D1_ENV}

    def test_dry_run_は読むだけで書かない(self, monkeypatch, tmp_path, fake_d1) -> None:
        self._patch_fetch(monkeypatch)
        store = fake_d1([("7203", None), ("9301", None)])
        assert master_sync.main(["--dry-run"], env=self._env(tmp_path)) == 0
        assert store.sql_log == [cs.SECTOR33_SNAPSHOT_SQL]
        assert store.write_sql == []

    def test_limit_指定では読みも書きもしない(self, monkeypatch, tmp_path, fake_d1) -> None:
        self._patch_fetch(monkeypatch)
        store = fake_d1([("7203", None)])
        assert master_sync.main(["--dry-run", "--limit", "5"], env=self._env(tmp_path)) == 0
        assert store.sql_log == []
