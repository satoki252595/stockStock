"""Cloudflare 正本ストアの単体テスト (docs/CF-CANONICAL-DESIGN.md)。

実通信はしない。R2 の後退禁止ガードと immutable キー命名は、破ると
再取得不能なデータ（信用残 2026-06-12〜07-31 は JPX から取り直せない）を
失うため、ここで機械的に固定する。
"""

from __future__ import annotations

from datetime import date

import pytest

from jp_stock_pipeline.cloud_store import keys
from jp_stock_pipeline.cloud_store.guards import (
    GuardError,
    check_contract_keys,
    check_no_regression,
)

# 公開 Worker が依存する契約キー（削除・改名が禁止）
MARGIN_CONTRACT = {
    "$": ("week",),
    "rows[]": ("code", "sell", "sell_chg", "buy", "buy_chg"),
}


class TestKeys:
    def test_raw_key_includes_doc_id_and_sha(self):
        key = keys.raw_key(
            source="EDINET", datatype="PDF", scope="8306",
            data_date=date(2024, 7, 29),
            sha256="a" * 64, ext=".pdf", doc_id="S100ABCD",
        )
        assert key == "raw/edinet/pdf/2024/2024-07-29/8306/S100ABCD/aaaaaaaaaaaaaaaa.pdf"

    def test_same_scope_and_date_but_different_doc_id_never_collide(self):
        """(scope, date) では一意にならない実測事例の再発防止。

        edinet_pdf_8306_20240729 には 250 個の別内容原本が存在した。
        """
        common = dict(
            source="edinet", datatype="pdf", scope="8306",
            data_date=date(2024, 7, 29), ext="pdf",
        )
        a = keys.raw_key(sha256="1" * 64, doc_id="S100AAAA", **common)
        b = keys.raw_key(sha256="2" * 64, doc_id="S100BBBB", **common)
        assert a != b

    def test_doc_id_absent_falls_back_without_breaking_key(self):
        key = keys.raw_key(
            source="edinet", datatype="documents_list", scope="ALL",
            data_date=date(2026, 9, 10), sha256="f" * 64, ext="json",
        )
        assert "/_/" in key  # 一覧は文書単位でないので fallback

    def test_raw_key_requires_sha256(self):
        with pytest.raises(ValueError):
            keys.raw_key(
                source="edinet", datatype="pdf", scope="8306",
                data_date=date(2024, 7, 29), sha256="", ext="pdf",
            )

    def test_derived_key_mirrors_raw_key(self):
        raw = keys.raw_key(
            source="edinet", datatype="xbrl", scope="7203",
            data_date=date(2026, 6, 30), sha256="b" * 64, ext="zip", doc_id="S100XU9L",
        )
        assert keys.derived_key(raw, suffix="csv") == (
            "derived/edinet/xbrl/2026/2026-06-30/7203/S100XU9L/bbbbbbbbbbbbbbbb.csv"
        )

    def test_derived_key_rejects_non_raw(self):
        with pytest.raises(ValueError):
            keys.derived_key("supply/7203.json", suffix="csv")

    def test_margin_key_matches_existing_layout(self):
        """既存 10 週と同じ形。変えると公開 Worker が読めなくなる。"""
        assert keys.margin_key(date(2026, 6, 12)) == "margin/2026-06-12.json"


