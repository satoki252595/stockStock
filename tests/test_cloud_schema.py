"""D1 の jss_* スキーマの検証 (docs/CF-CANONICAL-DESIGN.md §B)。

このテストの主目的は「D1 に置いてはいけないものが入り込んでいないこと」を
機械的に固定すること。D1 の 1DB 10GB 上限は申請でも引き上げ不可で、課金軸が
走査行数なので、時系列を1つ通すと後から戻せない。
"""

from __future__ import annotations

import re

from _doubles import RecordingD1

from jp_stock_pipeline.cloud_store import schema as S
from jp_stock_pipeline.licensing import LicenseTag

_CREATE_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)")


def _tables() -> set[str]:
    found = set()
    for statement in S.SCHEMA_STATEMENTS:
        m = _CREATE_TABLE.search(statement)
        if m:
            found.add(m.group(1))
    return found


class TestSchemaShape:
    def test_all_statements_are_idempotent(self):
        """再実行で既存データを壊さない。"""
        for statement in S.SCHEMA_STATEMENTS:
            assert "IF NOT EXISTS" in statement, statement[:80]

    def test_every_table_uses_the_jss_prefix(self):
        """既存の core_/swing_/ir_ 等と同居するので所有を接頭辞で明示する。"""
        for table in _tables():
            assert table.startswith("jss_"), table

    def test_expected_tables_exist(self):
        assert _tables() == {
            "jss_raw_files",
            "jss_financials",
            "jss_xbrl_documents",
            "jss_xbrl_elements",
            "jss_supply_latest",
            "jss_index_symbols",
            "jss_job_runs",
            "jss_dataset_freshness",
            "jss_writer_claims",
            "jss_column_license",
            "jss_notion_pages",
        }

    def test_no_table_for_unbounded_time_series(self):
        """②日足(年95.5万行)・⑧ファクト本体(年883万行)・需給日次(年210万行)は R2。

        D1 の判定基準は「年間増加10万行以下」。これを通すと 10GB 上限
        (引き上げ不可) に必ず当たる。
        """
        ddl = "\n".join(S.SCHEMA_STATEMENTS)
        for banned in ("jss_daily_ohlcv", "jss_xbrl_facts", "jss_supply_history", "jss_prices"):
            assert banned not in ddl, f"時系列テーブル {banned} が D1 に混入している"

    def test_xbrl_documents_holds_counts_not_facts(self):
        """ファクト本体ではなく所在と件数だけを持つ。"""
        ddl = next(s for s in S.SCHEMA_STATEMENTS if "jss_xbrl_documents" in s)
        assert "fact_count" in ddl
        assert "parquet_key" in ddl  # 本体は R2 の Parquet
        assert "element" not in ddl  # ファクトの明細列を持たない

    def test_financials_pk_includes_the_consolidation_flag(self):
        """連結と単体は同一期末の**別の測定範囲**。キーで分けないと後勝ちになる。

        旧 PK は `(code, fiscal_period_end, disclosure_type)` だった。
        `core_stock_annual_financials` で現に起きている事故（連結と単体の混在で
        持株会社 464 銘柄中 296 が自系列内 10 倍超スパン）と同じ構造なので、
        DDL のリテラルをここで固定する。
        """
        ddl = next(s for s in S.SCHEMA_STATEMENTS if "jss_financials" in s)
        assert (
            "PRIMARY KEY (code, fiscal_period_end, disclosure_type, consolidated)"
            in ddl
        )

    def test_financials_pk_constant_matches_the_ddl(self):
        """`FINANCIALS_PK`（writer が読む）と DDL のリテラルの突き合わせ。

        片方を導出にすると 1 要素を削る変異が両方へ同時に効いて検出できない
        ので、2 本のリテラルを別に持ってここで比較する。
        """
        ddl = next(s for s in S.SCHEMA_STATEMENTS if "jss_financials" in s)
        m = re.search(r"PRIMARY KEY \(([^)]*)\)", ddl)
        assert m is not None
        assert tuple(c.strip() for c in m.group(1).split(",")) == S.FINANCIALS_PK

    def test_financials_pk_columns_are_all_not_null(self):
        """SQLite は PK 列に NULL を許す（2026-09-13 sqlite 3.51.0 で実測）。

        nullable な列を PK に入れると、NULL 同士が衝突しないため
        `ON CONFLICT` が当たらず同じキーの行が毎回増える。後勝ちを直した
        つもりで別の静かな事故に化けるので、PK 列の NOT NULL を固定する。
        """
        ddl = next(s for s in S.SCHEMA_STATEMENTS if "jss_financials" in s)
        for column in S.FINANCIALS_PK:
            line = next(
                row
                for row in ddl.splitlines()
                if row.strip().startswith(f"{column} ")
            )
            assert "NOT NULL" in line, line

    def test_supply_latest_is_a_snapshot_keyed_by_code_and_type(self):
        ddl = next(s for s in S.SCHEMA_STATEMENTS if "jss_supply_latest" in s)
        assert "PRIMARY KEY (code, data_type)" in ddl  # 日付は主キーに入らない＝断面


