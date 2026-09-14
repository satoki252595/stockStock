"""local_store のマッパー純粋関数 (record→SQL/params) と sink 分岐のテスト。

実 PostgreSQL は不要。SQL 文・パラメータの正しさ（PK・完全置換・ライフサイクル
出し分け・SQL インジェクション安全な %()s 形式）と、sink の data_date None 省略を検証。
③ のマージ意味論だけは文字列一致では確かめられないので、ローカルの本物の DDL を
sqlite に流して生成 SQL をそのまま実行する（`TestFinancialUpsertMerge`）。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from conftest import provenance

from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.local_store import mappers
from jp_stock_pipeline.local_store import schema as local_schema
from jp_stock_pipeline.local_store.schema import SCHEMA_STATEMENTS
from jp_stock_pipeline.local_store.sink import LocalStore
from jp_stock_pipeline.models import (
    JST,
    ConvertStatus,
    DataQuality,
    DisclosureRecord,
    FinancialSummaryRecord,
    PriceTechnicalRecord,
    Provenance,
    RawArtifact,
    Source,
    StockMasterRecord,
)


def _prov(**kw) -> Provenance:
    kw.setdefault("fetched_at", datetime(2026, 6, 10, 12, tzinfo=JST))
    return provenance(**kw)


class TestStockMasterUpsert:
    def test_include_lifecycle_false_omits_lifecycle_cols(self):
        rec = StockMasterRecord(
            code="7203", name="トヨタ", status="上場",
            listing_date=date(2020, 1, 1), provenance=_prov(),
        )
        sql, params = mappers.stock_master_upsert(rec, include_lifecycle=False)
        # codelist 同期は status/上場日/上場廃止日 を一切送らない (§ Phase3 二重所有回避)
        assert "status" not in params
        assert "listing_date" not in params
        assert "delisting_date" not in params
        assert "status" not in sql
        assert "ON CONFLICT (code)" in sql
        assert "EXCLUDED" in sql
        # codelist 所有列は常に送る
        assert params["code"] == "7203"
        assert params["name"] == "トヨタ"
        assert params["listed"] is True

    def test_include_lifecycle_true_sends_lifecycle(self):
        rec = StockMasterRecord(
            code="7203", name="トヨタ", status="上場廃止",
            delisting_date=date(2026, 8, 31), provenance=_prov(),
        )
        sql, params = mappers.stock_master_upsert(rec, include_lifecycle=True)
        assert params["status"] == "上場廃止"
        assert params["delisting_date"] == date(2026, 8, 31)
        assert "status = EXCLUDED.status" in sql

    def test_provenance_expanded_to_columns(self):
        rec = StockMasterRecord(
            code="7203", name="t", provenance=_prov(quality=DataQuality.NEEDS_REVIEW),
        )
        _, params = mappers.stock_master_upsert(rec)
        assert params["source"] == "EDINET"
        assert params["license_tag"] == "commercial-ok"
        assert params["quality"] == "要確認"
        assert params["data_date"] == date(2026, 6, 10)

    def test_uses_named_placeholders_only(self):
        rec = StockMasterRecord(code="7203", name="x", provenance=_prov())
        sql, _ = mappers.stock_master_upsert(rec)
        # 値はプレースホルダ経由（文字列連結しない=SQLi 安全）
        assert "%(code)s" in sql
        assert "7203" not in sql


class TestBuildUpsertGuardColumn:
    """_build_upsert の guard_column オプション（#14 の共通実装）。"""

    def test_no_guard_column_produces_plain_upsert(self):
        sql, _ = mappers._build_upsert("t", {"pk": 1, "v": 2}, ["pk"])
        assert "WHERE" not in sql

    def test_guard_column_adds_where_clause(self):
        sql, _ = mappers._build_upsert(
            "financials", {"pk": 1, "disclosed_at": None}, ["pk"],
            guard_column="disclosed_at",
        )
        assert "WHERE EXCLUDED.disclosed_at >= financials.disclosed_at" in sql
        assert "OR financials.disclosed_at IS NULL" in sql

    def test_guard_column_not_in_params_raises(self):
        """タイポ等で存在しない列を指定したら黙って無視せずエラーにする。"""
        with pytest.raises(ValueError, match="disclosed_at"):
            mappers._build_upsert(
                "financials", {"pk": 1}, ["pk"], guard_column="disclosed_at"
            )

    def test_guard_column_excluded_from_params_raises(self):
        with pytest.raises(ValueError, match="disclosed_at"):
            mappers._build_upsert(
                "financials", {"pk": 1, "disclosed_at": None}, ["pk"],
                exclude_cols=("disclosed_at",), guard_column="disclosed_at",
            )


class TestPriceUpsert:
    def test_pk_is_code_and_data_date(self):
        rec = PriceTechnicalRecord(
            code="7203", close=2500.0, rsi14=55.5, provenance=_prov(data_date=date(2026, 6, 10)),
        )
        sql, params = mappers.price_upsert(rec)
        assert "ON CONFLICT (code, data_date)" in sql
        assert params["data_date"] == date(2026, 6, 10)
        assert params["close"] == 2500.0
        assert params["rsi14"] == 55.5
        # PK 列は UPDATE 対象に含めない
        assert "data_date = EXCLUDED.data_date" not in sql
        assert "code = EXCLUDED.code" not in sql
        # 非PK列は EXCLUDED で完全置換
        assert "close = EXCLUDED.close" in sql