class TestNoRegressionGuard:
    def test_new_object_is_allowed(self):
        check_no_regression(None, {"writer": "w", "bars": []}, writer="w")

    def test_shrinking_array_is_rejected(self):
        old = {"writer": "w", "bars": [{"date": "2026-01-01"}, {"date": "2026-01-02"}]}
        new = {"writer": "w", "bars": [{"date": "2026-01-01"}]}
        with pytest.raises(GuardError, match="ガード2違反"):
            check_no_regression(old, new, writer="w")

    def test_weeks_json_cannot_be_emptied(self):
        """margin/weeks.json は公開 Worker の信用残 API の唯一の入口。

        空配列で上書きすると R2 上にオブジェクトが残っていても画面上は
        データが消えたのと同じになる。既存 10 週は再取得不能。
        """
        old = ["2026-06-12", "2026-06-19", "2026-07-31"]
        with pytest.raises(GuardError, match="ガード2違反"):
            check_no_regression(old, [], writer="w", require_writer=False)

    def test_same_length_but_swapped_elements_is_rejected(self):
        old = ["2026-06-12", "2026-06-19"]
        new = ["2026-08-07", "2026-08-14"]
        with pytest.raises(GuardError, match="ガード3違反"):
            check_no_regression(old, new, writer="w", require_writer=False)

    def test_appending_is_allowed(self):
        old = ["2026-06-12", "2026-06-19"]
        new = ["2026-06-12", "2026-06-19", "2026-08-07"]
        check_no_regression(old, new, writer="w", require_writer=False)

    def test_nested_series_array_is_guarded(self):
        """supply/{code}.json の series.* のようなネストした配列も対象。"""
        old = {"writer": "w", "series": {"jsf_zandaka": [{"d": "2026-09-01"}]}}
        new = {"writer": "w", "series": {"jsf_zandaka": []}}
        with pytest.raises(GuardError, match="series.jsf_zandaka"):
            check_no_regression(old, new, writer="w")

    def test_foreign_writer_on_existing_object_is_rejected(self):
        """R2 の 429 はキーが分散する経路では出ないので payload で検知する。"""
        old = {"writer": "kabulab-cf", "bars": []}
        new = {"writer": "stockStock", "bars": []}
        with pytest.raises(GuardError, match="ガード5違反"):
            check_no_regression(old, new, writer="stockStock")

    def test_payload_writer_must_match_self(self):
        with pytest.raises(GuardError, match="ガード5違反"):
            check_no_regression(None, {"writer": "other"}, writer="stockStock")

    def test_first_date_must_not_regress(self):
        old = {"writer": "w", "first_date": "2016-01-04", "bars": []}
        new = {"writer": "w", "first_date": "2026-01-04", "bars": []}
        with pytest.raises(GuardError, match="ガード4違反"):
            check_no_regression(old, new, writer="w")


class TestContractKeys:
    def test_margin_contract_passes(self):
        payload = {
            "week": "2026-09-04",
            "rows": [{"code": "1301", "sell": 1, "sell_chg": 0, "buy": 2, "buy_chg": 0}],
        }
        check_contract_keys(payload, MARGIN_CONTRACT)

    def test_missing_top_level_key_is_rejected(self):
        with pytest.raises(GuardError, match="トップレベル"):
            check_contract_keys({"rows": []}, MARGIN_CONTRACT)

    def test_missing_row_key_is_rejected(self):
        payload = {"week": "2026-09-04", "rows": [{"code": "1301", "sell": 1}]}
        with pytest.raises(GuardError, match=r"rows\[0\]"):
            check_contract_keys(payload, MARGIN_CONTRACT)


class TestR2StoreHasNoDelete:
    def test_delete_object_is_intentionally_absent(self):
        """delete_object を実装しないのは設計判断であり実装漏れではない。

        既存 10 週 (2026-06-12〜、うち 07-03・07-10 は恒久欠測) を物理的に
        消せなくするためのガード。復活させるならユーザー判断を先に仰ぐこと。
        """
        from jp_stock_pipeline.cloud_store.r2 import R2Store

        assert not hasattr(R2Store, "delete_object")
        assert not hasattr(R2Store, "delete")


class _FakeS3:
    """boto3 S3 クライアントの最小スタブ（実通信なし）。"""

    def __init__(self, objects: dict[str, bytes] | None = None, *, get_error=None):
        self.objects = dict(objects or {})
        self.get_error = get_error
        self.puts: list[tuple[str, bytes]] = []

    def get_object(self, *, Bucket, Key):  # noqa: N803 - boto3 の引数名に合わせる
        if self.get_error is not None:
            raise self.get_error
        if Key not in self.objects:
            raise _make_client_error("NoSuchKey", 404)
        import io

        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803
        self.objects[Key] = Body
        self.puts.append((Key, Body))

    def head_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _make_client_error("404", 404)
        return {}


