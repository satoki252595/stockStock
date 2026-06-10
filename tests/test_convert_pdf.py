"""pdf_to_text（§5.2: OCRなし・真実性優先）のテスト。

実フィクスチャ: TDnet の実開示PDF（scripts/capture_convert_fixtures.py で実取得。
factual-cite §2.1 の内部保管。出所は tdnet_disclosure_sample.source.txt）。

「画像のみPDF → None」の経路は、実物の画像のみPDFが無い以上 PDF を捏造せず (§3-6)、
抽出文字数閾値ロジック has_meaningful_text を文字列レベルで検証する。
"""

from __future__ import annotations

from datetime import date

import pypdfium2 as pdfium
import pytest
from conftest import fixture_path

from jp_stock_pipeline.convert.json_to_parquet import convert_artifact
from jp_stock_pipeline.convert.pdf_to_text import (
    MIN_TEXT_CHARS,
    has_meaningful_text,
    pdf_to_text,
)
from jp_stock_pipeline.licensing import source_license
from jp_stock_pipeline.models import ConvertStatus, Source
from jp_stock_pipeline.rawstore import converted_filename, save_raw

PDF_REL = "convert/tdnet_disclosure_sample.pdf"
# 実際の出所は tests/fixtures/convert/tdnet_disclosure_sample.source.txt に記録
PDF_URL_NOTE = "https://www.release.tdnet.info/ (tdnet_disclosure_sample.source.txt 参照)"


def _make_artifact(tmp_path, content: bytes):
    return save_raw(
        content,
        source=Source.TDNET,
        datatype="disclosure_pdf",
        scope="ALL",
        data_date=date(2026, 6, 10),
        url=PDF_URL_NOTE,
        ext="pdf",
        license_tag=source_license(Source.TDNET),
        base_dir=tmp_path,
    )


class TestPdfToText:
    def test_real_disclosure_extracts_meaningful_text(self):
        """実開示PDFから非空のテキストが抽出されること。"""
        text = pdf_to_text(fixture_path(PDF_REL).read_bytes())
        assert text is not None
        assert len("".join(text.split())) >= MIN_TEXT_CHARS

    def test_extraction_is_deterministic(self):
        """同一原本からの抽出は同一結果（値不変 §5.2 の前提）。"""
        raw = fixture_path(PDF_REL).read_bytes()
        assert pdf_to_text(raw) == pdf_to_text(raw)

    def test_broken_pdf_raises(self):
        """破損PDF（実PDFの先頭64バイトのみ）は例外 → 呼び出し側で FAILED 扱い。"""
        broken = fixture_path(PDF_REL).read_bytes()[:64]
        with pytest.raises(pdfium.PdfiumError):
            pdf_to_text(broken)


class TestMeaningfulTextThreshold:
    """画像のみPDF相当の None 経路の閾値ロジック（文字列レベル検証 §3-6）。"""

    def test_empty_is_not_meaningful(self):
        assert not has_meaningful_text("")

    def test_whitespace_only_is_not_meaningful(self):
        assert not has_meaningful_text(" \n\t\r　 ")

    def test_below_threshold(self):
        assert not has_meaningful_text("x" * (MIN_TEXT_CHARS - 1))

    def test_at_threshold(self):
        assert has_meaningful_text("x" * MIN_TEXT_CHARS)

    def test_whitespace_is_not_counted(self):
        # 空白を除いた文字数で判定する（"x " の繰り返しは x の個数のみカウント）
        assert not has_meaningful_text("x " * (MIN_TEXT_CHARS - 1))
        assert has_meaningful_text("x " * MIN_TEXT_CHARS)


class TestConvertArtifact:
    def test_pdf_success_writes_txt_unmodified(self, tmp_path):
        content = fixture_path(PDF_REL).read_bytes()
        artifact = _make_artifact(tmp_path, content)
        convert_artifact(artifact, "pdf")
        assert artifact.convert_status is ConvertStatus.DONE
        assert [p.name for p in artifact.converted_paths] == [
            converted_filename(artifact.filename, "txt")
        ]
        # 書き出された .txt は抽出テキストと完全一致（値不変 §5.2）。
        # read_text の改行変換を避けるためバイト列で比較する
        written = artifact.converted_paths[0].read_bytes().decode("utf-8")
        assert written == pdf_to_text(content)

    def test_broken_pdf_records_failed_and_keeps_raw(self, tmp_path):
        """破損PDFは FAILED を記録し原本保存は成立 (§5.2)。"""
        broken = fixture_path(PDF_REL).read_bytes()[:64]
        artifact = _make_artifact(tmp_path, broken)
        convert_artifact(artifact, "pdf")
        assert artifact.convert_status is ConvertStatus.FAILED
        assert artifact.converted_paths == []
        assert artifact.local_path.exists()
