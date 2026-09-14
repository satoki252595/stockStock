"""やのしん TDnet コレクターのテスト (DESIGN.md §4 トラックA, §12)。

フィクスチャは 2026-06-10 に実取得したやのしんAPIレスポンス (§3-6):
- tdnet/yanoshin_list_recent.json   … recent（items[].Tdnet ラップ形式）
- tdnet/yanoshin_list_20260610.json … 日付指定（items[] フラット形式・180件）
- tdnet/disclosure_140120260610567733.pdf … 実開示PDF
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from jp_stock_pipeline.collectors import tdnet_yanoshin as ty
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import Source, now_jst
from jp_stock_pipeline.rawstore import sha256_bytes

from conftest import dry_settings, fixture_path


def _settings(tmp_path):
    return dry_settings(tmp_path)


def _load(relative: str) -> dict:
    return json.loads(fixture_path(relative).read_text(encoding="utf-8"))


class _StubResponse:
    def __init__(self, content: bytes):
        self.content = content


# ---------------------------------------------------------------------------
# フィールド正規化（純関数）
# ---------------------------------------------------------------------------


class TestNormalizeCompanyCode:
    def test_5文字から4文字化(self):
        # TDnet は末尾に検査文字1文字を付けた5文字で返す
        assert ty.normalize_company_code("70500") == "7050"
        assert ty.normalize_company_code("316A0") == "316A"

    def test_4桁はそのまま(self):
        assert ty.normalize_company_code("7203") == "7203"

    def test_末尾検査文字が0でない5文字はNone(self):
        """仕様変更。以前は `"12024" == "1202"` を要求していた。

        元の意図は「末尾は "0" 以外の実例もある」で、実際に
        2026-06-10 の TDnet フィクスチャに末尾が "0" でない実例が 1 件あったと
        当時のコメントが記録している（コードは伏せる。フィクスチャは公開
        リポジトリに置けず .gitignore 済みなので、ここでは再検証できない）。

        それでも None に倒すのは、「5文字なら先頭4文字」規則が **別の証券を
        既存銘柄に取り違える**ため。`"25935"`（伊藤園 第1種優先株式）は
        `"2593"`（同社 普通株）になり、本番 core_stocks に両方が実在するので
        優先株の開示が普通株のページへ付く。取り違えは取りこぼしより重い。

        取りこぼし側の実害は無い（実測）:

        - 本番 ir_disclosures 37,641 行の company_code は**全件が末尾 "0"**。
          末尾非0の開示は1件も取り込まれていない。
        - `1202` は合成コードで core_stocks に不在（母集団は内国普通株のみ）。
          旧実装でも ingest の母集団突合（`codeToId.get()` 相当）で落ちていた。

        つまりこの変更で落ちる行は、従来も1行あとで落ちていた行だけ。
        """
        assert ty.normalize_company_code("12024") is None

    def test_種類株コードを普通株に丸めない(self):
        # 伊藤園第1種優先株式。旧実装は "2593"（同社 普通株）を返していた。
        assert ty.normalize_company_code("25935") is None
        # 実在しないコード "0720" を捏造していた（本番に先頭0のコードは0件）。
        assert ty.normalize_company_code("07203") is None

    def test_1桁目英字はNone(self):
        # 旧実装は正規表現 [0-9A-Z]{4,5} で "A130" を通していた。
        # JPX の付番体系では英字は2桁目/4桁目のみ。
        assert ty.normalize_company_code("A130") is None

    def test_表記揺れは吸収する(self):
        # 小文字・全角・前後空白は「同じ値の別表記」なので正規化して受理する。
        # 旧実装はいずれも None にしており、EDINET 側の実装と割れていた。
        assert ty.normalize_company_code("130a") == "130A"
        assert ty.normalize_company_code("７２０３") == "7203"
        assert ty.normalize_company_code(" 7203 ") == "7203"

    def test_解釈できない形式はNone(self):
        # 推定しない (§3-1)
        assert ty.normalize_company_code(None) is None
        assert ty.normalize_company_code("") is None
        assert ty.normalize_company_code("123") is None
        assert ty.normalize_company_code("1234567") is None


class TestDocId:
    def test_rdphpラッパの展開(self):
        # recent フィクスチャ実例の document_url 形式
        wrapped = (
            "https://webapi.yanoshin.jp/rd.php?"
            "https://www.release.tdnet.info/inbs/140120260610567733.pdf"
        )
        direct = "https://www.release.tdnet.info/inbs/140120260610567733.pdf"
        assert ty.direct_document_url(wrapped) == direct
        assert ty.direct_document_url(direct) == direct

    def test_doc_idはPDFファイル名由来(self):
        # 公式フォールバックと同一の安定ID (CONTRACTS 不変条件5)
        url = "https://www.release.tdnet.info/inbs/140120260610567733.pdf"
        assert ty.doc_id_from_document_url(url) == "140120260610567733"

    def test_導出不能はNone(self):
        assert ty.doc_id_from_document_url(None) is None
        assert ty.doc_id_from_document_url("https://example.com/page.html") is None


# ---------------------------------------------------------------------------
# レスポンスJSONのパース（実フィクスチャ・両形式）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "tdnet/yanoshin_list_recent.json",  # Tdnet ラップ形式
        "tdnet/yanoshin_list_20260610.json",  # フラット形式
    ],
)
def test_parse_両形式で全itemsがレコード化される(relative):
    payload = _load(relative)
    records = ty.parse_list_payload(payload, fetched_at=now_jst())
    assert len(records) == len(payload["items"]) > 0

    for rec, item in zip(records, payload["items"], strict=True):
        t = item.get("Tdnet", item)
        # フィールドマッピング（値不変 §5.2）
        assert rec.title == t["title"].strip()
        # 5桁→4桁
        assert rec.code == t["company_code"][:4]
        # JST tz付き datetime
        assert rec.disclosed_at.utcoffset() == timedelta(hours=9)
        assert rec.disclosed_at.strftime("%Y-%m-%d %H:%M:%S") == t["pubdate"]
        # has_xbrl = url_xbrl の有無
        assert rec.has_xbrl is bool(t["url_xbrl"])
        # doc_id は document_url の PDF ファイル名由来
        assert rec.doc_id == rec.source_url.rsplit("/", 1)[-1].removesuffix(".pdf")
        # 来歴 (§3-3): TDnet / factual-cite / データ基準日=開示日
        assert rec.provenance.source is Source.TDNET
        assert rec.provenance.license_tag is LicenseTag.FACTUAL_CITE
        assert rec.provenance.data_date == rec.disclosed_at.date()


def test_parse_recent先頭レコードの具体値():
    """recent フィクスチャ（2026-06-10 取得）の先頭1件を具体値で検証する。"""
    payload = _load("tdnet/yanoshin_list_recent.json")
    rec = ty.parse_list_payload(payload, fetched_at=now_jst())[0]
    t = payload["items"][0]["Tdnet"]
    assert rec.doc_id == ty.doc_id_from_document_url(t["document_url"])
    # rd.php ラッパは直URLへ展開されている
    assert rec.source_url.startswith("https://www.release.tdnet.info/")
    assert "rd.php" not in rec.source_url
    assert len(rec.code) == 4


def test_parse_書類種別はclassify_titleと一致():
    payload = _load("tdnet/yanoshin_list_20260610.json")
    records = ty.parse_list_payload(payload, fetched_at=now_jst())
    for rec in records:
        assert rec.doc_type == ty.classify_title(rec.title)


# ---------------------------------------------------------------------------
# list_disclosures（fetch を実フィクスチャbytesで差し替え）
# ---------------------------------------------------------------------------


def test_list_disclosures_原本保存とレコード(tmp_path, monkeypatch):
    raw = fixture_path("tdnet/yanoshin_list_20260610.json").read_bytes()
    monkeypatch.setattr(ty, "fetch", lambda url, **kw: _StubResponse(raw))
    settings = _settings(tmp_path)

    artifact, records = ty.list_disclosures(settings, "20260610", limit=300)

    # 原本 (§8.1 step 2): 無加工保存・SHA256・メタデータ
    assert artifact.local_path.read_bytes() == raw
    assert artifact.sha256 == sha256_bytes(raw)
    assert artifact.source is Source.TDNET
    assert artifact.datatype == "tdnet_list"
    assert artifact.scope == "20260610"
    assert artifact.data_date == date(2026, 6, 10)
    assert artifact.license_tag is LicenseTag.FACTUAL_CITE
    assert "limit=300" in artifact.url
    # レコード（このフィクスチャは180件）
    assert len(records) == 180


def test_list_disclosures_recentはdata_dateなし(tmp_path, monkeypatch):
    raw = fixture_path("tdnet/yanoshin_list_recent.json").read_bytes()
    monkeypatch.setattr(ty, "fetch", lambda url, **kw: _StubResponse(raw))
    artifact, records = ty.list_disclosures(_settings(tmp_path), "recent", limit=30)
    # "recent" は特定日を指さない → 推定せず None (§3-1)
    assert artifact.data_date is None
    assert artifact.scope == "recent"
    assert len(records) == len(json.loads(raw)["items"])

