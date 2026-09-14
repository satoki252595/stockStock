"""③財務サマリ `jss_financials` の writer (移行 P5)。

実通信はしない。`cloud_store/schema.py` の本物の DDL をローカル sqlite に
流し、writer が生成した SQL を**そのまま実行**して結果を見る。SQL 文の
文字列一致だけでは「連結と単体が別行として残る」ことを確認できない
（D1 も SQLite なので、ここで通れば本番でも同じ意味になる）。

守るべき不変条件:
- 連結行と単体行が**共存する**（旧 PK の後勝ちの回帰テスト）
- 訂正開示が完全な行を NULL で潰さない
- 緩いライセンスタグが厳しいタグを洗わない
- 古い開示が新しい開示を巻き戻さない
- `fin is None` のとき writer を呼ばない / dry-run で 1 文も書かない
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from _doubles import SqliteD1

from jp_stock_pipeline.cloud_store import financials as F
from jp_stock_pipeline.cloud_store import schema as S
from jp_stock_pipeline.cloud_store.d1 import D1Error
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import (
    DataQuality,
    FinancialSummaryRecord,
    Provenance,
    Source,
)

JST = timezone.utc


def _record(
    *,
    code: str = "7203",
    consolidated: str | None = "連結",
    disclosure_type: str = "本決算",
    source: Source = Source.EDINET,
    license_tag: LicenseTag = LicenseTag.COMMERCIAL_OK,
    disclosed_at: datetime | None = datetime(2026, 6, 25, 15, 0, tzinfo=JST),
    **values,
) -> FinancialSummaryRecord:
    prov = Provenance(
        source=source,
        license_tag=license_tag,
        data_date=date(2026, 6, 25),
        fetched_at=datetime(2026, 6, 25, 21, 0, tzinfo=JST),
        quality=DataQuality.OK,
    )
    return FinancialSummaryRecord(
        code=code,
        fiscal_period_end=date(2026, 3, 31),
        disclosure_type=disclosure_type,
        provenance=prov,
        consolidated=consolidated,
        accounting_standard="日本基準",
        disclosed_at=disclosed_at,
        **values,
    )


class FakeStore(SqliteD1):
    """sqlite 裏打ちの `D1Store` + `core_stocks(id, code)` の最小形。"""

    def __init__(self) -> None:
        super().__init__(
            ddl=(
                *S.SCHEMA_STATEMENTS,
                "CREATE TABLE core_stocks (id INTEGER PRIMARY KEY, code TEXT NOT NULL)",
            ),
            seed=(("INSERT INTO core_stocks (id, code) VALUES (11, '7203')", ()),),
        )

    # --- テスト用の読み出し ---
    def fin_rows(self) -> list[dict]:
        return self.query(
            "SELECT * FROM jss_financials"
            " ORDER BY code, fiscal_period_end, disclosure_type, consolidated"
        )


def _write(store: FakeStore, record, *, doc_id="S100AAAA", raw_sha256="a" * 64) -> int:
    stock_id = F.resolve_stock_id(store, record.code)
    return F.write(
        store,
        [F.record_to_row(record, stock_id=stock_id, doc_id=doc_id, raw_sha256=raw_sha256)],
    )


class TestColumns:
    def test_thirty_three_columns(self) -> None:
        """レコード+Provenance 30 列 + doc_id + raw_sha256 + stock_id = 33。

        29 と数えると 1 列落ちる（`roe_pct` / `roa_pct` は常に None だが
        列としては存在する）。
        """
        assert len(F.COLUMNS) == 33
        assert len(set(F.COLUMNS)) == 33

    def test_columns_match_the_ddl(self) -> None:
        ddl = next(s for s in S.SCHEMA_STATEMENTS if "jss_financials" in s)
        declared = [
            line.strip().split()[0]
            for line in ddl.splitlines()
            if line.startswith("  ") and not line.strip().startswith("PRIMARY KEY")
        ]
        assert declared == list(F.COLUMNS)

    def test_every_column_is_classified_exactly_once(self) -> None:
        """分類漏れの列は SET 句から落ちて「永久に更新されない列」になる。"""
        classified = (
            set(S.FINANCIALS_PK)
            | set(F.MERGE_COLUMNS)
            | set(F.OVERWRITE_COLUMNS)
            | {F.LICENSE_COLUMN}
        )
        assert classified == set(F.COLUMNS)
        assert len(S.FINANCIALS_PK) + len(F.MERGE_COLUMNS) + len(
            F.OVERWRITE_COLUMNS
        ) + 1 == len(F.COLUMNS)

    def test_roe_and_roa_are_carried_as_columns_even_though_always_none(self) -> None:
        assert "roe_pct" in F.COLUMNS
        assert "roa_pct" in F.COLUMNS


class TestSql:
    def test_conflict_target_is_the_pk_including_consolidated(self) -> None:
        sql = F.build_upsert_sql(1)
        assert (
            "ON CONFLICT (code, fiscal_period_end, disclosure_type, consolidated)" in sql
        )

    def test_pk_columns_are_never_assigned(self) -> None:
        """主キーを自分で上書きしない。"""
        sql = F.build_upsert_sql(1)
        for column in S.FINANCIALS_PK:
            assert f"{column} = excluded.{column}" not in sql

    def test_value_columns_are_merged_with_coalesce(self) -> None:
        sql = F.build_upsert_sql(1)
        assert "net_sales = COALESCE(excluded.net_sales, jss_financials.net_sales)" in sql
        assert "eps = COALESCE(excluded.eps, jss_financials.eps)" in sql

    def test_guard_prevents_an_older_disclosure_from_winning(self) -> None:
        sql = F.build_upsert_sql(1)
        assert (
            "WHERE excluded.disclosed_at >= jss_financials.disclosed_at"
            " OR jss_financials.disclosed_at IS NULL" in sql
        )

    def test_license_tag_is_not_a_plain_overwrite(self) -> None:
        sql = F.build_upsert_sql(1)
        assert "license_tag = excluded.license_tag," not in sql
        assert "CASE excluded.license_tag" in sql

    def test_unknown_license_tag_ranks_as_the_strictest(self) -> None:
        """未知のタグを緩い側へ倒すと公開フィルタを素通りする。"""
        from jp_stock_pipeline.licensing import strictness_rank_sql

        expression = strictness_rank_sql("x")
        assert "ELSE 3 END" in expression

    def test_zero_rows_is_rejected(self) -> None:
        with pytest.raises(D1Error):
            F.build_upsert_sql(0)


class TestConsolidatedCoexistence:
    def test_consolidated_and_standalone_rows_coexist(self) -> None:
        """旧 PK の後勝ちの回帰テスト。

        旧 PK `(code, fiscal_period_end, disclosure_type)` では、同一期末の
        単体行が連結行を潰していた。
        """
        store = FakeStore()
        _write(store, _record(consolidated="連結", net_sales=1000.0))
        _write(store, _record(consolidated="単体", net_sales=50.0))
        rows = store.fin_rows()
        assert [(r["consolidated"], r["net_sales"]) for r in rows] == [
            ("単体", 50.0),
            ("連結", 1000.0),
        ]

    def test_undetermined_consolidation_becomes_an_explicit_value(self) -> None:
        """NULL のままだと PK が効かず同じキーの行が毎回増える。"""
        store = FakeStore()
        _write(store, _record(consolidated=None, net_sales=7.0))
        _write(store, _record(consolidated=None, net_sales=8.0))
        rows = store.fin_rows()
        assert len(rows) == 1
        assert rows[0]["consolidated"] == F.UNKNOWN_CONSOLIDATED
        assert rows[0]["net_sales"] == 8.0

    def test_unknown_row_does_not_collide_with_the_consolidated_row(self) -> None:
        store = FakeStore()
        _write(store, _record(consolidated="連結", net_sales=1000.0))
        _write(store, _record(consolidated=None, net_sales=9.0))
        assert len(store.fin_rows()) == 2


class TestCorrectionMerge:
    def test_a_partial_correction_does_not_null_out_a_complete_row(self) -> None:
        """`edinet_daily` は 120(有報) と 130(訂正有報) を同じ '本決算' に落とす。

        訂正は訂正した項目だけを載せるので、無条件の `excluded.c` 上書きだと
        完全な行の残りの列が全部 NULL になる。
        """
        store = FakeStore()
        _write(
            store,
            _record(net_sales=1000.0, eps=120.5, cf_operating=333.0, dps_actual=60.0),
        )
        # 訂正: 売上だけを載せ、他は None
        _write(
            store,
            _record(
                net_sales=1100.0,
                disclosed_at=datetime(2026, 7, 10, 15, 0, tzinfo=JST),
            ),
            doc_id="S100BBBB",
            raw_sha256="b" * 64,
        )
        row = store.fin_rows()[0]
        assert row["net_sales"] == 1100.0  # 訂正が載せた値は反映される
        assert row["eps"] == 120.5  # 載せていない値は残る
        assert row["cf_operating"] == 333.0
        assert row["dps_actual"] == 60.0

    def test_an_older_disclosure_cannot_roll_back_a_correction(self) -> None:
        store = FakeStore()
        _write(
            store,
            _record(
                net_sales=1100.0,
                disclosed_at=datetime(2026, 7, 10, 15, 0, tzinfo=JST),
            ),
        )
        _write(store, _record(net_sales=1000.0))  # 6/25 の古い開示を再取得
        assert store.fin_rows()[0]["net_sales"] == 1100.0

    def test_reprocessing_the_same_disclosure_is_idempotent(self) -> None:
        store = FakeStore()
        _write(store, _record(net_sales=1000.0, eps=120.5))
        _write(store, _record(net_sales=1000.0, eps=120.5))
        rows = store.fin_rows()
        assert len(rows) == 1
        assert (rows[0]["net_sales"], rows[0]["eps"]) == (1000.0, 120.5)

    def test_stock_id_is_not_erased_when_it_cannot_be_resolved(self) -> None:
        store = FakeStore()
        _write(store, _record(net_sales=1000.0))
        assert store.fin_rows()[0]["stock_id"] == 11
        F.write(
            store,
            [
                F.record_to_row(
                    _record(
                        net_sales=1200.0,
                        disclosed_at=datetime(2026, 7, 10, tzinfo=JST),
                    ),
                    stock_id=None,
                    doc_id="S100BBBB",
                    raw_sha256="b" * 64,
                )
            ],
        )
        assert store.fin_rows()[0]["stock_id"] == 11


class TestLicenseTag:
    def test_a_tdnet_correction_makes_the_row_stricter(self) -> None:
        store = FakeStore()
        _write(store, _record(net_sales=1000.0))
        _write(
            store,
            _record(
                net_sales=1100.0,
                source=Source.TDNET,
                license_tag=LicenseTag.FACTUAL_CITE,
                disclosed_at=datetime(2026, 7, 10, tzinfo=JST),
            ),
        )
        assert store.fin_rows()[0]["license_tag"] == LicenseTag.FACTUAL_CITE.value

    def test_an_edinet_write_does_not_launder_a_factual_cite_row(self) -> None:
        """混在した行を commercial-ok に戻すと、公開面へ全量が出てしまう。"""
        store = FakeStore()
        _write(
            store,
            _record(
                net_sales=1000.0,
                source=Source.TDNET,
                license_tag=LicenseTag.FACTUAL_CITE,
            ),
        )
        _write(
            store,
            _record(
                net_sales=1100.0,
                disclosed_at=datetime(2026, 7, 10, tzinfo=JST),
            ),
        )
        row = store.fin_rows()[0]
        assert row["net_sales"] == 1100.0
        assert row["license_tag"] == LicenseTag.FACTUAL_CITE.value
        assert row["source"] == Source.EDINET.value  # 来歴は最後に書いた側


class TestRowShape:
    def test_dates_and_epochs_match_the_column_types(self) -> None:
        row = F.record_to_row(
            _record(net_sales=1.0), stock_id=11, doc_id="S1", raw_sha256="a" * 64
        )
        values = dict(zip(F.COLUMNS, row, strict=True))
        assert values["fiscal_period_end"] == "2026-03-31"
        assert values["data_date"] == "2026-06-25"
        assert isinstance(values["disclosed_at"], int)
        assert isinstance(values["fetched_at"], int)

    def test_missing_code_is_rejected_before_sending(self) -> None:
        with pytest.raises(D1Error, match="code"):
            F.record_to_row(
                _record(code=""), stock_id=None, doc_id=None, raw_sha256=None
            )

    def test_ragged_row_is_rejected_before_sending(self) -> None:
        store = FakeStore()
        with pytest.raises(D1Error, match="値の数が列数と違う"):
            F.write(store, [[1, 2, 3]])
        assert store.sql_log == []

    def test_chunking_respects_the_bind_limit(self) -> None:
        """33 列 → 3 行/リクエスト。超えると実行時に落ちる。"""
        store = FakeStore()
        rows = [
            F.record_to_row(
                _record(code=f"{1000 + i}", net_sales=float(i)),
                stock_id=None,
                doc_id=f"S{i}",
                raw_sha256="a" * 64,
            )
            for i in range(7)
        ]
        assert F.write(store, rows) == 7
        assert len(store.sql_log) == 3  # 3 + 3 + 1
        assert len(store.fin_rows()) == 7

    def test_empty_rows_is_a_noop(self) -> None:
        store = FakeStore()
        assert F.write(store, []) == 0
        assert store.sql_log == []


class TestStockId:
    def test_resolves_through_the_unique_code_index(self) -> None:
        store = FakeStore()
        assert F.resolve_stock_id(store, "7203") == 11
        assert store.sql_log == [F.STOCK_ID_SQL]

    def test_absent_code_is_null_not_an_error(self) -> None:
        """ETF・優先株は core_stocks に無い。NULL が正しい（孤児ではない）。"""
        store = FakeStore()
        assert F.resolve_stock_id(store, "1234") is None

    def test_empty_code_does_not_query(self) -> None:
        store = FakeStore()
        assert F.resolve_stock_id(store, "") is None
        assert store.sql_log == []

    def test_cache_avoids_repeating_the_same_select(self) -> None:
        store = FakeStore()
        cache: dict[str, int | None] = {}
        assert F.resolve_stock_id(store, "7203", cache=cache) == 11
        assert F.resolve_stock_id(store, "7203", cache=cache) == 11
        assert len(store.sql_log) == 1

    def test_cache_remembers_the_miss_too(self) -> None:
        store = FakeStore()
        cache: dict[str, int | None] = {}
        F.resolve_stock_id(store, "1234", cache=cache)
        F.resolve_stock_id(store, "1234", cache=cache)
        assert len(store.sql_log) == 1


class TestCountRows:
    def test_count_is_measured_not_taken_from_the_write_result(self) -> None:
        """`write()` の戻り値はチャンクの行数で、反映行数ではない。

        `disclosed_at` ガードが効いて 1 行も更新されなくても SQLite は
        エラーを返さないため、両者は一致しない。
        """
        store = FakeStore()
        _write(
            store,
            _record(net_sales=1100.0, disclosed_at=datetime(2026, 7, 10, tzinfo=JST)),
        )
        assert _write(store, _record(net_sales=1.0)) == 1  # 古い開示。反映されない
        assert F.count_rows(store) == 1
        assert store.fin_rows()[0]["net_sales"] == 1100.0


class TestOldPkRegression:
    def test_the_previous_pk_would_have_lost_the_consolidated_row(self) -> None:
        """「PK を直さないと writer がバグを再生産する」ことの明示。

        旧 PK の器へ同じ 2 行を流すと 1 行に潰れることを実際に示す。
        """
        con = sqlite3.connect(":memory:")
        ddl = next(s for s in S.SCHEMA_STATEMENTS if "jss_financials" in s)
        old_ddl = ddl.replace(
            "PRIMARY KEY (code, fiscal_period_end, disclosure_type, consolidated)",
            "PRIMARY KEY (code, fiscal_period_end, disclosure_type)",
        ).replace("consolidated              TEXT NOT NULL", "consolidated  TEXT")
        con.execute(old_ddl.strip())
        insert = (
            "INSERT INTO jss_financials"
            " (code, fiscal_period_end, disclosure_type, consolidated, net_sales,"
            "  source, license_tag, fetched_at, quality)"
            " VALUES (?, ?, ?, ?, ?, 'EDINET', 'commercial-ok', 1, '正常')"
            " ON CONFLICT (code, fiscal_period_end, disclosure_type) DO UPDATE SET"
            "  consolidated = excluded.consolidated, net_sales = excluded.net_sales"
        )
        con.execute(insert, ["7203", "2026-03-31", "本決算", "連結", 1000.0])
        con.execute(insert, ["7203", "2026-03-31", "本決算", "単体", 50.0])
        assert con.execute(
            "SELECT consolidated, net_sales FROM jss_financials"
        ).fetchall() == [("単体", 50.0)]  # 連結が消えた


class _RecordingCloud:
    """`CloudSink` の代わり。③ の呼び出しだけを記録する。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def upsert_financial_summary(self, record, *, doc_id, raw_sha256):
        self.calls.append((record, doc_id, raw_sha256))
        return True