class TestFinancialUpsert:
    def test_composite_pk_and_values(self):
        rec = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
            net_sales=1.0e12, eps=250.0, provenance=_prov(),
        )
        sql, params = mappers.financial_upsert(rec)
        assert "ON CONFLICT (code, fiscal_period_end, disclosure_type, consolidated)" in sql
        # 連結区分を判定できなかったレコードは D1 と同じ '不明'（PK 列は NULL を許さない）
        assert params["consolidated"] == mappers.UNKNOWN_CONSOLIDATED == "不明"
        assert params["net_sales"] == 1.0e12
        assert params["eps"] == 250.0

    def test_disclosed_at_guard_is_present(self):
        """#14: 開示日時ガードが生成 SQL に入っていること。

        古い原報告の再実行で訂正後の値を巻き戻さないための WHERE 句。
        既存側が NULL（過去に未設定）のときは常に許可する。
        """
        rec = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
            disclosed_at=datetime(2026, 6, 10, tzinfo=JST), provenance=_prov(),
        )
        sql, params = mappers.financial_upsert(rec)
        assert "WHERE EXCLUDED.disclosed_at >= financials.disclosed_at" in sql
        assert "OR financials.disclosed_at IS NULL" in sql
        assert params["disclosed_at"] == datetime(2026, 6, 10, tzinfo=JST)


class TestBuildUpsertMergeCols:
    """_build_upsert の merge_cols / merge_scope / license_column（③ のための例外）。"""

    def test_default_is_full_replacement(self):
        """指定しなければ従来どおり完全置換（①②④⑤ の仕様を一律に変えない）。"""
        sql, _ = mappers._build_upsert("t", {"pk": 1, "v": 2}, ["pk"])
        assert "v = EXCLUDED.v" in sql
        assert "COALESCE" not in sql

    def test_merge_cols_coalesce_only_the_listed_columns(self):
        sql, _ = mappers._build_upsert(
            "t", {"pk": 1, "a": None, "b": None}, ["pk"], merge_cols=("a",)
        )
        assert "a = COALESCE(EXCLUDED.a, t.a)" in sql
        assert "b = EXCLUDED.b," in sql

    def test_merge_scope_limits_the_coalesce_to_the_same_scope(self):
        sql, _ = mappers._build_upsert(
            "t", {"pk": 1, "s": "連結", "a": None}, ["pk"],
            merge_cols=("a",), merge_scope="s",
        )
        assert (
            "a = CASE WHEN EXCLUDED.s IS NOT DISTINCT FROM t.s"
            " THEN COALESCE(EXCLUDED.a, t.a) ELSE EXCLUDED.a END" in sql
        )
        assert "s = EXCLUDED.s," in sql

    def test_license_column_keeps_the_stricter_tag(self):
        sql, _ = mappers._build_upsert(
            "t", {"pk": 1, "lt": "commercial-ok"}, ["pk"], license_column="lt"
        )
        assert "lt = EXCLUDED.lt" not in sql
        assert "lt = CASE WHEN CASE EXCLUDED.lt" in sql

    @pytest.mark.parametrize(
        "kw",
        [
            {"merge_cols": ("nope",)},
            {"merge_cols": ("pk",)},
            {"merge_cols": ("a",), "merge_scope": "nope"},
            {"license_column": "nope"},
        ],
    )
    def test_column_that_is_not_a_non_pk_param_is_rejected(self, kw):
        """タイポを黙って完全置換にすると、③ の NULL 潰しが列単位で再発する。"""
        with pytest.raises(ValueError, match="nope|pk"):
            mappers._build_upsert("t", {"pk": 1, "a": None}, ["pk"], **kw)

    @pytest.mark.parametrize("kw", [{"merge_scope": "a"}, {"license_column": "a"}])
    def test_overlapping_roles_are_rejected(self, kw):
        with pytest.raises(ValueError, match="重ねられない"):
            mappers._build_upsert(
                "t", {"pk": 1, "a": None}, ["pk"], merge_cols=("a",), **kw
            )


class TestOnlyFinancialsMerge:
    """③ 以外は完全置換のまま（冒頭 docstring が意図された仕様として宣言している）。"""

    @pytest.mark.parametrize(
        "build",
        [
            lambda: mappers.stock_master_upsert(
                StockMasterRecord(code="7203", name="t", provenance=_prov())
            ),
            lambda: mappers.price_upsert(PriceTechnicalRecord(code="7203", provenance=_prov())),
            lambda: mappers.disclosure_upsert(
                DisclosureRecord(
                    doc_id="d", title="t", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
                    code="7203", doc_type="短信", provenance=_prov(),
                )
            ),
            lambda: mappers.raw_file_upsert(_raw()),
        ],
        ids=["①", "②", "④", "⑤"],
    )
    def test_other_tables_stay_full_replacement(self, build):
        sql, _ = build()
        assert "COALESCE" not in sql
        assert "license_tag = EXCLUDED.license_tag" in sql


