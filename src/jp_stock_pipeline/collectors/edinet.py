"""EDINET API v2 コレクター (DESIGN.md §4, §12)。

- 書類一覧: GET {API}/documents.json?date=YYYY-MM-DD&type=2&Subscription-Key=KEY
- 書類取得: GET {API}/documents/{docID}?type={1|2|5} (1=XBRL zip, 2=PDF, 5=CSV zip)
- 商用利用可 (公共データ利用規約準拠 §2.1) のため license_tag=commercial-ok
- Subscription-Key は資格情報のため、原本メタの URL・ログには含めない
- API はエラー時も HTTP 200 + JSON ボディを返すことがある（実レスポンスで確認済み）
  → 書類取得で Content-Type が application/json の場合はエラーとみなし保存しない
  （原本として保存するのは実データのみ §3）
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

from ..config import ConfigError, Settings
from ..http import FetchError, fetch
from ..licensing import LicenseTag
from ..models import JST, DisclosureRecord, Provenance, RawArtifact, Source, now_jst
from ..rawstore import save_raw
from .edinet_codelist import normalize_sec_code

logger = logging.getLogger(__name__)

EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"

# ------------------------------------------------------------------
# docTypeCode 定数（EDINET API 仕様の書類種別コード）
# ------------------------------------------------------------------
DOC_TYPE_ANNUAL_REPORT = "120"  # 有価証券報告書
DOC_TYPE_ANNUAL_REPORT_AMEND = "130"  # 訂正有価証券報告書
DOC_TYPE_QUARTERLY_REPORT = "140"  # 四半期報告書
DOC_TYPE_QUARTERLY_REPORT_AMEND = "150"  # 訂正四半期報告書
DOC_TYPE_SEMIANNUAL_REPORT = "160"  # 半期報告書
DOC_TYPE_SEMIANNUAL_REPORT_AMEND = "170"  # 訂正半期報告書
DOC_TYPE_LARGE_HOLDING = "350"  # 大量保有報告書
DOC_TYPE_LARGE_HOLDING_AMEND = "360"  # 訂正大量保有報告書

# 収集対象とする書類種別 (§4: 有報・四半期/半期報告・大量保有等)
TARGET_DOC_TYPE_CODES: frozenset[str] = frozenset(
    {
        DOC_TYPE_ANNUAL_REPORT,
        DOC_TYPE_ANNUAL_REPORT_AMEND,
        DOC_TYPE_QUARTERLY_REPORT,
        DOC_TYPE_QUARTERLY_REPORT_AMEND,
        DOC_TYPE_SEMIANNUAL_REPORT,
        DOC_TYPE_SEMIANNUAL_REPORT_AMEND,
        DOC_TYPE_LARGE_HOLDING,
        DOC_TYPE_LARGE_HOLDING_AMEND,
    }
)

# docTypeCode → ④開示書類DB「書類種別」select (§6.4) のマッピング。
# 半期報告書 (160/170) は「半期報告」。2024-04 に四半期報告書を置き換えた別の書類で、
# 「四半期報告」に混ぜると制度の前後で件数の意味が変わる。
# 未知のコードは「その他」。
DOC_TYPE_LABELS: dict[str, str] = {
    DOC_TYPE_ANNUAL_REPORT: "有報",
    DOC_TYPE_ANNUAL_REPORT_AMEND: "有報",
    DOC_TYPE_QUARTERLY_REPORT: "四半期報告",
    DOC_TYPE_QUARTERLY_REPORT_AMEND: "四半期報告",
    DOC_TYPE_SEMIANNUAL_REPORT: "半期報告",
    DOC_TYPE_SEMIANNUAL_REPORT_AMEND: "半期報告",
    DOC_TYPE_LARGE_HOLDING: "大量保有",
    DOC_TYPE_LARGE_HOLDING_AMEND: "大量保有",
}

# 書類取得 type → (⑤データ種別, 拡張子)
_DOC_FETCH_TYPES: dict[int, tuple[str, str]] = {
    1: ("xbrl", "zip"),
    2: ("pdf", "pdf"),
    5: ("csv", "zip"),
}


def doc_type_label(doc_type_code: str | None) -> str:
    """docTypeCode を ④「書類種別」select の値へ変換する。未知は「その他」。"""
    return DOC_TYPE_LABELS.get((doc_type_code or "").strip(), "その他")


def is_target_document(doc: dict) -> bool:
    """書類一覧の1件が収集対象の docTypeCode か。"""
    return doc.get("docTypeCode") in TARGET_DOC_TYPE_CODES


def has_sec_code(doc: dict) -> bool:
    """証券コード付き（secCode 非 null・非空）の書類か。"""
    sec = doc.get("secCode")
    return sec is not None and str(sec).strip() != ""


def issuer_edinet_code(doc: dict) -> str | None:
    """発行者の EDINET コード。大量保有報告書で対象会社を特定する唯一の手掛かり。

    350/360 は**保有者が提出する**ため `secCode` が入らない（実測で 992 件中
    956 件が空、`subjectEdinetCode` も None）。一方 `issuerEdinetCode` は
    956/956 = 100% 入っていた。
    """
    value = doc.get("issuerEdinetCode")
    text = str(value).strip() if value is not None else ""
    return text or None


def is_large_holding(doc: dict) -> bool:
    """大量保有報告書 (350) / 訂正大量保有報告書 (360) か。"""
    return doc.get("docTypeCode") in (DOC_TYPE_LARGE_HOLDING, DOC_TYPE_LARGE_HOLDING_AMEND)


def has_identifiable_company(doc: dict) -> bool:
    """対象会社を特定できる書類か。

    通常の書類は secCode で、大量保有報告書は issuerEdinetCode で特定する。
    これを secCode だけで判定していたため 350/360 がほぼ全て取りこぼされていた。
    """
    if has_sec_code(doc):
        return True
    return is_large_holding(doc) and issuer_edinet_code(doc) is not None


def _require_api_key(settings: Settings) -> str:
    if not settings.edinet_api_key:
        raise ConfigError("EDINET_API_KEY が未設定。EDINET API v2 には必須 (§12)")
    return settings.edinet_api_key


def list_documents(
    settings: Settings, target_date: date
) -> tuple[RawArtifact, list[dict]]:
    """指定日の書類一覧を取得し、原本保存して (RawArtifact, results) を返す (§8.1)。

    type=2 で提出書類一覧+メタデータを取得する。エラーレスポンス
    （metadata.status != "200" / results 欠落）は実データではないため
    保存せず FetchError とする (§3)。
    """
    key = _require_api_key(settings)
    datestr = target_date.isoformat()
    url = f"{EDINET_API_BASE}/documents.json"
    resp = fetch(url, params={"date": datestr, "type": 2, "Subscription-Key": key})
    try:
        body = json.loads(resp.content)
    except ValueError as exc:
        raise FetchError(f"EDINET 書類一覧が JSON でない: date={datestr}") from exc

    status = str(body.get("metadata", {}).get("status", body.get("StatusCode", "")))
    results = body.get("results")
    if status != "200" or not isinstance(results, list):
        # 例: キー不正時は {"StatusCode": 401, "message": ...}（実レスポンス確認済み）
        raise FetchError(
            f"EDINET 書類一覧エラーレスポンス: date={datestr} status={status} "
            f"message={body.get('message', '')!r}"
        )

    artifact = save_raw(
        resp.content,
        source=Source.EDINET,
        datatype="documents_list",
        scope="ALL",
        data_date=target_date,
        url=f"{url}?date={datestr}&type=2",  # Subscription-Key は記録しない
        ext="json",
        license_tag=LicenseTag.COMMERCIAL_OK,
        base_dir=settings.raw_data_dir,
    )
    return artifact, results


def fetch_document(
    settings: Settings,
    doc_id: str,
    doc_type: int,
    *,
    code: str | None = None,
    data_date: date | None = None,
) -> RawArtifact:
    """書類本体（1=XBRL zip / 2=PDF / 5=CSV zip）を取得し原本保存する。

    - scope は銘柄4桁（code 指定時）、無ければ docID
    - Content-Type が application/json の場合は API のエラーレスポンスのため
      保存せず FetchError（実データのみ保存 §3）
    """
    key = _require_api_key(settings)
    if doc_type not in _DOC_FETCH_TYPES:
        raise ValueError(f"未知の書類取得 type={doc_type} (有効: {sorted(_DOC_FETCH_TYPES)})")
    datatype, ext = _DOC_FETCH_TYPES[doc_type]
    url = f"{EDINET_API_BASE}/documents/{doc_id}"
    resp = fetch(url, params={"type": doc_type, "Subscription-Key": key})

    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "application/json" in content_type:
        # 書類本体ではなくエラー JSON（例: 404 相当の {"StatusCode": ...}）
        raise FetchError(
            f"EDINET 書類取得エラーレスポンス: docID={doc_id} type={doc_type} "
            f"body={resp.content[:200]!r}"
        )
    # マジックバイト検証 (CONTRACTS): 200 で返るエラー/メンテHTML等を原本化しない (§3)
    magic = b"%PDF" if doc_type == 2 else b"PK\x03\x04"
    if not resp.content.startswith(magic):
        raise FetchError(
            f"EDINET 書類本文が {ext} 形式でない (エラーページ?): "
            f"docID={doc_id} type={doc_type} head={resp.content[:16]!r}"
        )

    return save_raw(
        resp.content,
        source=Source.EDINET,
        datatype=datatype,
        scope=normalize_sec_code(code) or doc_id,
        data_date=data_date,
        url=f"{url}?type={doc_type}",  # Subscription-Key は記録しない
        ext=ext,
        license_tag=LicenseTag.COMMERCIAL_OK,
        base_dir=settings.raw_data_dir,
        doc_id=doc_id,
    )


def _parse_submit_datetime(value: str) -> datetime:
    """submitDateTime ("YYYY-MM-DD hh:mm[:ss]") を JST の datetime にする。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    raise ValueError(f"submitDateTime を解釈できない: {value!r}")