def _make_client_error(code: str, status: int) -> Exception:
    exc = Exception(f"{code}")
    exc.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return exc


def _store(fake: _FakeS3, bucket: str = "jp-stock-raw", writer: str = "stockStock"):
    from jp_stock_pipeline.cloud_store.r2 import R2Store
    from jp_stock_pipeline.config import CloudStoreSettings

    store = R2Store(CloudStoreSettings(), bucket, writer=writer)
    store._s3 = fake  # noqa: SLF001 - テスト用に差し込む
    return store


class TestR2Store:
    def test_put_json_guarded_creates_new_object(self):
        import json

        fake = _FakeS3()
        _store(fake).put_json_guarded("margin/weeks.json", {"weeks": ["2026-06-12"]})
        assert len(fake.puts) == 1
        written = json.loads(fake.puts[0][1])
        assert written["weeks"] == ["2026-06-12"]
        assert written["writer"] == "stockStock"  # 自動付与される

    def test_put_json_guarded_refuses_to_shrink(self):
        import json

        from jp_stock_pipeline.cloud_store.r2 import R2Store  # noqa: F401

        existing = json.dumps(
            {"writer": "stockStock", "weeks": ["2026-06-12", "2026-06-19"]}
        ).encode()
        fake = _FakeS3({"margin/weeks.json": existing})
        with pytest.raises(GuardError):
            _store(fake).put_json_guarded("margin/weeks.json", {"weeks": ["2026-06-12"]})
        assert fake.puts == []  # PUT していない

    def test_get_failure_other_than_404_is_not_treated_as_absent(self):
        """ガード1: 「取れなかった」を「無かった」に潰すとガードが無力化する。"""
        from jp_stock_pipeline.cloud_store.r2 import R2Error

        fake = _FakeS3(get_error=_make_client_error("InternalError", 500))
        with pytest.raises(R2Error):
            _store(fake).put_json_guarded("margin/weeks.json", {"weeks": []})
        assert fake.puts == []

    def test_exists_is_false_on_404_and_raises_on_other_errors(self):
        from jp_stock_pipeline.cloud_store.r2 import R2Error

        assert _store(_FakeS3()).exists("raw/x") is False
        fake = _FakeS3()
        fake.head_object = lambda **_: (_ for _ in ()).throw(
            _make_client_error("InternalError", 500)
        )
        with pytest.raises(R2Error):
            _store(fake).exists("raw/x")


