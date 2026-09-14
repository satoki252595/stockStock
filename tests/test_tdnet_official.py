"""公式 TDnet 閲覧サービス フォールバックのテスト (DESIGN.md §11)。

フィクスチャは 2026-06-10 に実取得した公式一覧 HTML (§3-6):
- tdnet/official_I_list_001_20260610.html … 1ページ目（100件・全180件中）
- tdnet/official_I_list_002_20260610.html … 2ページ目（80件）
（I_list_003_20260610.html は 404 を実確認 → 2ページで完結）
未取得の場合は skip（scripts/capture_tdnet.py で再取得可能）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from jp_stock_pipeline.collectors import tdnet_official_fallback as tof
from jp_stock_pipeline.collectors import tdnet_yanoshin as ty
from jp_stock_pipeline.http import FetchError
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import Source, now_jst
from jp_stock_pipeline.rawstore import sha256_bytes

from conftest import dry_settings, fixture_path

TARGET = date(2026, 6, 10)


def _page(n: int) -> bytes:
    return fixture_path(f"tdnet/official_I_list_{n:03d}_20260610.html").read_bytes()


def _settings(tmp_path):
    return dry_settings(tmp_path)


class _StubResponse:
    def __init__(self, content: bytes):
        self.content = content


# ---------------------------------------------------------------------------
# HTML パース（実フィクスチャ）
# ---------------------------------------------------------------------------


def test_parse_1ページ目は100件():
    records = tof.parse_official_page(
        _page(1).decode("utf-8"), TARGET, fetched_at=now_jst()
    )
    assert len(records) == 100

    first = records[0]  # 19:25 70500 Ｇ－フロンティアＩ 事業譲受に関するお知らせ
    assert first.doc_id == "140120260610567733"
    assert first.code == "7050"  # 5桁→4桁
    assert first.title == "事業譲受に関するお知らせ"
    assert first.disclosed_at.strftime("%Y-%m-%d %H:%M") == "2026-06-10 19:25"
    assert first.disclosed_at.utcoffset() == timedelta(hours=9)  # JST tz付き
    # 相対リンク "140120260610567733.pdf" の絶対URL化
    assert first.source_url == "https://www.release.tdnet.info/inbs/140120260610567733.pdf"
    assert first.has_xbrl is False
    # 来歴 (§3-3)
    assert first.provenance.source is Source.TDNET
    assert first.provenance.license_tag is LicenseTag.FACTUAL_CITE
    assert first.provenance.data_date == TARGET


def test_parse_XBRL列とフィールド不変条件():
    records = tof.parse_official_page(
        _page(1).decode("utf-8"), TARGET, fetched_at=now_jst()
    )
    # 1ページ目の XBRL リンク付き行は16件（実HTMLで確認済み）
    assert sum(1 for r in records if r.has_xbrl) == 16
    # 決算短信（XBRL付き）の実例
    tanshin = next(r for r in records if r.doc_id == "140120260610567076")
    assert tanshin.code == "2751"
    assert tanshin.doc_type == ty.DOC_TYPE_TANSHIN
    assert tanshin.has_xbrl is True
    for r in records:
        assert r.code is None or len(r.code) == 4
        assert r.disclosed_at.tzinfo is not None
        assert r.source_url.startswith("https://www.release.tdnet.info/inbs/")


def test_pager解析でページ番号集合を検出():
    assert tof._known_page_numbers(_page(1).decode("utf-8"), TARGET) == {1, 2}
    assert tof._known_page_numbers(_page(2).decode("utf-8"), TARGET) == {1, 2}
    # 別日のリンクは数えない
    assert tof._known_page_numbers(_page(1).decode("utf-8"), date(2026, 6, 9)) == set()


def test_page_url形式():
    assert (
        tof.page_url(TARGET, 1)
        == "https://www.release.tdnet.info/inbs/I_list_001_20260610.html"
    )


# ---------------------------------------------------------------------------
# 複数ページ取得（fetch を実フィクスチャbytesで差し替え。404で停止）
# ---------------------------------------------------------------------------


def _stub_fetch_factory(requested: list[str]):
    pages = {
        tof.page_url(TARGET, 1): _page(1),
        tof.page_url(TARGET, 2): _page(2),
    }

    def stub(url, **kw):
        requested.append(url)
        if url not in pages:
            raise FetchError(f"取得失敗: {url}: HTTP 404")  # 実挙動: 3ページ目は404
        return _StubResponse(pages[url])

    return stub


def test_fetch_list_pages_全ページを原本保存(tmp_path, monkeypatch):
    requested: list[str] = []
    monkeypatch.setattr(tof, "fetch", _stub_fetch_factory(requested))

    pages = tof.fetch_list_pages(_settings(tmp_path), TARGET)

    assert len(pages) == 2  # ページャが示す 1,2 のみ取得（404 ページへ行かない）
    assert requested == [tof.page_url(TARGET, 1), tof.page_url(TARGET, 2)]
    for n, (artifact, records) in enumerate(pages, start=1):
        # 1ページ取得=1原本 (§5.1)。無加工保存 (§5.2)
        assert artifact.local_path.read_bytes() == _page(n)
        assert artifact.sha256 == sha256_bytes(_page(n))
        assert artifact.source is Source.TDNET
        assert artifact.datatype == "tdnet_official_list"
        assert artifact.scope == f"p{n:03d}"
        assert artifact.data_date == TARGET
        assert artifact.license_tag is LicenseTag.FACTUAL_CITE
    assert [len(recs) for _a, recs in pages] == [100, 80]


def test_list_disclosures_official_やのしんと同一契約(tmp_path, monkeypatch):
    monkeypatch.setattr(tof, "fetch", _stub_fetch_factory([]))

    artifact, records = tof.list_disclosures_official(_settings(tmp_path), TARGET)

    # 返り値契約 = (RawArtifact, list[DisclosureRecord])（§4 ソース抽象化）
    assert artifact.datatype == "tdnet_official_list"
    assert len(records) == 180  # 全ページ分
    # doc_id 一意（④ 冪等 upsert キー）
    assert len({r.doc_id for r in records}) == 180


def test_1ページ目404は送出(tmp_path, monkeypatch):
    """休日等で一覧が存在しない日は欠損として扱う（捏造しない §3-2）。"""
    def stub(url, **kw):
        raise FetchError(f"取得失敗: {url}: HTTP 404")

    monkeypatch.setattr(tof, "fetch", stub)
    try:
        tof.fetch_list_pages(_settings(tmp_path), date(2026, 6, 7))  # 日曜
    except FetchError:
        pass
    else:
        raise AssertionError("FetchError が送出されるべき")


# ---------------------------------------------------------------------------
# ソース間整合: やのしんと公式で同一の DisclosureRecord になる (§4, §11)
# ---------------------------------------------------------------------------


def test_やのしんと公式の同日データが一致する():
    """同一日の実フィクスチャ同士で doc_id / code / has_xbrl / 分時刻が一致。

    フォールバック切替時 (§11) に④の冪等 upsert キーが安定することを保証する。
    """
    payload = json.loads(
        fixture_path("tdnet/yanoshin_list_20260610.json").read_text(encoding="utf-8")
    )
    now = now_jst()
    yanoshin = {r.doc_id: r for r in ty.parse_list_payload(payload, fetched_at=now)}
    official = tof.parse_official_page(
        _page(1).decode("utf-8"), TARGET, fetched_at=now
    ) + tof.parse_official_page(_page(2).decode("utf-8"), TARGET, fetched_at=now)

    assert set(yanoshin) == {r.doc_id for r in official}
    for rec in official:
        y = yanoshin[rec.doc_id]
        assert rec.code == y.code
        assert rec.has_xbrl == y.has_xbrl
        assert rec.source_url == y.source_url
        # 公式ページは分単位のため秒を落として比較
        assert rec.disclosed_at == y.disclosed_at.replace(second=0)
        assert rec.doc_type == y.doc_type