def to_disclosure_record(doc: dict, *, raw_page_id: str | None = None) -> DisclosureRecord:
    """書類一覧の1件 (results[i]) を ④開示書類 DisclosureRecord に変換する。

    - キー=docID (§8.1-6) / secCode は4桁化 / has_xbrl=xbrlFlag
    - 値の補完はしない: 無い項目は None のまま (§3-1)
    """
    doc_id = str(doc.get("docID") or "").strip()
    if not doc_id:
        raise ValueError(f"docID の無い書類は変換できない: {doc!r}")
    submit_raw = str(doc.get("submitDateTime") or "").strip()
    if not submit_raw:
        raise ValueError(f"submitDateTime の無い書類は変換できない: docID={doc_id}")
    disclosed_at = _parse_submit_datetime(submit_raw)

    return DisclosureRecord(
        doc_id=doc_id,
        title=str(doc.get("docDescription") or "").strip(),
        disclosed_at=disclosed_at,
        code=normalize_sec_code(doc.get("secCode")),
        doc_type=doc_type_label(doc.get("docTypeCode")),
        source_url=f"{EDINET_API_BASE}/documents/{doc_id}",
        has_xbrl=str(doc.get("xbrlFlag") or "") == "1",
        provenance=Provenance(
            source=Source.EDINET,
            license_tag=LicenseTag.COMMERCIAL_OK,
            data_date=disclosed_at.date(),
            fetched_at=now_jst(),
            raw_page_id=raw_page_id,
        ),
    )
