"""xls_to_csv（§5.2 値不変・dtype=str）のテスト。

実フィクスチャ: JPX 上場銘柄一覧。personal-only (§2.1) の私的検証用
フィクスチャ（詳細は tests/fixtures/convert/README.md）。
未取得環境では fixture_path() により skip される。

JPX は 2026-09 に配布形式を .xls から .xlsx へ差し替えた（旧 URL は 404）。
どちらの形式のフィクスチャでも同じ検証が通るよう、**実在するほうを使う**。
拡張子でエンジンが変わる（.xls=xlrd / .xlsx=openpyxl）ので、名前を
`xls_to_csv` へそのまま渡すことが検証の一部になっている。
"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
import pytest
from conftest import FIXTURES_DIR, fixture_path

from jp_stock_pipeline.convert.json_to_parquet import convert_artifact
from jp_stock_pipeline.convert.xls_to_csv import xls_to_csv
from jp_stock_pipeline.licensing import source_license
from jp_stock_pipeline.models import ConvertStatus, Source
from jp_stock_pipeline.rawstore import converted_filename, save_raw

# 新形式を優先し、無ければ旧形式にフォールバックする。どちらも無ければ skip。
_CANDIDATES = ("convert/data_j.xlsx", "convert/data_j.xls")
XLS_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
)


def _fixture_name() -> str:
    """実在するフィクスチャのファイル名（拡張子つき）。無ければ skip。"""
    for rel in _CANDIDATES:
        if (FIXTURES_DIR / rel).exists():
            return rel.rsplit("/", 1)[-1]
    # どちらも無いことを skip として表現するため、新形式のパスで解決させる
    fixture_path(_CANDIDATES[0])
    raise AssertionError("unreachable")


def _fixture_ext() -> str:
    return _fixture_name().rsplit(".", 1)[-1]


def _load_bytes() -> bytes:
    return fixture_path(f"convert/{_fixture_name()}").read_bytes()


def _make_artifact(tmp_path, content: bytes):
    return save_raw(
        content,
        source=Source.JPX,
        datatype="listed_issues",
        scope="ALL",
        data_date=date(2026, 6, 10),
        url=XLS_URL,
        ext=_fixture_ext(),
        license_tag=source_license(Source.JPX),  # personal-only (§2.1)
        base_dir=tmp_path,
    )


class TestXlsToCsv:
    def test_real_data_j_readable_and_large(self):
        """実 data_j が読めて全上場銘柄規模（>3000行）であること。"""
        sheets = xls_to_csv(_load_bytes(), _fixture_name())
        assert len(sheets) >= 1
        _name, csv_bytes = next(iter(sheets.items()))
        df = pd.read_csv(io.BytesIO(csv_bytes), dtype=str, keep_default_na=False)
        assert len(df) > 3000

    def test_header_kept_and_dtype_str(self):
        """先頭行ヘッダのまま（加工なし）・dtype=str（数値推定なし §5.2）。"""
        sheets = xls_to_csv(_load_bytes(), _fixture_name())
        _name, csv_bytes = next(iter(sheets.items()))
        df = pd.read_csv(io.BytesIO(csv_bytes), dtype=str, keep_default_na=False)
        assert "コード" in df.columns
        assert all(isinstance(v, str) for v in df["コード"])
        # 実在銘柄コードが文字列のまま保持される（例: 7203 トヨタ自動車）
        assert "7203" in set(df["コード"])

    def test_values_identical_to_direct_read(self):
        """CSV 経由の値が pandas 直読みの値と完全一致（値不変 §5.2）。

        欠損セルは CSV 表現上空欄になるため、比較時のみ欠損→"" に正規化する
        （値の補完はしていない §3-1）。
        """
        raw = _load_bytes()
        sheets = xls_to_csv(raw, _fixture_name())
        name, csv_bytes = next(iter(sheets.items()))
        via_csv = pd.read_csv(io.BytesIO(csv_bytes), dtype=str, keep_default_na=False)
        # 比較側も同じ拡張子規則でエンジンを選ぶ（.xls=xlrd / .xlsx=openpyxl）。
        # ここを xlrd 固定にすると .xlsx のフィクスチャで XLRDError になる。
        engine = "xlrd" if _fixture_ext() == "xls" else "openpyxl"
        direct = pd.read_excel(io.BytesIO(raw), sheet_name=name, dtype=str, engine=engine)
        assert list(via_csv.columns) == [str(c) for c in direct.columns]
        assert len(via_csv) == len(direct)
        direct_norm = direct.where(direct.notna(), "")
        assert via_csv.values.tolist() == direct_norm.values.tolist()

    def test_csv_is_utf8(self):
        sheets = xls_to_csv(_load_bytes(), _fixture_name())
        for csv_bytes in sheets.values():
            csv_bytes.decode("utf-8")  # UTF-8 として正当（例外が出ないこと）

    def test_unsupported_extension_raises(self):
        with pytest.raises(ValueError):
            xls_to_csv(_load_bytes(), "data_j.txt")


class TestConvertArtifact:
    def test_xls_success(self, tmp_path):
        artifact = _make_artifact(tmp_path, _load_bytes())
        convert_artifact(artifact, _fixture_ext())
        assert artifact.convert_status is ConvertStatus.DONE
        # data_j は1シート → 変換版は1ファイル（命名規則 §5.2）
        assert [p.name for p in artifact.converted_paths] == [
            converted_filename(artifact.filename, "csv")
        ]
        df = pd.read_csv(artifact.converted_paths[0], dtype=str, keep_default_na=False)
        assert len(df) > 3000

    def test_broken_xls_records_failed_and_keeps_raw(self, tmp_path):
        """破損ファイル（実xlsの先頭128バイトのみ）は FAILED・原本保存は成立 (§5.2)。"""
        broken = _load_bytes()[:128]
        artifact = _make_artifact(tmp_path, broken)
        convert_artifact(artifact, _fixture_ext())
        assert artifact.convert_status is ConvertStatus.FAILED
        assert artifact.converted_paths == []
        assert artifact.local_path.exists()
