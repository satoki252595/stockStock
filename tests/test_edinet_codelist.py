"""EDINETコードリスト収集 (§2.1, P1) のテスト。

フィクスチャ tests/fixtures/edinet/Edinetcode.zip は 2026-06-10 に
公開URLから実取得した実レスポンス（捏造禁止 §3-6）。
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

from conftest import fixture_path

from jp_stock_pipeline.collectors import edinet_codelist as mod
from jp_stock_pipeline.config import load_settings
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import ConvertStatus, Source
from jp_stock_pipeline.rawstore import save_raw

ZIP_FIXTURE = "edinet/Edinetcode.zip"


def _zip_bytes() -> bytes:
    return fixture_path(ZIP_FIXTURE).read_bytes()


class TestNormalizeSecCode:
    def test_five_digits_trailing_zero(self):
        assert mod.normalize_sec_code("72030") == "7203"

    def test_new_style_alphanumeric(self):
        # 新方式コード（英字含み）も末尾0の5桁 → 4桁化（実コードリストに存在する形式）
        assert mod.normalize_sec_code("409A0") == "409A"

    def test_four_digits_kept(self):
        assert mod.normalize_sec_code("7203") == "7203"

    def test_missing_is_none(self):
        # 欠損は欠損のまま (§3-1)
        assert mod.normalize_sec_code(None) is None
        assert mod.normalize_sec_code("") is None
        assert mod.normalize_sec_code("  ") is None


class TestParseCodelist:
    def test_parses_listed_companies(self):
        records = mod.parse_codelist(_zip_bytes())
        # 全上場銘柄は約3,900社 (§1)
        assert len(records) > 3000
        assert all(r.listed for r in records)
        # 証券コードは4桁化済み（コードリストの実データは全件5桁・末尾0）
        assert all(len(r.code) == 4 for r in records)

    def test_toyota_7203(self):
        records = mod.parse_codelist(_zip_bytes())
        by_code = {r.code: r for r in records}
        toyota = by_code["7203"]
        assert toyota.name == "トヨタ自動車株式会社"
        assert toyota.edinet_code == "E02144"
        assert toyota.sector33 == "輸送用機器"

    def test_provenance(self):
        records = mod.parse_codelist(_zip_bytes(), raw_page_id="page-123")
        prov = records[0].provenance
        assert prov.source is Source.EDINET
        assert prov.license_tag is LicenseTag.COMMERCIAL_OK  # §2.1
        # データ基準日はメタ行（1行目）の「YYYY年MM月DD日現在」から取る
        assert isinstance(prov.data_date, date)
        assert prov.fetched_at.tzinfo is not None
        assert prov.raw_page_id == "page-123"

    def test_cp932_decodable(self):
        # zip 内 CSV が cp932 で読めること（実物の文字コード確認）
        text = mod._read_codelist_csv(_zip_bytes())
        assert "ＥＤＩＮＥＴコード" in text
        assert "証券コード" in text


class TestFetchCodelist:
    def test_saves_raw_with_license(self, tmp_path, monkeypatch):
        """fetch をモックし（内容は実フィクスチャのバイト列）保存経路を検証する。"""
        data = _zip_bytes()

        class _Resp:
            content = data

        monkeypatch.setattr(mod, "fetch", lambda url, **kw: _Resp())
        settings = load_settings(env={"RAW_DATA_DIR": str(tmp_path)}, dry_run=True)
        art = mod.fetch_codelist(settings)
        assert art.local_path.exists()
        assert art.local_path.read_bytes() == data  # 無加工保存 (§5.2)
        assert art.source is Source.EDINET
        assert art.datatype == "codelist"
        assert art.scope == "ALL"
        assert art.license_tag is LicenseTag.COMMERCIAL_OK
        assert art.url == mod.CODELIST_URL


class TestConvertCodelist:
    def _artifact(self, tmp_path):
        return save_raw(
            _zip_bytes(),
            source=Source.EDINET,
            datatype="codelist",
            scope="ALL",
            data_date=date(2026, 6, 10),
            url=mod.CODELIST_URL,
            ext="zip",
            license_tag=LicenseTag.COMMERCIAL_OK,
            base_dir=tmp_path,
        )

    def test_utf8_conversion_value_invariant(self, tmp_path):
        art = mod.convert_codelist(self._artifact(tmp_path))
        assert art.convert_status is ConvertStatus.DONE
        assert len(art.converted_paths) == 1
        out = art.converted_paths[0]
        assert out.name.endswith("_converted.csv")
        # 値不変 (§5.2): cp932→UTF-8 の文字コード正規化のみで内容は完全一致
        # （read_text は改行変換するため bytes で比較し、改行 \r\n も保持を確認）
        with zipfile.ZipFile(io.BytesIO(_zip_bytes())) as zf:
            original = zf.read("EdinetcodeDlInfo.csv").decode("cp932")
        assert out.read_bytes().decode("utf-8") == original

    def test_failure_records_status(self, tmp_path):
        art = self._artifact(tmp_path)
        art.local_path.write_bytes(b"not a zip")  # 破損原本でも例外にせず状態記録 (§8.1-3)
        art = mod.convert_codelist(art)
        assert art.convert_status is ConvertStatus.FAILED
        assert art.converted_paths == []
