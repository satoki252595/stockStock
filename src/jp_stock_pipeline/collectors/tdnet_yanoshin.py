"""TDnet 適時開示コレクター（やのしん TDnet WEB-API 経由）(DESIGN.md §4 トラックA, §12)。

エンドポイント (§12):
    GET https://webapi.yanoshin.jp/webapi/tdnet/list/{target}.json?limit=N
    target = "recent" | "YYYYmmdd" | 銘柄コード（公開API・キー不要）

実レスポンス構造（2026-06-10 取得のフィクスチャで確認）:
- ``items[]`` は target によって 2 形式が実在する:
  - ``recent``: ``items[].Tdnet`` 配下に各フィールド（ラップ形式）
  - ``YYYYmmdd``: ``items[]`` 直下に各フィールド（フラット形式）
  パーサは両形式を受け付ける。
- フィールド: ``id`` / ``company_code``(5桁) / ``company_name`` /
  ``pubdate``("YYYY-MM-DD HH:MM:SS" JST) / ``title`` / ``document_url`` /
  ``url_xbrl`` / ``markets_string`` / ``update_history`` 等。
- ``document_url`` は ``https://webapi.yanoshin.jp/rd.php?<直URL>`` 形式の
  リダイレクトラッパで返る場合がある（recent で確認）→ 直URLへ展開する。

ライセンス: やのしんAPIは個人運営の非公式API (§2.1)。開示メタデータは
``factual-cite`` として扱い、原文PDFは内部保管に留める。
個人運営ゆえ停止リスクがあるため、公式 TDnet ページパースの
``collectors/tdnet_official_fallback.py`` と同一インターフェースを提供する
（§4 ソース抽象化, §11）。

インターフェース契約（フォールバックと共通・§4）:
    list_disclosures*(settings, target, ...) -> (RawArtifact, list[DisclosureRecord])
真実性 (§3): 取得失敗は FetchError のまま送出し、欠損は欠損として呼び出し側
（ジョブ）が記録する。値の推定・補間は一切行わない。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from urllib.parse import urlsplit

from ..config import Settings
from ..http import FetchError, fetch
from ..licensing import source_license
from ..models import JST, DisclosureRecord, Provenance, RawArtifact, Source
from ..rawstore import save_raw

logger = logging.getLogger(__name__)

BASE_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list"

# ---------------------------------------------------------------------------
# 書類種別の判定（④ 開示書類DB「書類種別」select と一致 §6.4）
# 判定キーワードは定数化し tests/test_tdnet_classify.py で実在タイトルにより検証する
# ---------------------------------------------------------------------------

DOC_TYPE_TANSHIN = "短信"
DOC_TYPE_FORECAST_REVISION = "業績修正"
DOC_TYPE_DIVIDEND_REVISION = "配当修正"
DOC_TYPE_BUYBACK = "自社株買い"
DOC_TYPE_LARGE_HOLDING = "大量保有"
DOC_TYPE_ANNUAL_REPORT = "有報"
DOC_TYPE_QUARTERLY_REPORT = "四半期報告"
DOC_TYPE_OTHER = "その他"

KW_TANSHIN = "決算短信"
KW_FORECAST = "業績予想"
KW_REVISION = "修正"
KW_DIVIDEND = "配当"
KW_TREASURY = "自己株式"
KW_ACQUIRE = "取得"
KW_LARGE_HOLDING = "大量保有"
KW_ANNUAL_REPORT = "有価証券報告書"
KW_QUARTERLY_REPORT = "四半期報告書"


def classify_title(title: str) -> str:
    """開示タイトルから書類種別を判定する (§6.4 書類種別 select)。

    判定は上から順の優先度付き（例: 「第１四半期決算短信」は短信扱い、
    「業績予想及び配当予想の修正」は業績修正扱い）。
    どれにも該当しなければ「その他」。推定はせずキーワード一致のみ。
    """
    if KW_TANSHIN in title:
        return DOC_TYPE_TANSHIN
    if KW_FORECAST in title and KW_REVISION in title:
        return DOC_TYPE_FORECAST_REVISION
    if KW_DIVIDEND in title and KW_REVISION in title:
        return DOC_TYPE_DIVIDEND_REVISION
    if KW_TREASURY in title and KW_ACQUIRE in title:
        return DOC_TYPE_BUYBACK
    if KW_LARGE_HOLDING in title:
        return DOC_TYPE_LARGE_HOLDING
    if KW_ANNUAL_REPORT in title:
        return DOC_TYPE_ANNUAL_REPORT
    if KW_QUARTERLY_REPORT in title:
        return DOC_TYPE_QUARTERLY_REPORT
    return DOC_TYPE_OTHER


# ---------------------------------------------------------------------------
# フィールド正規化（値不変の形式変換のみ §5.2）
# ---------------------------------------------------------------------------


def normalize_company_code(raw_code: str | None) -> str | None:
    """TDnet の5桁コードを4桁基準コードへ正規化する。

    TDnet は末尾1桁（証券種別の識別子。普通株は "0"、ETF等は "4" 等の実例あり）
    を付けた5桁で返す（実例: "70500"→"7050", "316A0"→"316A", "16714"→"1671"）。
    4桁ちょうどならそのまま。判別できない形式は加工せず None（推定しない §3-1）。
    """
    code = (raw_code or "").strip()
    if re.fullmatch(r"[0-9A-Z]{5}", code):
        return code[:4]
    if re.fullmatch(r"[0-9A-Z]{4}", code):
        return code
    return None


def direct_document_url(url: str) -> str:
    """やのしんの rd.php リダイレクトラッパを直URLへ展開する。

    例: https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/...pdf
    ラッパでなければそのまま返す（値不変）。
    """
    marker = "rd.php?"
    idx = url.find(marker)
    if idx >= 0:
        return url[idx + len(marker):]
    return url


def doc_id_from_document_url(url: str | None) -> str | None:
    """document_url の PDF ファイル名から安定IDを導出する。

    TDnet の PDF ファイル名（例 140120260610567733.pdf）は開示1件に一意で、
    公式ページフォールバック (tdnet_official_fallback) と同一の doc_id になる
    ため、④ の冪等 upsert キー (CONTRACTS 不変条件5) としてソース間で安定する。
    導出できなければ None。
    """
    if not url:
        return None
    path = urlsplit(direct_document_url(url)).path
    name = path.rsplit("/", 1)[-1]
    if name.lower().endswith(".pdf") and len(name) > 4:
        return name[: -len(".pdf")]
    return None


def _parse_pubdate(pubdate: str) -> datetime:
    """pubdate "YYYY-MM-DD HH:MM:SS"（JST表記）を tz 付き datetime にする。"""
    return datetime.strptime(pubdate, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)


def _unwrap_item(item: dict) -> dict:
    """items[] 要素を両形式（Tdnet ラップ / フラット）から取り出す。"""
    inner = item.get("Tdnet")
    return inner if isinstance(inner, dict) else item


def xbrl_url_map(payload: dict) -> dict[str, str]:
    """payload から doc_id → XBRL zip 直URL の対応を作る（短信XBRL→③用 §8.2）。

    url_xbrl が無い開示は含めない（欠損は欠損のまま §3-1）。
    """
    mapping: dict[str, str] = {}
    for item in payload.get("items", []):
        t = _unwrap_item(item)
        doc_id = doc_id_from_document_url(t.get("document_url"))
        url_xbrl = t.get("url_xbrl")
        if doc_id and url_xbrl:
            mapping[doc_id] = direct_document_url(str(url_xbrl))
    return mapping


def parse_list_payload(
    payload: dict,
    *,
    fetched_at: datetime,
    raw_page_id: str | None = None,
) -> list[DisclosureRecord]:
    """やのしん list レスポンス JSON を DisclosureRecord 群へ変換する（値不変）。

    取得できないフィールドは None のまま（§3-1）。doc_id は document_url の
    PDF ファイル名由来を優先し、無ければやのしんの id を使う。
    """
    records: list[DisclosureRecord] = []
    for item in payload.get("items", []):
        t = _unwrap_item(item)
        title = (t.get("title") or "").strip()
        pubdate = t.get("pubdate")
        if not pubdate:
            # 開示日時が無い行は④の必須キーを満たせない。捏造せずスキップして記録
            logger.warning("pubdate 欠損のためスキップ: id=%s title=%s", t.get("id"), title[:40])
            continue
        disclosed_at = _parse_pubdate(pubdate)
        source_url = direct_document_url(t["document_url"]) if t.get("document_url") else None
        doc_id = doc_id_from_document_url(source_url)
        if doc_id is None:
            yanoshin_id = t.get("id")
            if not yanoshin_id:
                logger.warning("doc_id を導出できずスキップ: title=%s", title[:40])
                continue
            doc_id = f"yanoshin-{yanoshin_id}"
        records.append(
            DisclosureRecord(
                doc_id=doc_id,
                title=title,
                disclosed_at=disclosed_at,
                provenance=Provenance(
                    source=Source.TDNET,
                    license_tag=source_license(Source.TDNET),  # factual-cite (§2.2)
                    data_date=disclosed_at.date(),
                    fetched_at=fetched_at,
                    raw_page_id=raw_page_id,
                ),
                code=normalize_company_code(t.get("company_code")),
                doc_type=classify_title(title),
                source_url=source_url,
                has_xbrl=bool(t.get("url_xbrl")),
            )
        )
    return records


# ---------------------------------------------------------------------------
# 取得（§8.1 step 1-2: fetch → save_raw）
# ---------------------------------------------------------------------------


def _target_data_date(target: str) -> date | None:
    """target が YYYYmmdd ならその日付。recent / 銘柄コードは特定日を持たず None。"""
    if re.fullmatch(r"\d{8}", target):
        return datetime.strptime(target, "%Y%m%d").date()
    return None


def list_disclosures(
    settings: Settings,
    target: str = "recent",
    limit: int = 300,
) -> tuple[RawArtifact, list[DisclosureRecord]]:
    """適時開示一覧を取得し、原本保存して DisclosureRecord 群を返す。

    Args:
        settings: 共通設定（raw_data_dir を使用）
        target: "recent" | "YYYYmmdd" | 銘柄コード
        limit: 最大件数（やのしん側パラメータ）

    Returns:
        (RawArtifact, list[DisclosureRecord])
        RawArtifact は datatype="tdnet_list", scope=target, license_tag=factual-cite。
        ⑤へのアップロードと provenance.raw_page_id の設定は呼び出し側ジョブが行う
        （§8.1 step 4: 原本必須）。

    Note:
        tdnet_official_fallback.list_disclosures_official と同一の返り値契約
        （ソース抽象化 §4。やのしん停止時は差し替え可能 §11）。
    """
    url = f"{BASE_URL}/{target}.json"
    resp = fetch(url, params={"limit": limit})
    content = resp.content
    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise FetchError(f"やのしん list 応答が JSON でない: {url}") from exc
    # 構造検証 (CONTRACTS「HTTP 200 のエラーレスポンス」): items が list でない
    # 応答はエラー/制限メッセージとみなし、原本保存せず FetchError とする。
    # {"items": []} は正当な0件として通す
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise FetchError(f"やのしん list 応答が想定外構造 (items 欠落): {url}")
    artifact = save_raw(
        content,
        source=Source.TDNET,
        datatype="tdnet_list",
        scope=target,
        data_date=_target_data_date(target),
        url=f"{url}?limit={limit}",
        ext="json",
        license_tag=source_license(Source.TDNET),
        base_dir=settings.raw_data_dir,
    )
    records = parse_list_payload(payload, fetched_at=artifact.fetched_at)
    return artifact, records


def fetch_disclosure_pdf(settings: Settings, record: DisclosureRecord) -> RawArtifact:
    """開示原文 PDF を実取得し原本保存する（datatype="tdnet_pdf"）。

    原文の著作権は各上場会社 (§2.1) → factual-cite として内部保管に留める。
    PDF でないレスポンス（エラーページ等）は原本として保存せず FetchError
    （ダミー原本を作らない §3）。
    """
    if not record.source_url:
        raise FetchError(f"開示 {record.doc_id} に document_url が無い")
    resp = fetch(record.source_url)
    content = resp.content
    if not content.startswith(b"%PDF"):
        raise FetchError(f"PDF でないレスポンス: {record.source_url}")
    return save_raw(
        content,
        source=Source.TDNET,
        datatype="tdnet_pdf",
        scope=record.doc_id,  # 1開示=1原本。doc_id でファイル名が衝突しない
        data_date=record.disclosed_at.date(),
        url=record.source_url,
        ext="pdf",
        license_tag=source_license(Source.TDNET),
        base_dir=settings.raw_data_dir,
    )
