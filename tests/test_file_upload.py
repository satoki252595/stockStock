"""notion/file_upload.py のテスト (§5.2, §8.1-2〜4, CONTRACTS.md 契約)。

dry-run の NotionClient を使い実 Notion へは接続しない。
raw_api の書き込みは .ops に記録され合成ID ("dry-run-*") が返る。
"""

from __future__ import annotations

from datetime import date

import pytest

from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import ConvertStatus, Source
from jp_stock_pipeline.notion import file_upload, schema as S
from jp_stock_pipeline.notion.client import NotionRequestError
from jp_stock_pipeline.rawstore import converted_filename, save_raw

ENV_SETTINGS = {
    "NOTION_DB_IDS_FILE": "/nonexistent/db_ids.json",
    "NOTION_DB_RAW_FILES": "db-raw",
}


def make_settings():
    from jp_stock_pipeline.config import load_settings

    return load_settings(dry_run=True, env=ENV_SETTINGS)


def make_artifact(tmp_path, content: bytes = b"raw-bytes", *, with_converted: bool = False):
    """ローカル保存済みの RawArtifact (rawstore.save_raw で実際にファイルを作る)。"""
    art = save_raw(
        content,
        source=Source.EDINET,
        datatype="xbrl",
        scope="7203",
        data_date=date(2026, 6, 10),
        url="https://api.edinet-fsa.go.jp/api/v2/documents/S100XXXX",
        ext="zip",
        license_tag=LicenseTag.COMMERCIAL_OK,
        base_dir=tmp_path,
    )
    if with_converted:
        conv = tmp_path / converted_filename(art.filename, "csv")
        conv.write_bytes(b"code,element,value\n")
        art.converted_paths = [conv]
        art.convert_status = ConvertStatus.DONE
    return art


class TestPlanUpload:
    """20MB 境界で single_part / multi_part を分岐する (§5.2)。"""

    def test_exactly_20mb_is_single_part(self):
        assert file_upload.plan_upload(file_upload.SINGLE_PART_LIMIT) == ("single_part", 1)

    def test_one_byte_over_is_multi_part(self):
        mode, parts = file_upload.plan_upload(file_upload.SINGLE_PART_LIMIT + 1)
        assert mode == "multi_part"
        assert parts == 3  # ceil((20MB+1) / 10MB)

    def test_small_file_single_part(self):
        assert file_upload.plan_upload(1) == ("single_part", 1)

    def test_part_count_is_ceiling(self):
        size = file_upload.MULTIPART_CHUNK * 5 + 1
        assert file_upload.plan_upload(size) == ("multi_part", 6)


class TestContentType:
    """作成時宣言と send パートの content_type 一致 (Notion 400 回避)。"""

    def test_known_extensions(self):
        from pathlib import Path

        assert file_upload._content_type(Path("x.zip")) == "application/zip"
        assert file_upload._content_type(Path("x.csv")) == "text/csv"
        assert file_upload._content_type(Path("x.pdf")) == "application/pdf"

    def test_unknown_extension_falls_back_to_octet_stream(self):
        from pathlib import Path

        # .xbrl / .parquet は mimetypes 未知 → octet-stream で一致を保証
        assert file_upload._content_type(Path("x.xbrl")) == "application/octet-stream"
        assert file_upload._content_type(Path("x.parquet")) == "application/octet-stream"


class TestSha256DuplicateSkip:
    def test_existing_row_returns_its_page_id_without_upload(self, dry_client, tmp_path, monkeypatch):
        """SHA256 一致の既存行があれば再アップロードせず page_id を返す (§8.1-2)。"""
        settings = make_settings()
        artifact = make_artifact(tmp_path)
        seen_filters = []

        def fake_query(db_id, *, filter=None, **kwargs):
            seen_filters.append((db_id, filter))
            return [{"id": "page-existing"}]

        monkeypatch.setattr(dry_client, "query_database", fake_query)
        page_id = file_upload.upload_raw_artifact(dry_client, settings, artifact)
        assert page_id == "page-existing"
        assert artifact.notion_page_id == "page-existing"
        # 重複時はアップロードも行作成も発生しない
        assert dry_client.ops == []
        # SHA256 で ⑤ を検索している
        db_id, flt = seen_filters[0]
        assert db_id == "db-raw"
        assert flt == {
            "property": S.RAW_PROP_SHA256,
            "rich_text": {"equals": artifact.sha256},
        }


