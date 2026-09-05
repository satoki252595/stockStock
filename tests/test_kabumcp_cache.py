"""任意キャッシュ連携の安全性。財務値は捏造せず、ZIP検証には実EDINET保存物を再利用。"""

from argparse import Namespace
from dataclasses import replace
from datetime import date, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest

from conftest import fixture_path
from jp_stock_pipeline.config import load_settings
from jp_stock_pipeline.http import FetchError
from jp_stock_pipeline.jobs import edinet_daily as job
from jp_stock_pipeline.jobs.runner import JobContext, STATUS_PARTIAL, _status
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import JST, RawArtifact, Source
from jp_stock_pipeline.notion.file_upload import RawUploadError

DOC_ID = "S1001234"


@pytest.fixture
def artifact():
    # フォーマット/バイト同一性のテストのみ。銘柄マスタの数値を財務として取り込まない。
    path = fixture_path("edinet/Edinetcode.zip")
    return RawArtifact(
        source=Source.EDINET, datatype="csv", scope="7203", data_date=date(2026, 6, 10),
        fetched_at=datetime(2026, 6, 10, tzinfo=JST),
        url=f"{job.edinet.EDINET_API_BASE}/documents/{DOC_ID}?type=5",
        local_path=path, sha256=sha256(path.read_bytes()).hexdigest(),
        size_bytes=path.stat().st_size, license_tag=LicenseTag.COMMERCIAL_OK,
    )


def context(cache_dir, *, dry_run=False):
    return JobContext(
        settings=load_settings(dry_run=dry_run, env={}), client=None,
        args=Namespace(kabumcp_cache_dir=cache_dir),
    )


def test_atomic_copy_unchanged_conflict_and_symlink(tmp_path, artifact):
    cache = tmp_path / "cache"
    target = cache / f"{DOC_ID}.zip"
    assert job._copy_kabumcp_csv(artifact, DOC_ID, cache) == "created"
    before = target.stat()
    assert target.read_bytes() == artifact.local_path.read_bytes()
    assert job._copy_kabumcp_csv(artifact, DOC_ID, cache) == "unchanged"
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert target.stat().st_ino == before.st_ino
    assert list(cache.iterdir()) == [target]  # 一時ファイルを残さない

    target.write_bytes(b"existing different bytes")
    with pytest.raises(ValueError, match="上書き拒否"):
        job._copy_kabumcp_csv(artifact, DOC_ID, cache)
    assert target.read_bytes() == b"existing different bytes"
    target.unlink()
    target.symlink_to(artifact.local_path.resolve())
    with pytest.raises(OSError):
        job._copy_kabumcp_csv(artifact, DOC_ID, cache)
    assert target.is_symlink()
    assert list(cache.iterdir()) == [target]


@pytest.mark.parametrize("change,doc_id", [
    ({"source": Source.YFINANCE}, DOC_ID),
    ({"license_tag": LicenseTag.FACTUAL_CITE}, DOC_ID),
    ({"datatype": "xbrl"}, DOC_ID),
    ({"sha256": "0" * 64}, DOC_ID),
    ({"url": "https://example.com/?type=5"}, DOC_ID),
    ({}, "../S10012"),
    ({}, "S1001234\n"),
])
def test_invalid_input_never_creates_cache(tmp_path, artifact, change, doc_id):
    cache = tmp_path / "cache"
    with pytest.raises(ValueError):
        job._copy_kabumcp_csv(replace(artifact, **change), doc_id, cache)
    assert not cache.exists()


def test_bad_zip_and_optional_modes(tmp_path, artifact, caplog):
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a ZIP")
    bad = replace(artifact, local_path=corrupt, sha256=sha256(corrupt.read_bytes()).hexdigest())
    cache = tmp_path / "cache"
    job._export_kabumcp_cache(context(None), artifact, DOC_ID)  # 既定OFF
    job._export_kabumcp_cache(context(cache, dry_run=True), artifact, DOC_ID)
    assert not cache.exists()
    ctx = context(cache)
    ctx.add_success()  # Notion/ローカル収集済み
    job._export_kabumcp_cache(ctx, bad, DOC_ID)
    job._export_kabumcp_cache(ctx, replace(artifact, datatype="xbrl"), DOC_ID)
    assert ctx.failed == 2
    assert _status(ctx, False) == STATUS_PARTIAL
    assert "type1 fallback" in caplog.text
    assert not cache.exists()


@pytest.mark.parametrize("raw_saved", [True, False])
def test_cache_only_after_persistence(tmp_path, artifact, monkeypatch, raw_saved):
    events = []
    ctx = context(tmp_path / "cache")
    monkeypatch.setattr(job, "_fetch_financial_tidy", lambda *a: (artifact, None))

    def save_raw(*a):
        events.append("raw")
        if not raw_saved:
            raise RawUploadError("not saved")
        return "raw-page"

    def no_pdf(*a, **kw):
        raise FetchError("PDF unavailable")

    monkeypatch.setattr(ctx, "upload_raw", save_raw)
    monkeypatch.setattr(job.edinet, "fetch_document", no_pdf)
    monkeypatch.setattr(job.edinet, "to_disclosure_record", lambda *a, **kw: SimpleNamespace(
        code="7203", disclosed_at=datetime(2026, 6, 10, tzinfo=JST),
    ))
    monkeypatch.setattr(ctx, "persist", lambda *a, **kw: events.append("disclosure") or True)
    monkeypatch.setattr(job, "_export_kabumcp_cache", lambda *a: events.append("cache"))
    doc = {"docID": DOC_ID, "docTypeCode": "120", "secCode": "72030"}
    if raw_saved:
        job._process_document(ctx, doc, "list-page", master_map_ok=True)
        assert events == ["raw", "disclosure", "cache"]
    else:
        with pytest.raises(RawUploadError):
            job._process_document(ctx, doc, "list-page", master_map_ok=True)
        assert events == ["raw"]
