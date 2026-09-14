"""D1 の表区分（ライセンス地図の網羅性）のテスト。

本番 30 表 375 列のうち、行タグも列地図も無いのが 22 表 / 257 列（68.5%）
だった。しかも `license_tag` 列を物理的に持つ 8 表のうち、行が入って実際に
機能していたのは 3 表だけで、「行タグで判定する」という前提自体が大半の表で
成立していなかった。

ここで固定するのは次の 2 点:
- **網羅性の検査が行を 1 行も走査しない形になっていること**（`sqlite_master` の
  DDL だけで判定する。全表に `SUM(col IS NOT NULL)` を打つ案は 1 実行あたり
  約 6 万行の走査になり、設計書が「桁で下げる」と言っている対象と衝突する）
- **fail open / closed の境界**（未知の表は warning、宣言が実体から外れたら
  failure）。境界を逆にすると、対向リポジトリの migration ごとに毎日赤くなって
  誰も読まなくなるか、宣言が古いまま静かに腐る
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jp_stock_pipeline.cloud_store import core_stocks as cs
from jp_stock_pipeline.cloud_store import governance as G
from jp_stock_pipeline.cloud_store import schema as S
from jp_stock_pipeline.licensing import LicenseTag

CONTRACT = Path(__file__).parent / "fixtures" / "contracts" / "d1-license-map.json"

# 本番 `core_stocks` の実 DDL（P4a 適用後の 21 列）。`sqlite_master` は
# `ALTER TABLE ADD COLUMN` のときに保存済みの CREATE TABLE 文を書き換えるので、
# ALTER で足した 12 列もここに現れる。この前提が崩れると `ddl_columns` を
# 使う設計そのものが成立しない。
CORE_STOCKS_DDL = (
    "CREATE TABLE `core_stocks` ("
    "`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL, `code` text NOT NULL,"
    " `name` text NOT NULL, `market` text NOT NULL, `sector` text,"
    " `is_active` integer DEFAULT true NOT NULL,"
    " `is_yutai` integer DEFAULT false NOT NULL,"
    " `created_at` integer DEFAULT (unixepoch()) NOT NULL,"
    " `updated_at` integer DEFAULT (unixepoch()) NOT NULL,"
    " `instrument_type` TEXT, `sector33` TEXT, `sector17` TEXT, `edinet_code` TEXT,"
    " `listing_status` TEXT, `listing_date` TEXT, `delisting_date` TEXT,"
    " `license_tag` TEXT, `src_source` TEXT, `src_data_date` TEXT,"
    " `src_fetched_at` INTEGER, `quality` TEXT)"
)


def _observed() -> dict[str, str | None]:
    """宣言どおりの本番（=「問題なし」の基準状態）。"""
    out: dict[str, str | None] = {"core_stocks": CORE_STOCKS_DDL}
    for statement in S.SCHEMA_STATEMENTS:
        head = statement.strip()
        if head.upper().startswith("CREATE TABLE"):
            out[head.split("(", 1)[0].split()[-1]] = head
    for name in G.TABLE_LICENSE:
        out.setdefault(name, f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
    return out


class TestDdlColumnParser:
    def test_ALTER_で足した列も読める(self) -> None:
        """`PRAGMA table_info` を 30 回投げずに済む根拠。"""
        columns = G.ddl_columns(CORE_STOCKS_DDL)
        assert len(columns) == 21
        assert set(cs.NEW_COLUMNS) <= set(columns)
        assert columns[0] == "id"

    def test_型や既定値の中のカンマで分割しない(self) -> None:
        sql = (
            "CREATE TABLE t (a NUMERIC(10, 2), b integer DEFAULT (unixepoch()) NOT NULL,"
            " c TEXT)"
        )
        assert G.ddl_columns(sql) == ["a", "b", "c"]

    def test_表制約を列と間違えない(self) -> None:
        sql = (
            "CREATE TABLE t (a TEXT, b TEXT, PRIMARY KEY (a, b),"
            " FOREIGN KEY (a) REFERENCES u(x), UNIQUE (b), CHECK (a <> ''))"
        )
        assert G.ddl_columns(sql) == ["a", "b"]

    @pytest.mark.parametrize("sql", [None, "", "CREATE TABLE t", "CREATE TABLE t ("])
    def test_読めない_DDL_で例外を投げない(self, sql) -> None:
        assert G.ddl_columns(sql) == []


class TestCoverageDoesNotScanRows:
    def test_使う_SQL_が_sqlite_master_だけ(self) -> None:
        """判定の入力が「スキーマのカタログ」に閉じていること。"""
        assert "sqlite_master" in G.ALL_TABLES_SQL
        for banned in ("COUNT(", "SUM(", "JOIN", "core_stocks", "ir_disclosures"):
            assert banned not in G.ALL_TABLES_SQL, banned

    def test_判定は列名だけで行の値を受け取らない(self) -> None:
        """`coverage()` の入力は表名と DDL だけ。行を渡す口が無い。"""
        report = G.coverage(_observed())
        assert report.failures == ()


class TestFailOpenClosedBoundary:
    def test_宣言に無い表は警告で止めない(self) -> None:
        """対向リポジトリの migration ごとに毎日赤くする検査は読まれなくなる。"""
        observed = dict(_observed(), swing_new_table="CREATE TABLE swing_new_table (a TEXT)")
        report = G.coverage(observed)
        assert report.failures == ()
        assert any("swing_new_table" in w for w in report.warnings), report.warnings

    def test_宣言にあって本番に無い表は失敗させる(self) -> None:
        """宣言が実体から外れたまま残るのが「2 つの真実」の始まり。"""
        observed = _observed()
        del observed["swing_daily_ohlcv"]
        report = G.coverage(observed)
        assert report.failures != ()
        assert any("swing_daily_ohlcv" in f for f in report.failures)

    def test_内部表は未分類として報告しない(self) -> None:
        observed = dict(
            _observed(),
            d1_migrations="CREATE TABLE d1_migrations (id INTEGER)",
            _cf_KV="CREATE TABLE _cf_KV (k TEXT)",
        )
        report = G.coverage(observed)
        assert report.failures == ()
        assert not [w for w in report.warnings if "d1_migrations" in w or "_cf_KV" in w]

    def test_行タグ前提なのに列が無ければ失敗させる(self) -> None:
        """本番 8 表中 3 表しか機能していなかったのと同じ穴。"""
        observed = dict(_observed())
        observed["jss_supply_latest"] = "CREATE TABLE jss_supply_latest (code TEXT)"
        report = G.coverage(observed)
        assert report.failures != ()
        assert any("license_tag" in f for f in report.failures)

    def test_列地図にあって本番に無い列は失敗させる(self) -> None:
        observed = dict(_observed())
        observed["core_stocks"] = CORE_STOCKS_DDL.replace("`sector33` TEXT, ", "")
        report = G.coverage(observed)
        assert report.failures != ()
        assert any("sector33" in f for f in report.failures)

    def test_タグ未宣言の列は名前つきで警告する(self) -> None:
        """未宣言 = 公開してよいと決まっていない = 公開しない（漏れる方向に倒れない）。

        `core_stocks` の `id` / `is_active` / `is_yutai` / `created_at` /
        `updated_at` は kabulab-cf が書く既存列で、タグを決めるには派生元の判断が
        要る。推測で埋めず、決まっていないことを毎日見えるようにする。
        """
        report = G.coverage(_observed())
        undeclared = [w for w in report.warnings if "タグ未宣言の列" in w]
        assert len(undeclared) == 1, report.warnings
        for column in ("id", "is_active", "is_yutai", "created_at", "updated_at"):
            assert column in undeclared[0], column
        # 決めた列が「未宣言」に混ざっていないこと
        for column in ("sector33", "sector", "listing_status", "quality"):
            assert f"'{column}'" not in undeclared[0], column

    def test_2_つの地図が食い違ったら失敗させる(self, monkeypatch) -> None:
        """列地図を持つ表と `COLUMN_MAP` 区分は同じ集合でなければならない。"""
        monkeypatch.setitem(
            S.MIXED_LICENSE_COLUMNS, "swing_daily_ohlcv", {"close": LicenseTag.PERSONAL_ONLY}
        )
        report = G.coverage(_observed())
        assert report.failures != ()
        assert any("食い違う" in f for f in report.failures)


# 本番 D1 (`kabulab-cf`) の表名スナップショット。2026-09-13 に読み取り専用の
# `SELECT name FROM sqlite_master WHERE type='table'` で取得した 32 件から、
# 内部表 `_cf_KV` を除いた 31 件（`p_momentum` は同日 0011 で追加）のうち、
# DROP 済みの `finmath_price_snapshot` / `finmath_daily_ohlcv`（kabulab-cf
# drizzle/d1/0012）を除いた 29 件に、L-20 の `jss_notion_pages` を足した 30 件。
# `TABLE_LICENSE` の登録漏れを検出するために**宣言とは独立した観測値**として
# 置く（宣言から導くとテストが自明になる）。
PROD_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "core_stock_annual_financials", "core_stock_financials", "core_stocks",
        "ir_disclosures",
        "jss_column_license", "jss_dataset_freshness", "jss_financials",
        "jss_index_symbols", "jss_job_runs", "jss_notion_pages", "jss_raw_files",
        "jss_supply_latest",
        "jss_writer_claims", "jss_xbrl_documents", "jss_xbrl_elements",
        "otakara_stock_financials", "otakara_stock_scores", "p_momentum", "rsi_percentile",
        "swing_daily_ohlcv", "swing_entry_signals", "swing_market_context",
        "swing_sector_daily", "swing_stock_indicators", "swing_stock_screening",
        "yuho_documents", "yuho_order_facts", "yuho_overseas_facts",
        "yutai_benefits", "yutai_genres",
    }
)


class TestProductionSnapshot:
    """観測した本番 30 表すべてに区分があること。

    当初は「本番 PRAGMA を読めないので 27 表しか登録できない」としていたが、
    読み取り専用の `sqlite_master` 照会で残り 3 表（`swing_market_context` /
    `swing_sector_daily` / `yutai_genres`）の名前と DDL が取れた。登録漏れを
    残すと「地図に無い表」が恒久的に warning で出続け、未知の表を failure へ
    上げる道（module docstring の「更新手順」）が塞がる。
    """

    def test_観測した本番の表がすべて登録されている(self) -> None:
        missing = sorted(PROD_TABLE_NAMES - set(G.TABLE_LICENSE))
        assert missing == [], missing
        assert len(PROD_TABLE_NAMES) == G.OBSERVED_TABLE_COUNT

    def test_本番の表一覧に対して未知の表の警告が出ない(self) -> None:
        """`coverage()` を観測した表名で回して、warning が「未決」だけになること。"""
        observed = dict(_observed())
        for name in PROD_TABLE_NAMES:
            observed.setdefault(name, f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
        report = G.coverage(observed)
        assert report.failures == ()
        assert not [w for w in report.warnings if "地図に無い表" in w], report.warnings

    def test_JPX_の業種を持つ表を_personal_only_にしている(self) -> None:
        """`swing_sector_daily.sector` は `core_stocks.sector` と同じ JPX 33業種。

        Yahoo 派生だからではなく **JPX 由来の値を持つから** personal-only で
        なければならない（commercial-ok に倒すと公開面へ JPX の業種が出る）。
        """
        assert G.TABLE_LICENSE["swing_sector_daily"].tag is LicenseTag.PERSONAL_ONLY
        assert S.MIXED_LICENSE_COLUMNS["core_stocks"]["sector"] is LicenseTag.PERSONAL_ONLY


class TestClassification:
    def test_子表_14_本と_jss_10_本すべてに区分がある(self) -> None:
        """実測で名前が裏付けられている表を取りこぼしていないこと。"""
        for table in cs.CHILD_TABLES:
            assert table in G.TABLE_LICENSE, table
        for table in S.declared_tables():
            assert table in G.TABLE_LICENSE, table
        assert "core_stocks" in G.TABLE_LICENSE

    def test_全区分に一次ソースと裏付けが書かれている(self) -> None:
        """推測で埋めないための欄（§3-1）。空なら宣言の根拠が無い。"""
        for table, spec in G.TABLE_LICENSE.items():
            assert spec.source, table
            assert spec.evidence, table

    def test_UNIFORM_だけがタグを持つ(self) -> None:
        """行タグ・列地図の表に表全体のタグを持たせると 2 つの真実になる。"""
        for table, spec in G.TABLE_LICENSE.items():
            if spec.kind in (G.TableKind.ROW_TAG, G.TableKind.COLUMN_MAP):
                assert spec.tag is None, table
            elif spec.kind is G.TableKind.UNIFORM:
                assert spec.tag is not None, table

    def test_Yahoo_由来の派生は_personal_only_を継承する(self) -> None:
        """`licensing.inherit` の規則（最も厳しいタグを継承）と一致すること。"""
        for table in (
            "swing_daily_ohlcv", "swing_stock_indicators", "rsi_percentile",
            "otakara_stock_scores", "core_stock_financials",
        ):
            assert G.TABLE_LICENSE[table].tag is LicenseTag.PERSONAL_ONLY, table

    def test_鮮度表を行タグと取り違えていない(self) -> None:
        """`jss_dataset_freshness.license_tag` は**観測対象の説明**で行のタグではない。"""
        assert G.TABLE_LICENSE["jss_dataset_freshness"].kind is G.TableKind.OPERATIONAL


class TestSharedContract:
    """両リポジトリ共有の契約ファイルと実装が一致していること。

    地図を 1 つにするのが目的（`docs/TARGET-ARCHITECTURE.md` §8.2）。Python の
    リテラルを正本にし、契約ファイルを両リポジトリで同一バイト列にして CI の
    `cross-repo-contract` ジョブが diff する（銘柄コード契約と同じ方式）。
    """

    def test_列地図が契約ファイルと一致する(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        declared: dict[str, dict[str, str]] = {}
        for table, column, tag in S.column_license_rows():
            declared.setdefault(table, {})[column] = tag
        assert contract["column_license"] == declared

    def test_表区分が契約ファイルと一致する(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        declared = {
            table: {"kind": spec.kind.value, "tag": spec.tag.value if spec.tag else None}
            for table, spec in sorted(G.TABLE_LICENSE.items())
        }
        assert contract["table_license"] == declared

    def test_未宣言列の扱いが契約ファイルに書かれている(self) -> None:
        """「宣言が無い = 公開しない」を読み手（TypeScript 側）へ渡す。"""
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        assert contract["$undeclared_policy"]


class TestWriterClaimContract:
    def test_column_group_の語彙が契約ファイルと一致する(self) -> None:
        """PK が `(dataset, column_group)` なので後からの改名は破壊的書換になる。

        設計書には `base` / `enrich` の**定義が無く**、本番の既存 4 行は `all`
        だった。値を先に固定しないと、両リポジトリが別の語彙で書き始める。
        """
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        assert contract["column_groups"] == G.COLUMN_GROUPS

    def test_claim_が契約ファイルと一致する(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        declared = [
            {
                "dataset": c.dataset,
                "column_group": c.column_group,
                "writer": c.writer,
                "declared": c.declared,
            }
            for c in G.WRITER_CLAIMS
        ]
        assert contract["writer_claims"] == declared

    def test_claim_の_dataset_は区分の宣言に存在する(self) -> None:
        """存在しない表への claim は、誰も守らない宣言になる。"""
        for claim in G.WRITER_CLAIMS:
            assert claim.dataset in G.TABLE_LICENSE, claim.dataset

    def test_語彙外の_column_group_を宣言していない(self) -> None:
        for claim in G.WRITER_CLAIMS:
            assert claim.column_group in G.COLUMN_GROUPS, claim

    def test_宣言日が_UTC_固定で決定的(self) -> None:
        """ローカルタイムゾーン依存だと CI (UTC) と手元 (JST) で値が変わり、

        投入が毎回 UPDATE を打って冪等でなくなる。
        """
        assert G.declared_epoch("2026-09-13") == 1789257600

    def test_鮮度表の_writer_が_claim_と食い違わない(self) -> None:
        """`jss_dataset_freshness.writer` と `jss_writer_claims` の突合（§1.3-3）。

        `core_stocks` / `yutai_benefits` はどちらも stockStock のジョブ名を
        書いていたが、実際に行を書いているのは kabulab-cf である。
        """
        from jp_stock_pipeline.cloud_store.datasets import DATASET_SOURCE_BY_NAME

        for dataset in ("core_stocks", "yutai_benefits"):
            assert "kabulab-cf" in DATASET_SOURCE_BY_NAME[dataset].writer, dataset