class TestIndexSymbols:
    def test_seven_slugs_including_nikkei_vi(self):
        """旧設計の6つでは既存画面の入力(日経VI)が1つ欠ける。"""
        slugs = [slug for slug, _sym, _name in S.INDEX_SYMBOLS]
        assert len(slugs) == 7
        assert "nkvi" in slugs

    def test_unverified_symbol_is_none_not_guessed(self):
        """日経VI の Yahoo シンボルは未確認。推測で埋めない (§3-1)。"""
        nkvi = next(x for x in S.INDEX_SYMBOLS if x[0] == "nkvi")
        assert nkvi[1] is None


class TestColumnLicense:
    def test_core_stocks_mixes_two_licenses_in_one_row(self):
        """行の license_tag 1列では表現できないので列単位で持つ。"""
        rows = S.column_license_rows()
        by_column = {c: tag for t, c, tag in rows if t == "core_stocks"}
        assert by_column["edinet_code"] == LicenseTag.COMMERCIAL_OK.value
        assert by_column["market"] == LicenseTag.PERSONAL_ONLY.value

    def test_sector_and_sector33_are_tagged_by_provenance_not_by_name(self):
        """`sector` と `sector33` は名前が似ているだけで writer も一次ソースも違う。

        旧宣言は `sector33` を personal-only にしていたが、この列を書くのは
        stockStock の `collectors/edinet_codelist.py`（EDINET コードリストの
        「提出者業種」）で commercial-ok が正しい。実際に JPX `data_j.xlsx` 由来
        なのは kabulab-cf `src/cron/universe.ts` が書く既存列 `sector` で、
        そちらは地図に一度も載っていなかった。

        **片方だけ直すと危険**なのでこの 2 本を 1 つのテストで固定する:
        `sector33` だけ commercial-ok にして `sector` を載せ忘れると、JPX 由来の
        業種が「地図に無い＝何も止めない」状態のまま公開面に出る。
        """
        by_column = {c: tag for t, c, tag in S.column_license_rows() if t == "core_stocks"}
        assert by_column["sector33"] == LicenseTag.COMMERCIAL_OK.value  # EDINET 提出者業種
        assert by_column["sector"] == LicenseTag.PERSONAL_ONLY.value  # JPX 33業種

    def test_rows_are_deterministic(self):
        assert S.column_license_rows() == S.column_license_rows()


class _RecordingStore(RecordingD1):
    """発行文の形だけ見る。`statements` は旧名の別名（呼び出し側はそのまま）。"""

    @property
    def statements(self) -> list[str]:
        return self.sqls


class TestApply:
    def test_apply_schema_runs_every_statement(self):
        store = _RecordingStore()
        assert S.apply_schema(store) == len(S.SCHEMA_STATEMENTS)
        assert len(store.statements) == len(S.SCHEMA_STATEMENTS)

    def test_seed_skips_symbols_without_a_verified_ticker(self):
        store = _RecordingStore()
        S.seed_reference_tables(store)
        symbols = next(u for u in store.upserts if u[0] == "jss_index_symbols")
        seeded = [row[0] for row in symbols[2]]
        assert "nkvi" not in seeded  # 未確認のものは投入しない
        assert len(seeded) == 6

    def test_seed_writes_column_license(self):
        store = _RecordingStore()
        S.seed_reference_tables(store)
        licenses = next(u for u in store.upserts if u[0] == "jss_column_license")
        assert licenses[3] == ["table_name", "column_name"]
        assert len(licenses[2]) == len(S.column_license_rows())