class TestFinancialUpsertAssignments:
    @staticmethod
    def _sql() -> tuple[str, dict]:
        rec = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
            provenance=_prov(),
        )
        return mappers.financial_upsert(rec)

    def test_every_non_pk_column_is_assigned_exactly_once(self):
        """SET 句から落ちた列は「永久に更新されない列」になる。"""
        sql, params = self._sql()
        set_clause = sql.split(" DO UPDATE SET ", 1)[1].split(" WHERE ", 1)[0]
        assigned = re.findall(r"(?:^|, )(\w+) = ", set_clause)
        assert assigned == [c for c in params if c not in mappers.FIN_PK] + ["updated_at"]

    def test_value_columns_merge(self):
        """連結区分が PK に入ったので、衝突するのは同じ測定範囲の行だけ。

        以前は PK に無く、連結区分が一致するときだけ COALESCE する CASE で
        「単体の訂正値 + 前回の連結値」の混在を防いでいた。今は無条件に COALESCE する。
        """
        sql, _ = self._sql()
        for c in (
            "net_sales", "eps", "cf_operating", "dps_actual", "forecast_eps",
            "accounting_standard", "disclosed_at", "data_date",
        ):
            assert f"{c} = COALESCE(EXCLUDED.{c}, financials.{c})" in sql
        assert "CASE WHEN EXCLUDED.consolidated" not in sql

    def test_not_null_provenance_is_overwritten_and_license_is_not(self):
        sql, _ = self._sql()
        for c in ("source", "fetched_at", "quality"):
            assert f"{c} = EXCLUDED.{c}," in sql
        assert "consolidated = " not in sql.split(" DO UPDATE SET ", 1)[1]  # PK 列は更新しない
        assert "license_tag = EXCLUDED.license_tag" not in sql
        assert "license_tag = CASE WHEN CASE EXCLUDED.license_tag" in sql


# --- ③ のマージ意味論を実際に実行する ---------------------------------------
#
# CI に PostgreSQL は無いので、local_store/schema.py の本物の DDL を sqlite に流し、
# financial_upsert が生成した SQL をそのまま実行する。使っている構文
# （ON CONFLICT DO UPDATE ... WHERE / EXCLUDED / COALESCE / CASE /
# IS NOT DISTINCT FROM）は PostgreSQL と sqlite(>=3.39) で同じ意味を持つ。
# 方言差は機械的な 2 点だけ吸収する: プレースホルダ %(name)s → :name と now()。
# 日時は同じタイムゾーンの ISO 文字列で渡すので、文字列の >= が時刻順と一致する。

_ORIGINAL_AT = datetime(2026, 6, 25, 15, tzinfo=JST)
_CORRECTION_AT = datetime(2026, 7, 10, 15, tzinfo=JST)


def _fin(
    *,
    consolidated: str | None = "連結",
    disclosed_at: datetime = _ORIGINAL_AT,
    source: Source = Source.EDINET,
    license_tag: LicenseTag = LicenseTag.COMMERCIAL_OK,
    **values,
) -> FinancialSummaryRecord:
    return FinancialSummaryRecord(
        code="7203", fiscal_period_end=date(2026, 3, 31), disclosure_type="本決算",
        consolidated=consolidated, disclosed_at=disclosed_at,
        provenance=_prov(source=source, license_tag=license_tag, fetched_at=disclosed_at),
        **values,
    )


class _FinancialsDb:
    def __init__(self) -> None:
        ddl = next(s for s in SCHEMA_STATEMENTS if "TABLE IF NOT EXISTS financials" in s)
        self.con = sqlite3.connect(":memory:")
        self.con.create_function("now", 0, lambda: "now")
        self.con.execute(ddl.replace("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP"))

    def write(self, record: FinancialSummaryRecord) -> None:
        sql, params = mappers.financial_upsert(record)
        # datetime は date の派生なので両方 ISO 文字列になる
        values = {k: v.isoformat() if isinstance(v, date) else v for k, v in params.items()}
        self.con.execute(re.sub(r"%\((\w+)\)s", r":\1", sql), values)

    def rows(self) -> list[dict]:
        cur = self.con.execute("SELECT * FROM financials ORDER BY consolidated")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    def row(self) -> dict:
        (only,) = self.rows()  # PK が同じなので 1 行
        return only


