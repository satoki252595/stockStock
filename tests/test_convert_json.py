"""json_to_parquet（§5.2 値不変）のテスト。

フィクスチャは実APIレスポンス（やのしんTDnet recent.json、curl 実取得）のみ使用する (§3-6)。
未取得環境では fixture_path() により skip される。
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest
from conftest import fixture_path

from jp_stock_pipeline.convert.json_to_parquet import (
    convert_artifact,
    json_records,
    to_csv_and_parquet,
)
from jp_stock_pipeline.licensing import source_license
from jp_stock_pipeline.models import ConvertStatus, Source
from jp_stock_pipeline.rawstore import converted_filename, save_raw

FIXTURE_REL = "convert/yanoshin_tdnet_recent.json"
FIXTURE_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/recent.json?limit=5"


def _load_bytes() -> bytes:
    return fixture_path(FIXTURE_REL).read_bytes()


def _make_artifact(tmp_path, content: bytes):
    """実フィクスチャを rawstore.save_raw 経由で原本化する (§8.1 step 2)。"""
    return save_raw(
        content,
        source=Source.TDNET,
        datatype="recent_list",
        scope="ALL",
        data_date=date(2026, 6, 10),
        url=FIXTURE_URL,
        ext="json",
        license_tag=source_license(Source.TDNET),
        base_dir=tmp_path,
    )


def _cells(df: pd.DataFrame) -> list[list]:
    """欠損を None に正規化したセル行列（値不変比較用。値そのものは変更しない）。"""
    return [
        [None if pd.isna(v) else v for v in row]
        for row in df.itertuples(index=False, name=None)
    ]


class TestJsonRecords:
    def test_bytes_input_flattens_items(self):
        """bytes 入力: items 配列を推定し、ネストを a.b 形式にフラット化する。"""
        raw = _load_bytes()
        df = json_records(raw)
        parsed = json.loads(raw)
        assert len(df) == len(parsed["items"])
        assert "Tdnet.company_code" in df.columns
        # 値は原文のまま（実レスポンスと突合。値不変 §5.2）
        assert df["Tdnet.company_code"].iloc[0] == parsed["items"][0]["Tdnet"]["company_code"]
        assert df["Tdnet.title"].iloc[0] == parsed["items"][0]["Tdnet"]["title"]

    def test_list_input_used_directly(self):
        parsed = json.loads(_load_bytes())
        df = json_records(parsed["items"])
        assert len(df) == len(parsed["items"])
        assert "Tdnet.pubdate" in df.columns

    def test_dict_record_array_key_inference(self):
        """results / items / data のレコード配列キー推定（実レコードを載せ替えて検証）。"""
        items = json.loads(_load_bytes())["items"]
        for key in ("results", "items", "data"):
            df = json_records({key: items})
            assert len(df) == len(items), key

    def test_dict_without_record_array_is_single_row(self):
        first = json.loads(_load_bytes())["items"][0]["Tdnet"]
        df = json_records(first)
        assert len(df) == 1
        assert df["company_code"].iloc[0] == first["company_code"]

    def test_no_numeric_inference_all_cells_str_or_missing(self):
        """dtype は文字列保持・数値推定なし（値不変優先 §5.2）。"""
        df = json_records(_load_bytes())
        for col in df.columns:
            for v in df[col]:
                assert v is None or pd.isna(v) or isinstance(v, str), (col, v)


class TestCsvParquetRoundTrip:
    def test_parquet_roundtrip_value_identity(self, tmp_path):
        """Parquet 往復で値が一切変わらないこと（値不変 §5.2）。"""
        raw = _load_bytes()
        artifact = _make_artifact(tmp_path, raw)
        df = json_records(raw)
        _csv_path, parquet_path = to_csv_and_parquet(df, artifact)
        back = pd.read_parquet(parquet_path)
        assert list(back.columns) == [str(c) for c in df.columns]
        assert _cells(back) == _cells(df)

    def test_csv_roundtrip_value_identity(self, tmp_path):
        """CSV 往復で値が一切変わらないこと（欠損は CSV 表現上空欄になるのみ）。"""
        raw = _load_bytes()
        artifact = _make_artifact(tmp_path, raw)
        df = json_records(raw)
        csv_path, _parquet_path = to_csv_and_parquet(df, artifact)
        back = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        assert list(back.columns) == [str(c) for c in df.columns]
        expected = [["" if v is None else v for v in row] for row in _cells(df)]
        assert _cells(back) == expected

    def test_converted_filenames_follow_rule(self, tmp_path):
        """変換版の命名規則 (§5.2): {原本stem}_converted.{csv|parquet}、原本の隣に生成。"""
        raw = _load_bytes()
        artifact = _make_artifact(tmp_path, raw)
        csv_path, parquet_path = to_csv_and_parquet(json_records(raw), artifact)
        assert csv_path.name == converted_filename(artifact.filename, "csv")
        assert parquet_path.name == converted_filename(artifact.filename, "parquet")
        assert csv_path.parent == artifact.local_path.parent
        assert parquet_path.parent == artifact.local_path.parent


class TestConvertArtifact:
    def test_json_success_sets_paths_and_status(self, tmp_path):
        artifact = _make_artifact(tmp_path, _load_bytes())
        result = convert_artifact(artifact, "json")
        assert result is artifact
        assert artifact.convert_status is ConvertStatus.DONE
        assert [p.name for p in artifact.converted_paths] == [
            converted_filename(artifact.filename, "csv"),
            converted_filename(artifact.filename, "parquet"),
        ]
        for path in artifact.converted_paths:
            assert path.exists()

    def test_failure_records_failed_and_keeps_raw(self, tmp_path):
        """変換失敗は例外を握りつぶして FAILED を記録し、原本保存は成立 (§5.2)。

        入力は実レスポンスを途中で切った不正JSON（失敗経路の論理検証であり、
        実データを装うフィクスチャの捏造ではない）。
        """
        broken = _load_bytes()[:10]
        artifact = _make_artifact(tmp_path, broken)
        convert_artifact(artifact, "json")
        assert artifact.convert_status is ConvertStatus.FAILED
        assert artifact.converted_paths == []
        assert artifact.local_path.exists()
        assert artifact.local_path.read_bytes() == broken

    def test_unknown_kind_raises(self, tmp_path):
        """未知 kind はプログラミングエラー（変換失敗とは区別して送出）。"""
        artifact = _make_artifact(tmp_path, _load_bytes())
        with pytest.raises(ValueError):
            convert_artifact(artifact, "docx")