class TestEdinetDailyWiring:
    """`jobs/edinet_daily.py` の ③ → Cloudflare の配線。

    挿入位置を間違えると (a) raise の次の行 = 到達不能 (b) 外側インデント =
    `fin is None` を踏む のどちらかになる。両方を固定する。
    """

    def _run(self, monkeypatch, tmp_path, *, fin):
        from argparse import Namespace
        from hashlib import sha256

        from jp_stock_pipeline.config import load_settings
        from jp_stock_pipeline.jobs import edinet_daily as job
        from jp_stock_pipeline.jobs.runner import JobContext
        from jp_stock_pipeline.licensing import LicenseTag
        from jp_stock_pipeline.models import JST, RawArtifact

        path = tmp_path / "edinet_csv_7203_20260625.zip"
        path.write_bytes(b"PK\x03\x04payload")
        digest = sha256(path.read_bytes()).hexdigest()
        artifact = RawArtifact(
            source=Source.EDINET, datatype="csv", scope="7203",
            data_date=date(2026, 6, 25),
            fetched_at=datetime(2026, 6, 25, tzinfo=JST),
            url="https://example/doc?type=5", local_path=path, sha256=digest,
            size_bytes=path.stat().st_size, license_tag=LicenseTag.COMMERCIAL_OK,
        )
        cloud = _RecordingCloud()
        ctx = JobContext(
            settings=load_settings(dry_run=True, env={}), client=None,
            args=Namespace(kabumcp_cache_dir=None),
        )
        ctx.cloud = cloud
        tidy = object()
        monkeypatch.setattr(job, "_fetch_financial_tidy", lambda *a: (artifact, tidy))
        monkeypatch.setattr(
            job.edinet, "fetch_document",
            lambda *a, **kw: (_ for _ in ()).throw(job.FetchError("no pdf")),
        )
        monkeypatch.setattr(ctx, "upload_raw", lambda *a, **kw: "raw-page")
        monkeypatch.setattr(ctx, "mirror_xbrl_facts", lambda *a: None)
        monkeypatch.setattr(ctx, "persist", lambda *a, **kw: True)
        monkeypatch.setattr(
            job.normalize, "tidy_to_financial_record", lambda *a, **kw: fin
        )
        doc = {
            "docID": "S1001234", "docTypeCode": "120", "secCode": "72030",
            "submitDateTime": "2026-06-25 15:00",
        }
        job._process_document(ctx, doc, "list-page", master_map_ok=True)
        return cloud, digest

    def test_writer_is_called_with_the_raw_sha256(self, monkeypatch, tmp_path) -> None:
        fin = _record(net_sales=1000.0)
        cloud, digest = self._run(monkeypatch, tmp_path, fin=fin)
        assert cloud.calls == [(fin, "S1001234", digest)]

    def test_writer_is_not_called_when_the_record_could_not_be_built(
        self, monkeypatch, tmp_path
    ) -> None:
        """決算期末が導出できない書類では ③ レコードが作られない。"""
        cloud, _digest = self._run(monkeypatch, tmp_path, fin=None)
        assert cloud.calls == []