class TestFinancialUpsertMerge:
    def test_a_partial_correction_does_not_null_out_a_complete_row(self):
        """edinet_daily は 120(有報) と 130(訂正有報) を同じ '本決算' に落とす。

        訂正は訂正した項目だけを載せるので、完全置換だと完全な行の残りが全部 NULL になる。
        """
        db = _FinancialsDb()
        db.write(
            _fin(
                accounting_standard="日本基準", net_sales=1000.0, eps=120.5,
                cf_operating=333.0, dps_actual=60.0,
            )
        )
        db.write(_fin(net_sales=1100.0, disclosed_at=_CORRECTION_AT))  # 訂正: 売上だけ
        row = db.row()
        assert row["net_sales"] == 1100.0  # 訂正が載せた値は反映される
        assert (row["eps"], row["cf_operating"], row["dps_actual"]) == (120.5, 333.0, 60.0)
        assert row["accounting_standard"] == "日本基準"
        assert row["disclosed_at"] == _CORRECTION_AT.isoformat()
        assert row["fetched_at"] == _CORRECTION_AT.isoformat()  # NOT NULL の来歴は上書き

    def test_an_older_disclosure_cannot_roll_back_a_correction(self):
        db = _FinancialsDb()
        db.write(_fin(net_sales=1000.0, eps=120.5))
        db.write(_fin(net_sales=1100.0, disclosed_at=_CORRECTION_AT))
        db.write(_fin(net_sales=1000.0, eps=99.0))  # 6/25 の原報告を再取得
        row = db.row()
        assert (row["net_sales"], row["eps"]) == (1100.0, 120.5)
        assert row["disclosed_at"] == _CORRECTION_AT.isoformat()

    def test_reprocessing_the_same_disclosure_is_idempotent(self):
        db = _FinancialsDb()
        db.write(_fin(net_sales=1000.0, eps=120.5))
        db.write(_fin(net_sales=1000.0, eps=120.5))
        row = db.row()
        assert (row["net_sales"], row["eps"]) == (1000.0, 120.5)

    def test_a_tdnet_correction_makes_the_row_stricter(self):
        db = _FinancialsDb()
        db.write(_fin(net_sales=1000.0))
        db.write(
            _fin(
                net_sales=1100.0, source=Source.TDNET,
                license_tag=LicenseTag.FACTUAL_CITE, disclosed_at=_CORRECTION_AT,
            )
        )
        assert db.row()["license_tag"] == LicenseTag.FACTUAL_CITE.value

    def test_an_edinet_write_does_not_launder_a_factual_cite_row(self):
        """混在した行を commercial-ok に戻すと、公開面へ全量が出てしまう。"""
        db = _FinancialsDb()
        db.write(
            _fin(
                net_sales=1000.0, eps=50.0,
                source=Source.TDNET, license_tag=LicenseTag.FACTUAL_CITE,
            )
        )
        db.write(_fin(net_sales=1100.0, disclosed_at=_CORRECTION_AT))
        row = db.row()
        assert (row["net_sales"], row["eps"]) == (1100.0, 50.0)  # eps は TDnet 由来のまま
        assert row["license_tag"] == LicenseTag.FACTUAL_CITE.value
        assert row["source"] == Source.EDINET.value  # 来歴は最後に書いた側

    def test_a_different_consolidation_is_a_separate_row(self):
        """連結区分は PK の一部。単体の値が連結の行を置き換えも、混ざりもしない。

        以前は PK に無く、単体の書き込みが連結の行を完全置換していた（値は混ざらないが
        連結の行が消えた）。D1 (PR #39) と同じく別行にする。
        """
        db = _FinancialsDb()
        db.write(_fin(net_sales=1000.0, eps=120.5))
        db.write(_fin(consolidated="単体", net_sales=50.0, disclosed_at=_CORRECTION_AT))
        rows = {r["consolidated"]: (r["net_sales"], r["eps"]) for r in db.rows()}
        assert rows == {"連結": (1000.0, 120.5), "単体": (50.0, None)}

    def test_undetermined_consolidation_merges_with_itself(self):
        """normalize は単体のみの会社で consolidated=None を返しうる。

        None 同士は同じ測定範囲とみなしてマージする（D1 の '不明' 同士と同じ）。
        """
        db = _FinancialsDb()
        db.write(_fin(consolidated=None, net_sales=1000.0, eps=120.5))
        db.write(_fin(consolidated=None, net_sales=1100.0, disclosed_at=_CORRECTION_AT))
        row = db.row()
        assert (row["net_sales"], row["eps"]) == (1100.0, 120.5)
        assert row["consolidated"] == "不明"


class TestFinancialsPkMigration:
    """既存のローカル DB の ③ PK を (code, 決算期末, 開示種別, 連結区分) へ張り替える DDL。

    CI に PostgreSQL は無いので、ここでは文の形と並びを確かめる。実際の張り替えは
    PostgreSQL 17 で確認した（PR 本文）。
    """

    @staticmethod
    def _statements() -> list[str]:
        return list(SCHEMA_STATEMENTS)

    def test_new_tables_are_created_with_the_four_column_pk(self):
        ddl = next(s for s in SCHEMA_STATEMENTS if "TABLE IF NOT EXISTS financials" in s)
        assert "PRIMARY KEY (code, fiscal_period_end, disclosure_type, consolidated)" in ddl
        assert "consolidated              TEXT NOT NULL" in ddl

    def test_migration_runs_right_after_the_create_table(self):
        statements = self._statements()
        create = next(
            i for i, s in enumerate(statements) if "TABLE IF NOT EXISTS financials" in s
        )
        assert statements[create + 1] is local_schema.FINANCIALS_PK_MIGRATION

    def test_migration_is_guarded_and_fills_unknown_before_not_null(self):
        sql = local_schema.FINANCIALS_PK_MIGRATION
        assert sql.index("RETURN") < sql.index("LOCK TABLE financials")  # 済んでいればロックしない
        assert sql.count("a.attname = 'consolidated'") == 2  # ロック後に確認し直す
        assert (
            sql.index(f"SET consolidated = '{mappers.UNKNOWN_CONSOLIDATED}' WHERE consolidated IS NULL")
            < sql.index("SET NOT NULL")
            < sql.index("DROP CONSTRAINT")
            < sql.index("ADD PRIMARY KEY (code, fiscal_period_end, disclosure_type, consolidated)")
        )
        assert "%" not in sql  # psycopg のプレースホルダと誤解されない

    def test_unknown_consolidation_matches_d1(self):
        from jp_stock_pipeline.cloud_store.financials import UNKNOWN_CONSOLIDATED

        assert mappers.UNKNOWN_CONSOLIDATED == UNKNOWN_CONSOLIDATED


class TestDisclosureUpsert:
    def test_doc_id_pk_and_corporate_action_attrs(self):
        rec = DisclosureRecord(
            doc_id="d1", title="株式分割", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="株式分割", split_ratio="1:3", split_factor=3.0,
            effective_date=date(2026, 7, 1), provenance=_prov(),
        )
        sql, params = mappers.disclosure_upsert(rec)
        assert "ON CONFLICT (doc_id)" in sql
        assert params["doc_id"] == "d1"
        assert params["split_ratio"] == "1:3"
        assert params["split_factor"] == 3.0
        assert params["effective_date"] == date(2026, 7, 1)


