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
    """send パートの content_type を create 応答（Notion 推定値）に一致させる。

    create には content_type を渡さず Notion に推定させ、その応答値を send の
    3要素タプルに使う。OS 依存の mimetypes を正本にすると Linux runner で
    .csv→None になり 400 になった事故の回帰防止。
    """

    def _fake_client(self, create_resp: dict):
        """raw_api を差し替えて (op, json_body, files) を記録するダブル。"""
        calls = []

        class _C:
            def raw_api(self, method, path, *, json_body=None, data=None, files=None,
                        record_in_dry_run=True):
                calls.append({"path": path, "json": json_body, "files": files})
                if path == "file_uploads":
                    return create_resp
                return {"id": create_resp.get("id", "u1")}

        return _C(), calls

    def test_send_uses_create_response_content_type(self, tmp_path):
        client, calls = self._fake_client({"id": "u1", "content_type": "text/csv; charset=utf-8"})
        p = tmp_path / "yfinance_daily_prices_batch_ALL_20260626.csv"
        p.write_bytes(b"code,close\n7203,2500\n")
        upload_id = file_upload._upload_single(client, p)
        assert upload_id == "u1"
        # create には content_type を渡さない
        create = next(c for c in calls if c["path"] == "file_uploads")
        assert "content_type" not in create["json"]
        # send は create 応答の content_type を 3要素タプルで送る
        send = next(c for c in calls if c["path"].endswith("/send"))
        name, content, ctype = send["files"]["file"]
        assert ctype == "text/csv; charset=utf-8"
        assert name == p.name

    def test_send_falls_back_to_2tuple_when_response_lacks_content_type(self, tmp_path):
        # 応答に content_type が無く mimetypes も未知（.xbrl）→ 2要素タプルに退避
        client, calls = self._fake_client({"id": "u1"})
        p = tmp_path / "doc.xbrl"
        p.write_bytes(b"<xbrl/>")
        file_upload._upload_single(client, p)
        send = next(c for c in calls if c["path"].endswith("/send"))
        assert len(send["files"]["file"]) == 2  # (name, content) のみ


class TestUnsupportedExtensionZipWrap:
    """Notion 非対応拡張子(.parquet 等)は .zip ラップして UL する (§5.2)。

    根本原因の回帰防止: yfinance/stooq は原本(.csv)に Parquet 変換版を併置するが、
    Notion File Upload は .parquet を「extension not supported」で 400 にする。
    """

    def test_parquet_is_zip_wrapped_and_name_changes(self, tmp_path):
        calls = []

        class _C:
            def raw_api(self, method, path, *, json_body=None, data=None, files=None,
                        record_in_dry_run=True):
                calls.append({"path": path, "json": json_body, "files": files})
                if path == "file_uploads":
                    return {"id": "u1", "content_type": "application/zip"}
                return {"id": "u1"}

        client = _C()
        p = tmp_path / "yfinance_daily_prices_batch_ALL_20260626_converted.parquet"
        p.write_bytes(b"PARQ-binary-bytes")
        upload_id, name = file_upload.upload_file(client, p)
        assert upload_id == "u1"
        # 添付名は元名 + .zip（Notion は .zip 対応）
        assert name == "yfinance_daily_prices_batch_ALL_20260626_converted.parquet.zip"
        # create に送った filename も .zip
        create = next(c for c in calls if c["path"] == "file_uploads")
        assert create["json"]["filename"].endswith(".parquet.zip")

    def test_supported_extension_uploaded_as_is(self, tmp_path):
        calls = []

        class _C:
            def raw_api(self, method, path, *, json_body=None, data=None, files=None,
                        record_in_dry_run=True):
                calls.append({"path": path, "json": json_body})
                if path == "file_uploads":
                    return {"id": "u1", "content_type": "text/csv; charset=utf-8"}
                return {"id": "u1"}

        client = _C()
        p = tmp_path / "data.csv"
        p.write_bytes(b"a,b\n1,2\n")
        upload_id, name = file_upload.upload_file(client, p)
        assert name == "data.csv"  # 対応拡張子はそのまま
        create = next(c for c in calls if c["path"] == "file_uploads")
        assert create["json"]["filename"] == "data.csv"

    def test_zip_wrapped_content_is_recoverable(self, tmp_path):
        import zipfile

        p = tmp_path / "x.parquet"
        p.write_bytes(b"PARQ-original-content")
        with file_upload._zip_wrapped(p) as zpath:
            assert zpath.name == "x.parquet.zip"
            with zipfile.ZipFile(zpath) as zf:
                # zip 内エントリ名は元ファイル名・中身は元バイト列（解凍で原本復元可能）
                assert zf.namelist() == ["x.parquet"]
                assert zf.read("x.parquet") == b"PARQ-original-content"
        assert not zpath.exists()  # コンテキスト終了で一時ファイルは消える


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