class TestDatasetLicenseTag:
    def test_the_dataset_tag_is_derived_not_hardcoded(self) -> None:
        """EDINET と TDnet が混ざる表なので最も厳しい側へ倒す。"""
        from jp_stock_pipeline.cloud_store.datasets import DATASET_SOURCE_BY_NAME
        from jp_stock_pipeline.licensing import inherit

        source = DATASET_SOURCE_BY_NAME["financials"]
        assert source.license_tag == inherit(
            [LicenseTag.COMMERCIAL_OK, LicenseTag.FACTUAL_CITE]
        ).value
        assert source.license_tag == LicenseTag.FACTUAL_CITE.value


class TestPrefetchStockIds:
    """繁忙日の往復回数を削るための一括解決（走査行は増やさない）。"""

    def test_one_request_resolves_many_codes(self) -> None:
        store = FakeStore()
        store.con.execute("INSERT INTO core_stocks (id, code) VALUES (12, '6758')")
        store.con.commit()
        cache: dict[str, int | None] = {}
        assert F.prefetch_stock_ids(store, ["7203", "6758"], cache=cache) == 2
        assert len(store.sql_log) == 1
        assert cache == {"7203": 11, "6758": 12}

    def test_misses_are_cached_so_they_do_not_fall_back_per_code(self) -> None:
        """覚えないと per-code SELECT へ落ちてまとめた意味が無くなる。"""
        store = FakeStore()
        cache: dict[str, int | None] = {}
        F.prefetch_stock_ids(store, ["1234"], cache=cache)
        assert cache == {"1234": None}
        F.resolve_stock_id(store, "1234", cache=cache)
        assert len(store.sql_log) == 1

    def test_already_cached_codes_are_not_queried_again(self) -> None:
        store = FakeStore()
        cache: dict[str, int | None] = {"7203": 11}
        assert F.prefetch_stock_ids(store, ["7203"], cache=cache) == 0
        assert store.sql_log == []

    def test_batches_stay_within_the_bind_limit(self) -> None:
        store = FakeStore()
        codes = [f"{1000 + i}" for i in range(200)]
        cache: dict[str, int | None] = {}
        assert F.prefetch_stock_ids(store, codes, cache=cache) == 200
        # 90 件/回 → 200 件は 3 回。1 件ずつなら 200 回だった。
        assert len(store.sql_log) == 3
        assert len(cache) == 200

    def test_blank_codes_are_dropped(self) -> None:
        store = FakeStore()
        cache: dict[str, int | None] = {}
        assert F.prefetch_stock_ids(store, ["", None], cache=cache) == 0
        assert store.sql_log == []

    def test_full_table_map_is_not_used(self) -> None:
        """`SELECT id, code FROM core_stocks` は 4,445 行を毎回読む。

        D1 の課金軸は走査行なので、必要なのが 61 件でも全件マップは高くつく。
        """
        store = FakeStore()
        cache: dict[str, int | None] = {}
        F.prefetch_stock_ids(store, ["7203"], cache=cache)
        assert "WHERE code IN (?)" in store.sql_log[0]