class TestRawFileUpsert:
    def test_sha256_pk(self):
        art = RawArtifact(
            source=Source.EDINET, datatype="codelist", scope="ALL",
            data_date=date(2026, 6, 10), fetched_at=datetime(2026, 6, 10, tzinfo=JST),
            url="https://x/y.zip", local_path=Path("data/raw/y.zip"), sha256="abc123",
            size_bytes=999, license_tag=LicenseTag.COMMERCIAL_OK,
            convert_status=ConvertStatus.DONE,
        )
        sql, params = mappers.raw_file_upsert(art)
        assert "ON CONFLICT (sha256)" in sql
        assert params["sha256"] == "abc123"
        assert params["filename"] == "y.zip"
        assert params["convert_status"] == "完了"
        assert params["size_bytes"] == 999


class TestJobLogInsert:
    def test_insert_only_no_conflict(self):
        sql, params = mappers.job_log_insert("master_sync", "成功", 10, 0, [], None, 1.5)
        assert sql.startswith("INSERT INTO job_log")
        assert "ON CONFLICT" not in sql
        assert params["processed"] == 10
        assert params["failed_codes"] is None  # 空リストは NULL

    def test_failed_codes_joined(self):
        _, params = mappers.job_log_insert(
            "j", "一部失敗", 5, 2, ["7203", "9999"], "https://run", 2.0
        )
        assert params["failed_codes"] == "7203,9999"
        assert params["run_url"] == "https://run"


class TestLifecycleAndAbsent:
    def test_mark_absent_only_sets_listed_false(self):
        sql, params = mappers.mark_absent_update("7203")
        assert "listed = FALSE" in sql
        assert "status" not in sql  # 状態は一次開示が所有
        assert params == {"code": "7203"}

    def test_lifecycle_delisting_with_date(self):
        rec = DisclosureRecord(
            doc_id="d", title="上場廃止", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="上場廃止", effective_date=date(2026, 8, 31),
            provenance=_prov(),
        )
        sql, params = mappers.lifecycle_update(rec)
        assert "status = %(status)s" in sql
        assert "delisting_date = %(eff_date)s" in sql
        assert params["status"] == "上場廃止"
        assert params["eff_date"] == date(2026, 8, 31)

    def test_lifecycle_delisting_without_date_omits_date(self):
        rec = DisclosureRecord(
            doc_id="d", title="上場廃止後の取り扱い",
            disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="上場廃止", provenance=_prov(),
        )
        sql, params = mappers.lifecycle_update(rec)
        # 効力発生日が無いときは日付キーを送らない（既存値保持 §3-1）
        assert "delisting_date" not in sql
        assert params == {"status": "上場廃止", "code": "7203"}

    def test_lifecycle_new_listing(self):
        rec = DisclosureRecord(
            doc_id="d", title="新規上場", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="300A", doc_type="新規上場", effective_date=date(2026, 4, 1),
            provenance=_prov(),
        )
        sql, params = mappers.lifecycle_update(rec)
        assert params["status"] == "上場"
        assert "listing_date = %(eff_date)s" in sql

    def test_lifecycle_non_lifecycle_type_returns_none(self):
        rec = DisclosureRecord(
            doc_id="d", title="決算短信", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="短信", provenance=_prov(),
        )
        assert mappers.lifecycle_update(rec) is None

    def test_lifecycle_without_code_returns_none(self):
        rec = DisclosureRecord(
            doc_id="d", title="上場廃止", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code=None, doc_type="上場廃止", provenance=_prov(),
        )
        assert mappers.lifecycle_update(rec) is None


# --- sink の分岐 (DB は記録のみのフェイク接続) -----------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, seq_params):
        self.executed.append((sql, list(seq_params)))


class _FakeConn:
    def __init__(self) -> None:
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur

    def close(self):
        pass


class TestSinkBranches:
    def test_price_skipped_when_data_date_none(self):
        store = LocalStore(_FakeConn())
        rec = PriceTechnicalRecord(code="7203", close=1.0, provenance=_prov(data_date=None))
        store.upsert_price_technical(rec)
        assert store._conn.cur.executed == []  # 主キーを作れないので格納しない

    def test_price_executed_when_data_date_present(self):
        store = LocalStore(_FakeConn())
        rec = PriceTechnicalRecord(code="7203", close=1.0, provenance=_prov(data_date=date(2026, 6, 10)))
        store.upsert_price_technical(rec)
        assert len(store._conn.cur.executed) == 1
        sql, params = store._conn.cur.executed[0]
        assert "INSERT INTO prices" in sql
        assert params["code"] == "7203"

    def test_apply_lifecycle_noop_for_non_lifecycle(self):
        store = LocalStore(_FakeConn())
        rec = DisclosureRecord(
            doc_id="d", title="短信", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="短信", provenance=_prov(),
        )
        store.apply_disclosure_lifecycle(rec)
        assert store._conn.cur.executed == []


# --- JobContext.mirror の型 dispatch / degrade（⑤配線の回帰含む） ---------