class TestD1Store:
    def _store(self, monkeypatch, captured: list, response: dict):
        from jp_stock_pipeline import http
        from jp_stock_pipeline.cloud_store.d1 import D1Store
        from jp_stock_pipeline.config import CloudStoreSettings

        class _Resp:
            def json(self):
                return response

        def fake_post(url, *, json_body, headers, idempotent=False, **kwargs):
            captured.append({"url": url, "body": json_body, "idempotent": idempotent})
            return _Resp()

        monkeypatch.setattr(http, "post_json", fake_post)
        settings = CloudStoreSettings(
            cf_account_id="acct", cf_api_token="tok", d1_database_id="db"
        )
        return D1Store(settings, writer="stockStock")

    def test_success_false_is_an_error_even_on_http_200(self, monkeypatch):
        """Cloudflare API は HTTP 200 でも body の success=false でエラーを返す。"""
        from jp_stock_pipeline.cloud_store.d1 import D1Error

        captured: list = []
        store = self._store(monkeypatch, captured, {"success": False, "errors": ["boom"]})
        with pytest.raises(D1Error, match="D1 エラー"):
            store.query("SELECT 1")

    def test_query_returns_rows(self, monkeypatch):
        captured: list = []
        store = self._store(
            monkeypatch, captured,
            {"success": True, "result": [{"results": [{"code": "7203"}]}]},
        )
        assert store.query("SELECT code FROM t") == [{"code": "7203"}]
        assert captured[0]["idempotent"] is True  # upsert/SELECT のみ通す前提

    def test_bound_param_limit_is_enforced(self, monkeypatch):
        """D1 の 1 リクエストのバインドパラメータ上限は 100。"""
        from jp_stock_pipeline.cloud_store.d1 import MAX_BOUND_PARAMS, D1Error

        captured: list = []
        store = self._store(monkeypatch, captured, {"success": True, "result": []})
        with pytest.raises(D1Error, match="バインドパラメータ上限"):
            store.query("SELECT 1", list(range(MAX_BOUND_PARAMS + 1)))

    def test_upsert_builds_conflict_clause_without_overwriting_pk(self, monkeypatch):
        captured: list = []
        store = self._store(monkeypatch, captured, {"success": True, "result": []})
        written = store.upsert(
            "jss_supply_latest",
            ["code", "data_date", "buy"],
            [["7203", "2026-09-10", 100]],
            conflict=["code"],
        )
        assert written == 1
        sql = captured[0]["body"]["sql"]
        assert "ON CONFLICT (code) DO UPDATE SET" in sql
        assert "data_date = excluded.data_date" in sql
        assert "code = excluded.code" not in sql  # 主キーは自分で上書きしない

    def test_keep_columns_are_written_back_from_the_table_not_excluded(self, monkeypatch):
        """`keep` に挙げた列は再送のたびに excluded.* で上書きしない (overlaps_refactor)。"""
        captured: list = []
        store = self._store(monkeypatch, captured, {"success": True, "result": []})
        store.upsert(
            "jss_raw_files",
            ["sha256", "first_fetched_at", "last_fetched_at"],
            [["a" * 64, 1, 2]],
            conflict=["sha256"],
            keep=["first_fetched_at"],
        )
        sql = captured[0]["body"]["sql"]
        assert "first_fetched_at = jss_raw_files.first_fetched_at" in sql
        assert "first_fetched_at = excluded.first_fetched_at" not in sql
        assert "last_fetched_at = excluded.last_fetched_at" in sql