class TestShaMap:
    """L-21: 対象日の {SHA256: page_id} を 1 回作り原本ごとの検索を省く。"""

    DAY = date(2026, 6, 10)  # make_artifact の data_date と同じ

    def _boom_query(self, monkeypatch, client):
        def boom(*a, **kw):
            raise AssertionError("per-record 検索を打ってはならない")

        monkeypatch.setattr(client, "query_database", boom)

    def test_ヒットすれば検索も作成もしない(self, dry_client, tmp_path, monkeypatch):
        settings = make_settings()
        artifact = make_artifact(tmp_path)
        self._boom_query(monkeypatch, dry_client)
        page_id = file_upload.upload_raw_artifact(
            dry_client, settings, artifact,
            sha_map={artifact.sha256: "page-mapped"}, sha_map_date=self.DAY,
        )
        assert page_id == "page-mapped"
        assert artifact.notion_page_id == "page-mapped"
        assert dry_client.ops == []

    def test_ミスなら検索せず作ってmapへ足す(self, dry_client, tmp_path, monkeypatch):
        """対象日内は map を信用する。作った行は同一 run 内に見える。"""
        settings = make_settings()
        artifact = make_artifact(tmp_path)
        self._boom_query(monkeypatch, dry_client)
        sha_map: dict[str, str] = {}
        page_id = file_upload.upload_raw_artifact(
            dry_client, settings, artifact, sha_map=sha_map, sha_map_date=self.DAY
        )
        assert page_id.startswith("dry-run-")
        assert sha_map == {artifact.sha256: page_id}
        # 同一 run の再送は map ヒットで作成しない
        page_id2 = file_upload.upload_raw_artifact(
            dry_client, settings, artifact, sha_map=sha_map, sha_map_date=self.DAY
        )
        assert page_id2 == page_id
        creates = [o for o in dry_client.ops if o.op == "create_page"]
        assert len(creates) == 1

    def test_日付が違えばper_recordへ落とす(self, dry_client, tmp_path, monkeypatch):
        """遅延提出など対象日外の原本に map を信用しない（④ と同じ規則）。"""
        settings = make_settings()
        artifact = make_artifact(tmp_path)
        seen = []
        monkeypatch.setattr(
            dry_client, "query_database",
            lambda *a, **kw: seen.append(kw.get("filter")) or [],
        )
        file_upload.upload_raw_artifact(
            dry_client, settings, artifact,
            sha_map={}, sha_map_date=date(2026, 6, 11),
        )
        assert seen == [
            {"property": S.RAW_PROP_SHA256, "rich_text": {"equals": artifact.sha256}}
        ]

    def test_対象日で絞って最古勝ち(self, dry_client, tmp_path, monkeypatch):
        settings = make_settings()
        captured = {}

        def fake_query(db_id, *, filter=None, **kwargs):
            captured["db_id"] = db_id
            captured["filter"] = filter
            return [
                {
                    "id": "new",
                    "created_time": "2026-06-10T02:00:00.000Z",
                    "properties": {
                        S.RAW_PROP_SHA256: {"rich_text": [{"plain_text": "ab12"}]}
                    },
                },
                {
                    "id": "old",
                    "created_time": "2026-06-10T01:00:00.000Z",
                    "properties": {
                        S.RAW_PROP_SHA256: {"rich_text": [{"plain_text": "ab12"}]}
                    },
                },
            ]

        monkeypatch.setattr(dry_client, "query_database", fake_query)
        out = file_upload.load_raw_page_map(dry_client, settings, data_date=self.DAY)
        assert out == {"ab12": "old"}
        assert captured["db_id"] == "db-raw"
        assert captured["filter"] == {
            "property": S.PROP_DATA_DATE,
            "date": {"equals": "2026-06-10"},
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
        # create に content_type は渡さない（Notion が filename から推定し、その応答
        # 値を send のパートに一致させる。OS 依存の mimetypes を正本にしない）
        assert create_fu == {"mode": "single_part", "filename": artifact.filename}

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
    def test_ambiguous_page_creation_degrades_without_second_post(
        self, dry_client, tmp_path, monkeypatch
    ):
        """作成済みか不明でも再POSTせず、既存 RawUploadError 契約で原本を保全する。"""
        from unittest.mock import Mock

        from notion_client.errors import RequestTimeoutError

        from jp_stock_pipeline.notion.client import NotionClient

        settings = make_settings()
        artifact = make_artifact(tmp_path)
        client = NotionClient("test-token-not-a-credential")
        create = Mock(side_effect=RequestTimeoutError())
        monkeypatch.setattr(client._client.pages, "create", create)
        monkeypatch.setattr(client._throttle, "wait", lambda: None)
        monkeypatch.setattr(client, "query_database", lambda *a, **kw: [])
        monkeypatch.setattr(client, "raw_api", dry_client.raw_api)
        try:
            with pytest.raises(file_upload.RawUploadError, match="結果不明"):
                file_upload.upload_raw_artifact(client, settings, artifact)
            assert create.call_count == 1
            assert artifact.local_path.read_bytes() == b"raw-bytes"
            assert artifact.notion_page_id is None
        finally:
            client._client.close()
            client._session.close()

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