class _RecordingLocal:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def upsert_stock_master(self, r, *, include_lifecycle=True):
        self.calls.append(("master", include_lifecycle))

    def upsert_price_technical(self, r):
        self.calls.append(("price",))

    def upsert_financial_summary(self, r):
        self.calls.append(("fin",))

    def upsert_disclosure(self, r):
        self.calls.append(("disc",))

    def upsert_raw_artifact(self, a):
        self.calls.append(("raw",))

    def mark_master_absent(self, code):
        self.calls.append(("absent", code))

    def apply_disclosure_lifecycle(self, r):
        self.calls.append(("lifecycle",))

    def upsert_xbrl_facts(self, rows, artifact):
        rows = list(rows)
        self.calls.append(("facts", len(rows)))
        return len(rows)


def _ctx(local):
    from jp_stock_pipeline.jobs.runner import JobContext

    return JobContext(settings=None, client=None, args=None, local=local)


def _raw(data_date=date(2026, 6, 10)) -> RawArtifact:
    return RawArtifact(
        source=Source.EDINET, datatype="codelist", scope="ALL", data_date=data_date,
        fetched_at=datetime(2026, 6, 10, tzinfo=JST), url="https://x/y.zip",
        local_path=Path("data/raw/y.zip"), sha256="s", size_bytes=1,
        license_tag=LicenseTag.COMMERCIAL_OK,
    )


class TestJobContextMirror:
    def test_raw_artifact_is_dispatched(self):
        # ⑤ 配線の回帰: RawArtifact が upsert_raw_artifact へ dispatch される
        local = _RecordingLocal()
        _ctx(local).mirror(_raw())
        assert local.calls == [("raw",)]

    def test_stock_master_passes_include_lifecycle(self):
        local = _RecordingLocal()
        rec = StockMasterRecord(code="7203", name="t", provenance=_prov())
        _ctx(local).mirror(rec, include_lifecycle=False)
        assert local.calls == [("master", False)]

    def test_price_and_disclosure_dispatch(self):
        local = _RecordingLocal()
        ctx = _ctx(local)
        ctx.mirror(PriceTechnicalRecord(code="7203", provenance=_prov()))
        ctx.mirror(
            DisclosureRecord(
                doc_id="d", title="t", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
                code="7203", doc_type="短信", provenance=_prov(),
            )
        )
        assert local.calls == [("price",), ("disc",)]

    def test_noop_when_local_none(self):
        # local 未接続なら何もしない（例外を出さない）
        _ctx(None).mirror(_raw())  # 例外が出ないこと自体が assert

    def test_failure_degrades_without_raising(self):
        class _Bad:
            def upsert_raw_artifact(self, a):
                raise RuntimeError("db down")

        ctx = _ctx(_Bad())
        ctx.mirror(_raw())  # Notion 正本は別。ミラー失敗で例外を投げない
        assert ctx.mirror_failed == 1


# --- 双方向フェールセーフ: persist / upload_raw -------------------------------
# Notion とローカルを独立に書き、片系統が落ちても他系統へ書く（どちらか一方に
# 残れば成功・両系統失敗のみ失敗）。ユーザー要望 (2026-06-28) の中核挙動。


def _notion_writer(ok: bool):
    """persist に渡す Notion 書き込み callable と呼び出し記録を返す。"""
    calls: list[str] = []

    def _w() -> None:
        calls.append("notion")
        if not ok:
            raise RuntimeError("notion down")

    return _w, calls


def _ok_uploader(page_id: str = "raw-page-1"):
    def _f(client, settings, artifact, **kw):
        artifact.notion_page_id = page_id
        return page_id

    return _f


def _fail_uploader(client, settings, artifact, **kw):
    raise RuntimeError("notion ⑤ down")


class _RaisingLocal:
    """すべての upsert で例外を投げるローカル（ミラー失敗の再現）。"""

    def upsert_price_technical(self, r):
        raise RuntimeError("db down")

    def upsert_raw_artifact(self, a):
        raise RuntimeError("db down")

    def mark_master_absent(self, code):
        raise RuntimeError("db down")

    def apply_disclosure_lifecycle(self, r):
        raise RuntimeError("db down")


class TestPersistFailover:
    def _price(self) -> PriceTechnicalRecord:
        return PriceTechnicalRecord(code="7203", provenance=_prov())

    def test_both_succeed(self):
        local = _RecordingLocal()
        ctx = _ctx(local)
        w, calls = _notion_writer(ok=True)
        assert ctx.persist(self._price(), w, label="②") is True
        assert calls == ["notion"] and local.calls == [("price",)]
        assert ctx.notion_failed == 0 and ctx.mirror_failed == 0

    def test_notion_fails_local_succeeds(self):
        # Notion 失敗 → ローカルには書かれ、取得単位は成功扱い（フェールセーフ）
        local = _RecordingLocal()
        ctx = _ctx(local)
        w, _ = _notion_writer(ok=False)
        assert ctx.persist(self._price(), w, label="②") is True
        assert ctx.notion_failed == 1 and local.calls == [("price",)]

    def test_local_fails_notion_succeeds(self):
        # ローカル失敗 → Notion には書かれ、取得単位は成功扱い（逆方向も対称）
        ctx = _ctx(_RaisingLocal())
        w, calls = _notion_writer(ok=True)
        assert ctx.persist(self._price(), w, label="②") is True
        assert calls == ["notion"] and ctx.mirror_failed == 1

    def test_both_fail_returns_false(self):
        # 両系統とも失敗した取得単位のみ「失敗」を呼び出し側へ返す
        ctx = _ctx(_RaisingLocal())
        w, _ = _notion_writer(ok=False)
        assert ctx.persist(self._price(), w, label="②") is False
        assert ctx.notion_failed == 1 and ctx.mirror_failed == 1

    def test_local_none_follows_notion(self):
        # ローカル系統なし（Notion 単独運用）は従来どおり Notion 成否で決まる
        w_ok, _ = _notion_writer(ok=True)
        assert _ctx(None).persist(self._price(), w_ok, label="②") is True
        ctx = _ctx(None)
        w_bad, _ = _notion_writer(ok=False)
        assert ctx.persist(self._price(), w_bad, label="②") is False
        assert ctx.notion_failed == 1

    def test_mark_absent_failover(self):
        local = _RecordingLocal()
        ctx = _ctx(local)
        w, _ = _notion_writer(ok=False)
        assert ctx.persist_mark_absent("7203", w, label="absent") is True
        assert ("absent", "7203") in local.calls and ctx.notion_failed == 1

    def test_lifecycle_failover(self):
        local = _RecordingLocal()
        ctx = _ctx(local)
        w, _ = _notion_writer(ok=False)
        rec = DisclosureRecord(
            doc_id="d", title="t", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
            code="7203", doc_type="上場廃止", provenance=_prov(),
        )
        assert ctx.persist_lifecycle(rec, w, label="life") is True
        assert ("lifecycle",) in local.calls and ctx.notion_failed == 1