class TestD1BatchUpsert:
    """複数行を1文にまとめて往復を減らす。

    1行ずつ投げると往復回数が行数と同じになり、jss_supply_latest の 4,755 行で
    約16分かかった（本番実測）。D1 のバインドパラメータ上限 100 から逆算して詰める。
    """

    def _store(self, monkeypatch, captured: list):
        from jp_stock_pipeline import http
        from jp_stock_pipeline.cloud_store.d1 import D1Store
        from jp_stock_pipeline.config import CloudStoreSettings

        class _Resp:
            def json(self):
                return {"success": True, "result": [{"results": []}]}

        def fake_post(url, *, json_body, headers, idempotent=False, **kwargs):
            captured.append(json_body)
            return _Resp()

        monkeypatch.setattr(http, "post_json", fake_post)
        return D1Store(
            CloudStoreSettings(cf_account_id="a", cf_api_token="t", d1_database_id="d"),
            writer="w",
        )

    def test_rows_per_request_is_derived_from_the_bind_limit(self, monkeypatch):
        store = self._store(monkeypatch, [])
        assert store.rows_per_request(14) == 7   # 100 // 14
        assert store.rows_per_request(100) == 1
        assert store.rows_per_request(3) == 33

    def test_many_rows_are_sent_in_few_requests(self, monkeypatch):
        captured: list = []
        store = self._store(monkeypatch, captured)
        rows = [[f"c{i}", "t", i] for i in range(100)]
        written = store.upsert("t", ["code", "data_type", "n"], rows, conflict=["code"])
        assert written == 100
        # 3 列なら 33 行/回 → 100 行は 4 回。1 行ずつなら 100 回だった。
        assert len(captured) == 4

    def test_params_are_flattened_in_row_order(self, monkeypatch):
        captured: list = []
        store = self._store(monkeypatch, captured)
        store.upsert("t", ["a", "b"], [[1, 2], [3, 4]], conflict=["a"])
        assert captured[0]["params"] == [1, 2, 3, 4]
        assert captured[0]["sql"].count("(?, ?)") == 2

    def test_never_exceeds_the_bind_limit(self, monkeypatch):
        from jp_stock_pipeline.cloud_store.d1 import MAX_BOUND_PARAMS

        captured: list = []
        store = self._store(monkeypatch, captured)
        columns = [f"c{i}" for i in range(14)]
        rows = [[i] * 14 for i in range(50)]
        store.upsert("t", columns, rows, conflict=["c0"])
        assert all(len(body["params"]) <= MAX_BOUND_PARAMS for body in captured)

    def test_ragged_row_is_rejected_before_sending(self, monkeypatch):
        from jp_stock_pipeline.cloud_store.d1 import D1Error

        captured: list = []
        store = self._store(monkeypatch, captured)
        with pytest.raises(D1Error, match="値の数が列数と違う"):
            store.upsert("t", ["a", "b"], [[1, 2], [3]], conflict=["a"])
        assert captured == []  # 送っていない

    def test_all_columns_as_conflict_key_is_rejected(self, monkeypatch):
        """更新する列が無い upsert は SQL が壊れるので事前に弾く。"""
        from jp_stock_pipeline.cloud_store.d1 import D1Error

        store = self._store(monkeypatch, [])
        with pytest.raises(D1Error, match="更新できる列が無い"):
            store.upsert("t", ["a"], [[1]], conflict=["a"])

    def test_empty_rows_is_a_noop(self, monkeypatch):
        captured: list = []
        store = self._store(monkeypatch, captured)
        assert store.upsert("t", ["a"], [], conflict=["a"]) == 0
        assert captured == []


class TestD1UpsertKeepAgainstRealSqlite:
    """文字列一致だけでなく、実際に SQLite (D1 と同じエンジン) に流して確認する。

    `jss_raw_files.first_fetched_at` が再取得のたびに上書きされていたバグ
    (overlaps_refactor) の回帰テスト。
    """

    def test_keep_column_survives_a_conflicting_upsert(self, monkeypatch):
        import sqlite3

        from jp_stock_pipeline import http
        from jp_stock_pipeline.cloud_store.d1 import D1Store
        from jp_stock_pipeline.config import CloudStoreSettings

        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE jss_raw_files (sha256 TEXT PRIMARY KEY, "
            "first_fetched_at INTEGER, last_fetched_at INTEGER)"
        )

        class _Resp:
            def json(self):
                return {"success": True, "result": [{"results": []}]}

        def fake_post(url, *, json_body, headers, idempotent=False, **kwargs):
            con.execute(json_body["sql"], json_body["params"])
            con.commit()
            return _Resp()

        monkeypatch.setattr(http, "post_json", fake_post)
        store = D1Store(
            CloudStoreSettings(cf_account_id="a", cf_api_token="t", d1_database_id="d"),
            writer="w",
        )
        columns = ["sha256", "first_fetched_at", "last_fetched_at"]
        store.upsert(
            "jss_raw_files", columns, [["s1", 100, 100]],
            conflict=["sha256"], keep=["first_fetched_at"],
        )
        # 再取得（同一原本の再送）: 初回取得時刻はそのまま、最終取得時刻だけ進む。
        store.upsert(
            "jss_raw_files", columns, [["s1", 999, 200]],
            conflict=["sha256"], keep=["first_fetched_at"],
        )
        row = con.execute(
            "SELECT first_fetched_at, last_fetched_at FROM jss_raw_files WHERE sha256='s1'"
        ).fetchone()
        assert row == (100, 200)
