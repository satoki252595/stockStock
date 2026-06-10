"""XBRL→tidy 変換 (§5.2, CONTRACTS.md tidy 列定義) のテスト。

- tidy 列順・列名は CONTRACTS.md と完全一致すること（B が生成し F が消費する契約）
- XBRL/CSV 書類の実フィクスチャは APIキー必須のため scripts/capture_edinet.py で
  取得する。未取得は skip (§3-6)
- 公開実ファイル（EDINETコードリスト zip）で zip 取り扱い・書き出し経路を検証する
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
from conftest import fixture_path

from jp_stock_pipeline.collectors.edinet_codelist import normalize_sec_code
from jp_stock_pipeline.convert import xbrl_to_csv as mod
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import ConvertStatus, Source
from jp_stock_pipeline.rawstore import save_raw

# CONTRACTS.md「XBRL tidy 形式」列定義（この順）。変更は契約違反
CONTRACT_COLUMNS = [
    "code",
    "doc_id",
    "element",
    "context_ref",
    "period_start",
    "period_end",
    "instant_date",
    "consolidated",
    "unit",
    "value",
]


class TestTidySchema:
    def test_columns_match_contract_exactly(self):
        assert mod.TIDY_COLUMNS == CONTRACT_COLUMNS

    def test_empty_frame_has_contract_columns(self):
        # 実在する公開 zip（コードリスト）には .xbrl が無い → 空 tidy（スキーマは維持）
        data = fixture_path("edinet/Edinetcode.zip").read_bytes()
        df = mod.xbrl_zip_to_tidy(data, code="", doc_id="")
        assert list(df.columns) == CONTRACT_COLUMNS
        assert len(df) == 0


class TestConsolidatedRule:
    """連結/単体は contextRef 文字列規則のみで判定。不明は空文字 (§3-1)。"""

    def test_non_consolidated_member(self):
        assert mod.consolidated_from_context("CurrentYearDuration_NonConsolidatedMember") == "単体"

    def test_default_contexts_are_consolidated(self):
        assert mod.consolidated_from_context("CurrentYearDuration") == "連結"
        assert mod.consolidated_from_context("Prior1YearInstant") == "連結"
        assert mod.consolidated_from_context("CurrentYTDDuration") == "連結"
        assert mod.consolidated_from_context("InterimDuration") == "連結"

    def test_unknown_contexts_are_empty(self):
        # 提出日時点・独自メンバー付き等は判別不能 → 空文字（推定しない）
        assert mod.consolidated_from_context("FilingDateInstant") == ""
        assert mod.consolidated_from_context("CurrentYearDuration_ReportableSegmentsMember") == ""
        assert mod.consolidated_from_context("") == ""


class TestWriteTidy:
    def _artifact(self, tmp_path):
        # 実在する公開ファイルのバイト列で原本を作る（内容捏造をしない §3-6）
        data = fixture_path("edinet/Edinetcode.zip").read_bytes()
        return save_raw(
            data,
            source=Source.EDINET,
            datatype="xbrl",
            scope="ALL",
            data_date=date(2026, 6, 10),
            url="https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip",
            ext="zip",
            license_tag=LicenseTag.COMMERCIAL_OK,
            base_dir=tmp_path,
        )

    def test_writes_csv_and_parquet_with_schema(self, tmp_path):
        data = fixture_path("edinet/Edinetcode.zip").read_bytes()
        df = mod.xbrl_zip_to_tidy(data, code="", doc_id="")  # 空 tidy（実 zip 由来）
        art = mod.write_tidy(df, self._artifact(tmp_path))
        assert art.convert_status is ConvertStatus.DONE
        assert len(art.converted_paths) == 2
        csv_path, parquet_path = art.converted_paths
        assert csv_path.name.endswith("_converted.csv")
        assert parquet_path.name.endswith("_converted.parquet")
        back_csv = pd.read_csv(csv_path, dtype=str)
        assert list(back_csv.columns) == CONTRACT_COLUMNS
        back_parquet = pd.read_parquet(parquet_path)
        assert list(back_parquet.columns) == CONTRACT_COLUMNS

    def test_failure_records_status(self, tmp_path, monkeypatch):
        art = self._artifact(tmp_path)
        monkeypatch.setattr(
            pd.DataFrame, "to_parquet", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk"))
        )
        df = pd.DataFrame(columns=mod.TIDY_COLUMNS)
        art = mod.write_tidy(df, art)
        assert art.convert_status is ConvertStatus.FAILED  # §8.1-3: 状態記録して続行


def _doc_meta(name: str) -> dict:
    """capture_edinet.py が保存する実書類メタ（docID・secCode）。未取得は skip。"""
    return json.loads(fixture_path(name).read_text("utf-8"))


class TestRealXbrlFixture:
    """実 XBRL zip（要APIキー取得）でのテスト。未取得は skip。"""

    def test_xbrl_zip_to_tidy(self):
        meta = _doc_meta("edinet/xbrl_sample.meta.json")
        data = fixture_path("edinet/xbrl_sample.zip").read_bytes()
        code = normalize_sec_code(meta.get("secCode")) or ""
        df = mod.xbrl_zip_to_tidy(data, code=code, doc_id=meta["docID"])
        assert list(df.columns) == CONTRACT_COLUMNS
        assert len(df) > 0  # 実書類にはファクトが存在する
        assert (df["code"] == code).all()
        assert (df["doc_id"] == meta["docID"]).all()
        # 全列文字列・value は原文のまま（数値化しない §5.2）
        assert all(str(dtype) == "string" for dtype in df.dtypes)
        # context の period 解決: duration か instant のどちらかを持つ行が存在する
        assert ((df["period_end"] != "") | (df["instant_date"] != "")).any()
        # 連結/単体は "連結"/"単体"/"" のみ
        assert set(df["consolidated"].unique()) <= {"連結", "単体", ""}

    def test_roundtrip_value_invariant(self, tmp_path):
        meta = _doc_meta("edinet/xbrl_sample.meta.json")
        data = fixture_path("edinet/xbrl_sample.zip").read_bytes()
        df = mod.xbrl_zip_to_tidy(data, code="", doc_id=meta["docID"])
        art = save_raw(
            data,
            source=Source.EDINET,
            datatype="xbrl",
            scope=meta["docID"],
            data_date=None,
            url="https://api.edinet-fsa.go.jp/api/v2/documents/" + meta["docID"],
            ext="zip",
            license_tag=LicenseTag.COMMERCIAL_OK,
            base_dir=tmp_path,
        )
        art = mod.write_tidy(df, art)
        assert art.convert_status is ConvertStatus.DONE
        back = pd.read_parquet(art.converted_paths[1]).fillna("")
        # 値不変 (§5.2): 書き出し前後で value 列が完全一致
        assert list(back["value"]) == list(df["value"])


class TestRealEdinetCsvFixture:
    """実 EDINET type=5 CSV zip（要APIキー取得）でのテスト。未取得は skip。"""

    def test_edinet_csv_zip_to_tidy(self):
        meta = _doc_meta("edinet/csv_sample.meta.json")
        data = fixture_path("edinet/csv_sample.zip").read_bytes()
        code = normalize_sec_code(meta.get("secCode")) or ""
        df = mod.edinet_csv_zip_to_tidy(data, code=code, doc_id=meta["docID"])
        assert list(df.columns) == CONTRACT_COLUMNS
        assert len(df) > 0
        assert (df["code"] == code).all()
        assert set(df["consolidated"].unique()) <= {"連結", "単体", ""}
        # 要素名・コンテキストIDは原文のまま入る
        assert (df["element"] != "").all()
        assert (df["context_ref"] != "").all()
