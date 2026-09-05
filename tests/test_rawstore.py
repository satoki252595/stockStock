"""原本保存・命名規則・SHA256 (§5.2) のテスト。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest

from jp_stock_pipeline import rawstore
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

    @pytest.mark.parametrize("existing_alternate", [False, True])
    def test_idempotent_same_content(self, tmp_path, monkeypatch, existing_alternate):
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
        if existing_alternate:
            save_raw(b"older raw bytes", **kwargs)
        a1 = save_raw(b"date,close\n", **kwargs)
        before = a1.local_path.stat()

        def forbid_temporary_file(*args, **kwargs):
            pytest.fail("同内容の再実行で一時ファイルを作成してはいけない")

        monkeypatch.setattr(rawstore.tempfile, "NamedTemporaryFile", forbid_temporary_file)
        a2 = save_raw(b"date,close\n", **kwargs)
        assert a1.sha256 == a2.sha256
        assert a1.local_path == a2.local_path
        assert a2.local_path.stat().st_ino == before.st_ino
        assert a2.local_path.stat().st_mtime_ns == before.st_mtime_ns

    def test_same_name_different_content_preserves_both(self, tmp_path):
        """同日再取得で内容が変わった場合、旧原本を上書きしない (§5.1)。"""
        kwargs = dict(
            source=Source.TDNET,
            datatype="tdnet_list",
            scope="recent",
            data_date=date(2026, 6, 10),
            url="https://example.invalid/list",
            ext="json",
            license_tag=LicenseTag.FACTUAL_CITE,
            base_dir=tmp_path,
        )
        a1 = save_raw(b'{"items": [1]}', **kwargs)
        a2 = save_raw(b'{"items": [1, 2]}', **kwargs)
        assert a1.local_path != a2.local_path
        assert a1.local_path.read_bytes() == b'{"items": [1]}'  # 旧原本が残る
        assert a2.local_path.read_bytes() == b'{"items": [1, 2]}'
        assert a2.sha256[:8] in a2.local_path.name

    @pytest.mark.parametrize("same_content", [True, False])
    def test_concurrent_save_preserves_contents(self, tmp_path, monkeypatch, same_content):
        """同時公開を強制して、同内容の一意性と異内容の旧原本保全を確認する。"""
        kwargs = dict(
            source=Source.EDINET, datatype="csv", scope="7203",
            data_date=date(2026, 6, 10), url="https://example.invalid/doc",
            ext="zip", license_tag=LicenseTag.COMMERCIAL_OK, base_dir=tmp_path,
        )
        primary = tmp_path / "edinet_csv_7203_20260610.zip"
        barrier = Barrier(2)
        original_link = rawstore.os.link

        def simultaneous_link(src, dst):
            if dst == primary:
                barrier.wait(timeout=5)
            return original_link(src, dst)

        monkeypatch.setattr(rawstore.os, "link", simultaneous_link)
        contents = [b"first raw bytes", b"first raw bytes" if same_content else b"other raw bytes"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            artifacts = list(executor.map(lambda content: save_raw(content, **kwargs), contents))

        paths = {artifact.local_path for artifact in artifacts}
        assert len(paths) == (1 if same_content else 2)
        assert primary in paths
        assert set(tmp_path.iterdir()) == paths  # 途中ファイルを残さない
        for artifact, content in zip(artifacts, contents, strict=True):
            assert artifact.local_path.read_bytes() == content
            assert artifact.sha256 == sha256_bytes(content)
            if artifact.local_path != primary:
                assert artifact.sha256[:8] in artifact.local_path.name

    def test_short_hash_collision_preserves_existing_files(self, tmp_path):
        content = b"new raw bytes"
        primary = tmp_path / "edinet_csv_7203_20260610.zip"
        alternate = tmp_path / f"edinet_csv_7203_20260610_{sha256_bytes(content)[:8]}.zip"
        primary.write_bytes(b"existing primary bytes")
        alternate.write_bytes(b"existing alternate bytes")

        with pytest.raises(ValueError, match="上書き拒否"):
            save_raw(
                content, source=Source.EDINET, datatype="csv", scope="7203",
                data_date=date(2026, 6, 10), url="https://example.invalid/doc",
                ext="zip", license_tag=LicenseTag.COMMERCIAL_OK, base_dir=tmp_path,
            )

        assert primary.read_bytes() == b"existing primary bytes"
        assert alternate.read_bytes() == b"existing alternate bytes"
        assert set(tmp_path.iterdir()) == {primary, alternate}  # 失敗時も一時ファイルを除去
