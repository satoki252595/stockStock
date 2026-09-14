"""tdnet_hourly の短信 XBRL 原本保存に doc_id が渡ることの回帰テスト。

jss_raw_files.doc_id が全行 NULL だった原因の一つ: `_process_financial_xbrl`
が `save_raw` に `doc_id` を渡していなかった (fix/raw-doc-id-legacy-key)。
やのしんAPI・XBRL変換・Notion書込は実通信せず、コラボレータをすべて
monkeypatch/差し替えて `save_raw` 呼び出しの引数だけを検証する。
"""

from __future__ import annotations

import argparse
import types
from datetime import date, datetime, timezone

from jp_stock_pipeline.config import load_settings
from jp_stock_pipeline.jobs import tdnet_hourly as mod
from jp_stock_pipeline.jobs.runner import JobContext
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import DisclosureRecord, Provenance, Source
from jp_stock_pipeline.notion.client import NotionClient

UTC = timezone.utc


def _ctx(raw_data_dir: str) -> JobContext:
    settings = load_settings(env={"RAW_DATA_DIR": raw_data_dir}, dry_run=True)
    ctx = JobContext(
        settings=settings,
        client=NotionClient(None, rps=1000.0, dry_run=True),
        args=argparse.Namespace(),
    )
    # ①③の Notion/ローカル/Cloudflare 書込は本テストの対象外。呼ばれたことだけ
    # 分かればよいので no-op に差し替える（実 SQL/API を叩かない）。
    ctx.upload_raw = lambda artifact, **kw: "raw-page-id"  # noqa: ARG005
    ctx.mirror_xbrl_facts = lambda tidy, artifact: None  # noqa: ARG005
    ctx.persist = lambda record, notion_write, *, label, include_lifecycle=True: True  # noqa: ARG005
    ctx.cloud_financial_summary = lambda fin, *, doc_id, raw_sha256: None  # noqa: ARG005
    return ctx


def _record(doc_id: str = "81234567") -> DisclosureRecord:
    prov = Provenance(
        source=Source.TDNET,
        license_tag=LicenseTag.FACTUAL_CITE,
        data_date=date(2026, 9, 11),
        fetched_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
    )
    return DisclosureRecord(
        doc_id=doc_id,
        title="決算短信",
        disclosed_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
        provenance=prov,
        code="7203",
        doc_type="短信",
        has_xbrl=True,
    )


class TestProcessFinancialXbrlSavesDocId:
    def test_save_raw_receives_the_disclosure_doc_id(self, monkeypatch, tmp_path):
        captured: dict = {}

        def fake_save_raw(content, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                local_path=types.SimpleNamespace(read_bytes=lambda: b"zip-bytes"),
                sha256="f" * 64,
            )

        monkeypatch.setattr(
            mod, "fetch", lambda url, **kw: types.SimpleNamespace(content=b"zip-bytes")  # noqa: ARG005
        )
        monkeypatch.setattr(mod, "save_raw", fake_save_raw)
        monkeypatch.setattr(
            mod.xbrl_to_csv, "xbrl_zip_to_tidy", lambda content, code, doc_id: "tidy"  # noqa: ARG005
        )
        monkeypatch.setattr(mod.xbrl_to_csv, "write_tidy", lambda tidy, artifact: None)  # noqa: ARG005
        monkeypatch.setattr(
            mod.normalize,
            "tidy_to_financial_record",
            lambda tidy, code, prov, *, disclosed_at: types.SimpleNamespace(code="7203"),  # noqa: ARG005
        )

        ctx = _ctx(str(tmp_path))
        record = _record(doc_id="81234567")
        mod._process_financial_xbrl(  # noqa: SLF001
            ctx, record, "https://example/xbrl.zip", master_id="M1", master_resolved=True
        )

        assert captured["doc_id"] == "81234567"
        assert captured["scope"] == "7203"
