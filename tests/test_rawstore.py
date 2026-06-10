"""原本保存・命名規則・SHA256 (§5.2) のテスト。"""

from datetime import date

from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import ConvertStatus, Source
from jp_stock_pipeline.rawstore import (
    converted_filename,
    raw_filename,
    save_raw,
    sha256_bytes,
)


class TestNaming:
    def test_raw_filename_rule(self):
        name = raw_filename(Source.EDINET, "documents_list", "ALL", date(2026, 6, 10), "json")
        assert name == "edinet_documents_list_ALL_20260610.json"

    def test_converted_filename_rule(self):
        assert (
            converted_filename("edinet_documents_list_ALL_20260610.json", "parquet")
            == "edinet_documents_list_ALL_20260610_converted.parquet"
        )

    def test_unsafe_chars_sanitized(self):
        name = raw_filename(Source.STOOQ, "daily/prices", "7203.jp", date(2026, 1, 5), "csv")
        assert "/" not in name
        assert name.endswith(".csv")


class TestSaveRaw:
    def test_saves_bytes_unmodified(self, tmp_path):
        content = b"\x00\x01raw-bytes\xff"
        art = save_raw(
            content,
            source=Source.EDINET,
            datatype="xbrl",
            scope="7203",
            data_date=date(2026, 6, 10),
            url="https://example.invalid/doc",
            ext="zip",
            license_tag=LicenseTag.COMMERCIAL_OK,
            base_dir=tmp_path,
        )
        assert art.local_path.read_bytes() == content
        assert art.sha256 == sha256_bytes(content)
        assert art.size_bytes == len(content)
        assert art.convert_status is ConvertStatus.NOT_APPLICABLE
        assert art.notion_page_id is None

    def test_idempotent_same_content(self, tmp_path):
        kwargs = dict(
            source=Source.STOOQ,
            datatype="daily",
            scope="7203",
            data_date=date(2026, 6, 10),
            url="https://example.invalid/csv",
            ext="csv",
            license_tag=LicenseTag.PERSONAL_ONLY,
            base_dir=tmp_path,
        )
        a1 = save_raw(b"date,close\n", **kwargs)
        a2 = save_raw(b"date,close\n", **kwargs)
        assert a1.sha256 == a2.sha256
        assert a1.local_path == a2.local_path