class TestNewUploadSinglePart:
    def test_raw_api_call_sequence_and_row_creation(self, dry_client, tmp_path):
        settings = make_settings()
        artifact = make_artifact(tmp_path)
        page_id = file_upload.upload_raw_artifact(dry_client, settings, artifact)

        assert page_id.startswith("dry-run-")
        assert artifact.notion_page_id == page_id
        ops = [o.op for o in dry_client.ops]
        # (1) file_upload 作成 → (2) send → (3) ⑤ 行作成
        assert ops == ["POST file_uploads", "POST file_uploads/dry-run-1/send", "create_page"]
        create_fu = dry_client.ops[0].payload
        # content_type を作成時に宣言し send 時のパートと一致させる (.zip→application/zip)
        assert create_fu == {
            "mode": "single_part",
            "filename": artifact.filename,
            "content_type": "application/zip",
        }

    def test_row_properties(self, dry_client, tmp_path):
        settings = make_settings()
        artifact = make_artifact(tmp_path)
        file_upload.upload_raw_artifact(dry_client, settings, artifact)
        (create_page,) = [o for o in dry_client.ops if o.op == "create_page"]
        props = create_page.payload["properties"]
        assert create_page.payload["parent"] == {"database_id": "db-raw"}
        assert props[S.RAW_PROP_FILENAME]["title"][0]["text"]["content"] == artifact.filename
        assert props[S.RAW_PROP_SHA256]["rich_text"][0]["text"]["content"] == artifact.sha256
        assert props[S.RAW_PROP_SIZE] == {"number": artifact.size_bytes}
        assert props[S.RAW_PROP_CONVERT_STATUS]["select"]["name"] == "対象外"
        assert props[S.RAW_PROP_SCOPE]["rich_text"][0]["text"]["content"] == "7203"
        # ファイル添付は file_upload id
        files = props[S.RAW_PROP_FILES]["files"]
        assert files == [
            {
                "type": "file_upload",
                "file_upload": {"id": "dry-run-1"},
                "name": artifact.filename,
            }
        ]
        # 共通プロパティ (⑤向けサブセット §6.3)
        assert props[S.PROP_SOURCE]["select"]["name"] == "EDINET"
        assert props[S.PROP_LICENSE_TAG]["select"]["name"] == "commercial-ok"
        assert props[S.PROP_DATA_DATE]["date"]["start"] == "2026-06-10"
        assert S.PROP_RAW_RELATION not in props
        assert S.PROP_QUALITY not in props

    def test_converted_versions_uploaded_together(self, dry_client, tmp_path):
        """原本+変換版を同一行に併置する (§5.2)。"""
        settings = make_settings()
        artifact = make_artifact(tmp_path, with_converted=True)
        file_upload.upload_raw_artifact(dry_client, settings, artifact)
        fu_creates = [o for o in dry_client.ops if o.op == "POST file_uploads"]
        assert len(fu_creates) == 2  # 原本 + 変換版
        (create_page,) = [o for o in dry_client.ops if o.op == "create_page"]
        files = create_page.payload["properties"][S.RAW_PROP_FILES]["files"]
        assert len(files) == 2
        assert files[1]["name"].endswith("_converted.csv")
        assert (
            create_page.payload["properties"][S.RAW_PROP_CONVERT_STATUS]["select"]["name"]
            == "完了"
        )


class TestMultipart:
    def test_multipart_send_per_part_then_complete(self, dry_client, tmp_path, monkeypatch):
        """20MB超相当: part_number 毎に send → complete (§5.2)。

        実際に20MB超のファイルは作らず、境界定数を縮小して分岐ロジックを検証する
        （分岐は plan_upload のサイズ比較のみで、データは実ファイルのまま）。
        """
        monkeypatch.setattr(file_upload, "SINGLE_PART_LIMIT", 10)
        monkeypatch.setattr(file_upload, "MULTIPART_CHUNK", 4)
        settings = make_settings()
        artifact = make_artifact(tmp_path, content=b"0123456789A")  # 11 bytes → 3パート
        file_upload.upload_raw_artifact(dry_client, settings, artifact)

        ops = [o.op for o in dry_client.ops]
        assert ops == [
            "POST file_uploads",
            "POST file_uploads/dry-run-1/send",
            "POST file_uploads/dry-run-1/send",
            "POST file_uploads/dry-run-1/send",
            "POST file_uploads/dry-run-1/complete",
            "create_page",
        ]
        create_fu = dry_client.ops[0].payload
        assert create_fu["mode"] == "multi_part"
        assert create_fu["number_of_parts"] == 3
        # 各 send に part_number (multipart/form-data の data= 経由)
        part_numbers = [o.payload["part_number"] for o in dry_client.ops[1:4]]
        assert part_numbers == ["1", "2", "3"]


class TestRawUploadError:
    def test_api_failure_raises_raw_upload_error(self, dry_client, tmp_path, monkeypatch):
        """失敗時は RawUploadError (呼び出し側は構造化書き込みを中止 §8.1-4)。"""
        settings = make_settings()
        artifact = make_artifact(tmp_path)

        def boom(*args, **kwargs):
            raise NotionRequestError("Notion API リトライ枯渇")

        monkeypatch.setattr(dry_client, "raw_api", boom)
        with pytest.raises(file_upload.RawUploadError):
            file_upload.upload_raw_artifact(dry_client, settings, artifact)
        assert artifact.notion_page_id is None

    def test_dedup_query_failure_raises_raw_upload_error(self, dry_client, tmp_path, monkeypatch):
        """SHA256 重複クエリの失敗も契約例外 RawUploadError に揃える (§8.1-4)。

        find_raw_page_by_sha256 → query_database が NotionRequestError を投げても
        生例外を漏らさず、呼び出し側 (ジョブ) は取得単位を degrade できること。
        """
        settings = make_settings()
        artifact = make_artifact(tmp_path)

        def boom(*args, **kwargs):
            raise NotionRequestError("Notion クエリ リトライ枯渇")

        monkeypatch.setattr(dry_client, "query_database", boom)
        with pytest.raises(file_upload.RawUploadError):
            file_upload.upload_raw_artifact(dry_client, settings, artifact)
        assert artifact.notion_page_id is None
        assert dry_client.ops == []  # 重複判定前に倒れるので書き込みは一切ない

    def test_missing_upload_id_raises(self, dry_client, tmp_path, monkeypatch):
        settings = make_settings()
        artifact = make_artifact(tmp_path)
        monkeypatch.setattr(dry_client, "raw_api", lambda *a, **kw: {})
        with pytest.raises(file_upload.RawUploadError):
            file_upload.upload_raw_artifact(dry_client, settings, artifact)

    def test_missing_local_file_raises(self, dry_client, tmp_path):
        settings = make_settings()
        artifact = make_artifact(tmp_path)
        artifact.local_path.unlink()
        with pytest.raises(file_upload.RawUploadError):
            file_upload.upload_raw_artifact(dry_client, settings, artifact)
