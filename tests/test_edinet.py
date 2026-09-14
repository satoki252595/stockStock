"""EDINET API v2 コレクター (§4, §12) のテスト。

- documents.json / 書類本体は APIキー必須のため、実フィクスチャは
  scripts/capture_edinet.py で取得する。未取得なら skip (§3-6)
- documents_error_401.json はキー無しで実取得した実エラーレスポンス
  （HTTP 200 + JSON ボディ）で、エラーレスポンス判定の検証に使う
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import dry_settings, fixture_path

from jp_stock_pipeline.collectors import edinet as mod
from jp_stock_pipeline.config import ConfigError
from jp_stock_pipeline.http import FetchError
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import Source


def _settings(tmp_path, api_key: str | None = "test-key"):
    extra = {"EDINET_API_KEY": api_key} if api_key else {}
    return dry_settings(tmp_path, **extra)


class TestDocTypeMapping:
    def test_constants(self):
        assert mod.DOC_TYPE_ANNUAL_REPORT == "120"
        assert mod.DOC_TYPE_ANNUAL_REPORT_AMEND == "130"
        assert mod.DOC_TYPE_QUARTERLY_REPORT == "140"
        assert mod.DOC_TYPE_SEMIANNUAL_REPORT == "160"
        assert mod.DOC_TYPE_LARGE_HOLDING == "350"
        assert mod.DOC_TYPE_LARGE_HOLDING_AMEND == "360"

    def test_labels(self):
        assert mod.doc_type_label("120") == "有報"
        assert mod.doc_type_label("130") == "有報"
        assert mod.doc_type_label("140") == "四半期報告"
        # 160/170 は 2024-04 に四半期報告書を置き換えた半期報告書。別の書類なので
        # 「四半期報告」に混ぜない（混ぜると制度の前後で件数の意味が変わる）。
        assert mod.doc_type_label("150") == "四半期報告"
        assert mod.doc_type_label("160") == "半期報告"
        assert mod.doc_type_label("170") == "半期報告"
        assert mod.doc_type_label("350") == "大量保有"
        assert mod.doc_type_label("360") == "大量保有"

    def test_unknown_is_other(self):
        assert mod.doc_type_label("999") == "その他"
        assert mod.doc_type_label(None) == "その他"
        assert mod.doc_type_label("") == "その他"

    def test_target_codes(self):
        assert "120" in mod.TARGET_DOC_TYPE_CODES
        assert "350" in mod.TARGET_DOC_TYPE_CODES
        assert "999" not in mod.TARGET_DOC_TYPE_CODES


class TestApiKeyRequired:
    def test_list_documents_requires_key(self, tmp_path):
        settings = _settings(tmp_path, api_key=None)
        with pytest.raises(ConfigError):
            mod.list_documents(settings, date(2026, 6, 9))

    def test_fetch_document_requires_key(self, tmp_path):
        settings = _settings(tmp_path, api_key=None)
        with pytest.raises(ConfigError):
            mod.fetch_document(settings, "S100ABCD", 1)


class TestErrorResponses:
    """実取得済みのエラーレスポンス（HTTP 200 + JSON）の扱い (§3: 実データのみ保存)。"""

    def _error_resp(self):
        data = fixture_path("edinet/documents_error_401.json").read_bytes()

        class _Resp:
            content = data
            headers = {"Content-Type": "application/json; charset=utf-8"}  # 実レスポンスの値

        return _Resp()

    def test_list_documents_error_body_raises(self, tmp_path, monkeypatch):
        resp = self._error_resp()
        monkeypatch.setattr(mod, "fetch", lambda url, **kw: resp)
        settings = _settings(tmp_path)
        with pytest.raises(FetchError):
            mod.list_documents(settings, date(2026, 6, 9))
        # エラーレスポンスは原本として保存されない
        assert list(tmp_path.iterdir()) == []

    def test_fetch_document_json_content_type_raises(self, tmp_path, monkeypatch):
        resp = self._error_resp()
        monkeypatch.setattr(mod, "fetch", lambda url, **kw: resp)
        settings = _settings(tmp_path)
        with pytest.raises(FetchError):
            mod.fetch_document(settings, "S100ABCD", 1)
        assert list(tmp_path.iterdir()) == []

    def test_invalid_fetch_type_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            mod.fetch_document(_settings(tmp_path), "S100ABCD", 3)


class TestFetchDocumentPassesDocId:
    """jss_raw_files.doc_id が全行 NULL だった原因: save_raw に doc_id を渡していなかった。"""

    def test_fetch_document_passes_doc_id_to_save_raw(self, tmp_path, monkeypatch):
        class _Resp:
            content = b"%PDF-1.4 fake body for test"
            headers = {"Content-Type": "application/pdf"}

        monkeypatch.setattr(mod, "fetch", lambda url, **kw: _Resp())
        captured: dict = {}

        def fake_save_raw(content, **kwargs):
            captured.update(kwargs)
            return "SENTINEL"

        monkeypatch.setattr(mod, "save_raw", fake_save_raw)
        settings = _settings(tmp_path)
        result = mod.fetch_document(
            settings, "S100ABCD", 2, code="7203", data_date=date(2026, 6, 25)
        )
        assert result == "SENTINEL"
        assert captured["doc_id"] == "S100ABCD"


class TestWithRealDocumentsList:
    """実 documents.json フィクスチャ（要APIキー取得）でのテスト。未取得は skip。"""

    def _results(self) -> list[dict]:
        body = json.loads(fixture_path("edinet/documents_list_sample.json").read_text("utf-8"))
        results = body.get("results", [])
        assert isinstance(results, list)
        return results

    def test_list_documents_saves_raw(self, tmp_path, monkeypatch):
        data = fixture_path("edinet/documents_list_sample.json").read_bytes()

        class _Resp:
            content = data
            headers = {"Content-Type": "application/json; charset=utf-8"}

        monkeypatch.setattr(mod, "fetch", lambda url, **kw: _Resp())
        settings = _settings(tmp_path)
        artifact, results = mod.list_documents(settings, date(2026, 6, 9))
        assert artifact.local_path.read_bytes() == data  # 無加工保存
        assert artifact.datatype == "documents_list"
        assert artifact.license_tag is LicenseTag.COMMERCIAL_OK
        assert "Subscription-Key" not in artifact.url  # 資格情報を残さない
        assert isinstance(results, list)

    def test_to_disclosure_record_real_docs(self):
        results = self._results()
        targets = [d for d in results if mod.is_target_document(d) and mod.has_sec_code(d)]
        if not targets:
            pytest.skip("この日付の一覧に対象書類（secCode あり）が無い")
        for doc in targets:
            rec = mod.to_disclosure_record(doc)
            assert rec.doc_id == doc["docID"]
            assert rec.disclosed_at.tzinfo is not None
            assert rec.code is not None and len(rec.code) == 4  # secCode 5桁→4桁
            assert rec.doc_type in ("有報", "四半期報告", "半期報告", "大量保有", "その他")
            assert rec.has_xbrl == (str(doc.get("xbrlFlag")) == "1")
            prov = rec.provenance
            assert prov.source is Source.EDINET
            assert prov.license_tag is LicenseTag.COMMERCIAL_OK
            assert prov.data_date == rec.disclosed_at.date()