class TestTdnetFinancialPredicate:
    """事前解決と実処理が同じ条件を見ていること。

    食い違うと「先に解決したコード」と「実際に使うコード」がずれ、per-code
    SELECT に落ちる。静かに遅くなるだけなので気付けない。
    """

    def _record(self, **kw):
        from jp_stock_pipeline.models import DisclosureRecord

        prov = Provenance(
            source=Source.TDNET,
            license_tag=LicenseTag.FACTUAL_CITE,
            data_date=date(2026, 6, 25),
            fetched_at=datetime(2026, 6, 25, tzinfo=JST),
        )
        base = dict(
            doc_id="D1",
            title="2026年3月期 決算短信",
            disclosed_at=datetime(2026, 6, 25, 15, 0, tzinfo=JST),
            code="7203",
            doc_type="短信",
            source_url="https://example/d",
            has_xbrl=True,
            provenance=prov,
        )
        base.update(kw)
        return DisclosureRecord(**base)

    def test_matches_only_tanshin_with_a_reachable_xbrl(self) -> None:
        from jp_stock_pipeline.jobs.tdnet_hourly import _has_financial_xbrl

        urls = {"D1": "https://example/x.zip"}
        assert _has_financial_xbrl(self._record(), urls) is True
        assert _has_financial_xbrl(self._record(doc_type="決算説明資料"), urls) is False
        assert _has_financial_xbrl(self._record(has_xbrl=False), urls) is False
        assert _has_financial_xbrl(self._record(), {}) is False