class TestUploadRawFailover:
    def test_both_succeed_returns_page_id(self, monkeypatch):
        from jp_stock_pipeline.notion import file_upload

        monkeypatch.setattr(file_upload, "upload_raw_artifact", _ok_uploader("p1"))
        local = _RecordingLocal()
        ctx = _ctx(local)
        assert ctx.upload_raw(_raw()) == "p1"
        assert local.calls == [("raw",)]

    def test_notion_fails_local_succeeds_returns_none(self, monkeypatch):
        # Notion ⑤ 失敗でもローカル ⑤ に原本が残れば構造化続行可（page_id=None）
        from jp_stock_pipeline.notion import file_upload

        monkeypatch.setattr(file_upload, "upload_raw_artifact", _fail_uploader)
        local = _RecordingLocal()
        ctx = _ctx(local)
        assert ctx.upload_raw(_raw()) is None
        assert ctx.notion_failed == 1 and local.calls == [("raw",)]

    def test_notion_ok_local_fails_degrades(self, monkeypatch):
        from jp_stock_pipeline.notion import file_upload

        monkeypatch.setattr(file_upload, "upload_raw_artifact", _ok_uploader("p1"))
        ctx = _ctx(_RaisingLocal())
        assert ctx.upload_raw(_raw()) == "p1"  # Notion ⑤ には残る
        assert ctx.mirror_failed == 1

    def test_both_fail_raises(self, monkeypatch):
        # 原本ゼロ（両系統失敗）は §3-3 違反 → 取得単位中止
        from jp_stock_pipeline.notion import file_upload

        monkeypatch.setattr(file_upload, "upload_raw_artifact", _fail_uploader)
        ctx = _ctx(_RaisingLocal())
        with pytest.raises(file_upload.RawUploadError):
            ctx.upload_raw(_raw())

    def test_notion_fails_local_none_raises(self, monkeypatch):
        # ローカル未接続 + Notion 失敗 = 原本ゼロ → 従来どおり RawUploadError
        from jp_stock_pipeline.notion import file_upload

        monkeypatch.setattr(file_upload, "upload_raw_artifact", _fail_uploader)
        ctx = _ctx(None)
        with pytest.raises(file_upload.RawUploadError):
            ctx.upload_raw(_raw())


# --- 接続プロファイル cloud/lan と --db-target -------------------------------


class TestConnectionProfiles:
    def _settings(self, **kw):
        from jp_stock_pipeline.config import LocalStoreSettings

        base = dict(
            host="cloud.example", port=5432, dbname="jp_stock", user="u",
            password="p w0rd", api_key=None, lan_host="192.168.1.50",
            sslmode="require", lan_sslmode="prefer",
        )
        base.update(kw)
        return LocalStoreSettings(**base)

    def test_cloud_profile_uses_host_and_require(self):
        kw = self._settings().connect_kwargs("cloud")
        assert kw["host"] == "cloud.example"
        assert kw["sslmode"] == "require"
        # 空白入りパスワードも keyword 引数なので安全に渡る
        assert kw["password"] == "p w0rd"

    def test_lan_profile_uses_lan_host_and_prefer(self):
        kw = self._settings().connect_kwargs("lan")
        assert kw["host"] == "192.168.1.50"
        assert kw["sslmode"] == "prefer"

    def test_default_target_is_cloud(self):
        assert self._settings().connect_kwargs()["host"] == "cloud.example"

    def test_enabled_per_target(self):
        s = self._settings(host=None)  # cloud host 無し / lan_host 明示あり
        assert s.enabled("cloud") is False
        assert s.enabled("lan") is True
        # lan_host 未設定なら lan も無効（localhost への誤ミラーを防ぐ）
        assert self._settings(host=None, lan_host=None).enabled("lan") is False

    def test_load_settings_reads_both_profiles(self):
        from jp_stock_pipeline.config import load_settings

        env = {
            "LOCAL_DB_HOST": "cloud.h", "LOCAL_DB_LAN_HOST": "10.0.0.5",
            "LOCAL_DB_SSLMODE": "disable", "LOCAL_DB_PASSWORD": "pw",
        }
        s = load_settings(env=env).local_store
        assert s.host == "cloud.h"
        assert s.lan_host == "10.0.0.5"
        assert s.sslmode == "disable"  # VPN 経由などで TLS 不要時に上書き可

    def test_lan_unset_is_none_but_api_falls_back_localhost(self):
        from jp_stock_pipeline.config import load_settings

        s = load_settings(env={"LOCAL_DB_HOST": "x"}).local_store
        assert s.lan_host is None              # 未設定なら None（dual-write lan は無効）
        assert s.enabled("lan") is False
        # API(端末B 内)の自DB接続のみ localhost に補完される
        assert s.connect_kwargs("lan")["host"] == "localhost"

    def test_build_parser_db_target(self):
        from jp_stock_pipeline.jobs.runner import build_parser

        assert build_parser("x").parse_args([]).db_target == "cloud"
        assert build_parser("x").parse_args(["--db-target", "lan"]).db_target == "lan"


# --- ⑧ XBRL 全ファクト（ローカル専用・非構造化保持 §7.1） --------------------


def _fact_row(**kw) -> dict:
    """convert.xbrl_to_csv の tidy 1 行（dict）を作る。"""
    base = dict(
        code="7203", doc_id="S100ABCD", element="jpcrp_cor:NetSales",
        context_ref="CurrentYearDuration", period_start="2025-04-01",
        period_end="2026-03-31", instant_date="", consolidated="連結",
        unit="JPY", value="1000",
    )
    base.update(kw)
    return base


class TestXbrlFactsInsert:
    def test_sql_shape_and_placeholders(self):
        sql, params = mappers.xbrl_facts_insert([_fact_row()], _raw())
        assert "INSERT INTO xbrl_facts" in sql
        assert "ON CONFLICT (doc_id, element, context_ref) DO UPDATE" in sql
        assert "%(value)s" in sql and "%(is_text_block)s" in sql  # %()s 形式（注入対策）
        assert "updated_at = now()" in sql
        assert len(params) == 1

    def test_skips_empty_value(self):
        rows = [_fact_row(value=""), _fact_row(element="X", value="   "), _fact_row()]
        _sql, params = mappers.xbrl_facts_insert(rows, _raw())
        # 空・空白のみは欠損として格納しない（§3-1）
        assert len(params) == 1
        assert params[0]["element"] == "jpcrp_cor:NetSales"

    def test_dedup_by_pk_last_wins(self):
        rows = [_fact_row(value="100"), _fact_row(value="200")]  # 同一 PK
        _sql, params = mappers.xbrl_facts_insert(rows, _raw())
        assert len(params) == 1  # executemany の二重更新を避ける de-dup
        assert params[0]["value"] == "200"

    def test_is_text_block_flag(self):
        rows = [
            _fact_row(element="jpcrp_cor:BusinessRisksTextBlock", value="リスク本文"),
            _fact_row(element="jpcrp_cor:NetSales", context_ref="c2", value="1000"),
        ]
        _sql, params = mappers.xbrl_facts_insert(rows, _raw())
        by_elem = {p["element"]: p for p in params}
        assert by_elem["jpcrp_cor:BusinessRisksTextBlock"]["is_text_block"] is True
        assert by_elem["jpcrp_cor:NetSales"]["is_text_block"] is False

    def test_provenance_from_artifact(self):
        art = _raw()
        _sql, params = mappers.xbrl_facts_insert([_fact_row()], art)
        p = params[0]
        assert p["source"] == art.source.value
        assert p["license_tag"] == art.license_tag.value
        assert p["fetched_at"] == art.fetched_at

    def test_skips_rows_without_pk(self):
        rows = [_fact_row(context_ref=""), _fact_row(element="")]
        _sql, params = mappers.xbrl_facts_insert(rows, _raw())
        assert params == []


class TestXbrlFactsSink:
    def test_executemany_called_and_count_returned(self):
        store = LocalStore(_FakeConn())
        rows = [_fact_row(), _fact_row(context_ref="c2")]
        n = store.upsert_xbrl_facts(rows, _raw())
        assert n == 2
        assert len(store._conn.cur.executed) == 1
        sql, params = store._conn.cur.executed[0]
        assert "INSERT INTO xbrl_facts" in sql and len(params) == 2

    def test_empty_is_noop_returns_zero(self):
        store = LocalStore(_FakeConn())
        assert store.upsert_xbrl_facts([_fact_row(value="")], _raw()) == 0
        assert store._conn.cur.executed == []  # 実行しない


class TestMirrorXbrlFacts:
    def test_dispatch_with_list_rows(self):
        local = _RecordingLocal()
        assert _ctx(local).mirror_xbrl_facts([_fact_row()], _raw()) is True
        assert local.calls == [("facts", 1)]

    def test_dataframe_converted_to_records(self):
        import pandas as pd

        local = _RecordingLocal()
        df = pd.DataFrame([_fact_row(), _fact_row(context_ref="c2")])
        _ctx(local).mirror_xbrl_facts(df, _raw())
        assert local.calls == [("facts", 2)]

    def test_noop_when_local_none(self):
        assert _ctx(None).mirror_xbrl_facts([_fact_row()], _raw()) is None

    def test_noop_when_empty(self):
        local = _RecordingLocal()
        assert _ctx(local).mirror_xbrl_facts([], _raw()) is None
        assert local.calls == []
