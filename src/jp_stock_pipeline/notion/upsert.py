"""Record dataclass → Notion プロパティ payload 構築と冪等 upsert (DESIGN.md §8.1-6)。

不変条件:
- None の値は**明示的な空値**として payload に含める（{"number": None} 等）。
  これにより update 時に前回値が残存せず、行は常に最新レコードの完全な姿になる
  （§3-1 欠損は欠損として表示 / §3-3 行の来歴と値が常に同一ソース由来 /
  §2.2 ライセンスタグと値の不整合＝personal-only 汚染の防止）
- 行は「単一ソースのスナップショット」を表す完全置換セマンティクス。
  別ソースが同一キー行を更新する場合は値・来歴・タグがまとめて置き換わる
- 全行に Provenance (ソース/ライセンスタグ/データ基準日/取得日時/原本relation/
  データ品質) を設定する (§3-3, §6.3)
- upsert は冪等: キー検索 → update or create (§8.1-6)
  キー = 銘柄コード(①②) / 銘柄コード×決算期末×開示種別(③) / docID(④) / データセット名(⑥)
- 原本 page_id が dry-run 合成ID ("dry-run-" 始まり) の場合は relation を設定せず
  スキップする (本番DBへテストデータ・無効IDを書かない §3-6)

プロパティ名は schema.py の定数を唯一の正として import する。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime

from ..config import Settings
from ..models import (
    DisclosureRecord,
    FinancialSummaryRecord,
    PriceTechnicalRecord,
    Provenance,
    StockMasterRecord,
    now_jst,
)
from . import schema as S
from .client import NotionClient

logger = logging.getLogger(__name__)

# dry-run 合成IDの接頭辞 (client.py の _record() が生成する)
DRY_RUN_ID_PREFIX = "dry-run-"

# Notion rich_text 1要素の上限は2000文字
_TEXT_LIMIT = 2000


# ---------------------------------------------------------------------------
# プロパティ値 payload ビルダー (file_upload.py からも import される)
# ---------------------------------------------------------------------------


def _clip(value: str) -> str:
    """rich_text 上限超過時の切り詰め (値の改変ではなく表示上の制約 §6.1)。"""
    if len(value) <= _TEXT_LIMIT:
        return value
    return value[: _TEXT_LIMIT - 10] + "…(省略)"


def title_prop(value: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": _clip(str(value))}}]}


def text_prop(value: str | None) -> dict:
    """None は空 rich_text（update 時に前回値をクリアする §3-1）。"""
    if value is None:
        return {"rich_text": []}
    return {"rich_text": [{"type": "text", "text": {"content": _clip(str(value))}}]}


def number_prop(value: float | int | None) -> dict:
    return {"number": value}


def select_prop(value: str | None) -> dict:
    return {"select": {"name": str(value)} if value is not None else None}


def date_prop(value: date | datetime | None) -> dict:
    return {"date": {"start": value.isoformat()} if value is not None else None}


def url_prop(value: str | None) -> dict:
    return {"url": str(value) if value is not None else None}


def checkbox_prop(value: bool) -> dict:
    return {"checkbox": bool(value)}


def relation_prop(page_ids: Iterable[str]) -> dict:
    return {"relation": [{"id": pid} for pid in page_ids]}


def files_prop(uploads: Iterable[tuple[str, str]]) -> dict:
    """File Upload API でアップロード済みの (file_upload_id, ファイル名) を添付する。"""
    return {
        "files": [
            {"type": "file_upload", "file_upload": {"id": fid}, "name": _clip(name)}
            for fid, name in uploads
        ]
    }


def real_page_id(page_id: str | None) -> str | None:
    """dry-run 合成IDなら None を返す (本番relationに無効IDを書かない)。"""
    if page_id and not page_id.startswith(DRY_RUN_ID_PREFIX):
        return page_id
    return None


def _set(props: dict, name: str, builder, value) -> None:
    """None も明示的な空値として送信する (update 時の前回値残存防止 §3-1/§2.2)。"""
    props[name] = builder(value)


# ---------------------------------------------------------------------------
# Provenance → 共通プロパティ (§6.3)
# ---------------------------------------------------------------------------


def provenance_properties(
    prov: Provenance, *, include_raw_relation: bool = True, include_quality: bool = True
) -> dict:
    """共通プロパティ payload。⑤向けには raw relation / 品質を含めない (§6.3)。

    原本 page_id が dry-run 合成IDの場合は relation を設定せずスキップする。
    """
    props: dict = {
        S.PROP_SOURCE: select_prop(prov.source.value),
        S.PROP_LICENSE_TAG: select_prop(prov.license_tag.value),
        S.PROP_FETCHED_AT: date_prop(prov.fetched_at),
    }
    _set(props, S.PROP_DATA_DATE, date_prop, prov.data_date)
    if include_quality:
        props[S.PROP_QUALITY] = select_prop(prov.quality.value)
    if include_raw_relation:
        raw_id = real_page_id(prov.raw_page_id)
        if raw_id:
            props[S.PROP_RAW_RELATION] = relation_prop([raw_id])
    return props


# ---------------------------------------------------------------------------
# Record → プロパティ payload (§6.4)
# ---------------------------------------------------------------------------


def stock_master_properties(
    record: StockMasterRecord, *, include_lifecycle: bool = True
) -> dict:
    """① 銘柄マスタ。None 項目は明示的な空値で送信し前回値をクリア (§3-1 完全置換)。

    include_lifecycle=False のとき 状態/上場日/上場廃止日 を payload に含めない。
    これらは「開示イベント (apply_disclosure_lifecycle) とコードリスト消失
    (mark_master_absent_from_codelist) が所有する」フィールドであり、月次の
    codelist 同期 (master_sync) が上書き・消去してはならない (§ Phase3 二重所有の回避)。
    名称/市場/業種/EDINETコード/listed は codelist 所有なので常に完全置換する。
    """
    props = {
        S.MASTER_PROP_NAME: title_prop(record.name),
        S.MASTER_PROP_CODE: text_prop(record.code),
        S.MASTER_PROP_LISTED: checkbox_prop(record.listed),
        S.MASTER_PROP_LAST_UPDATED: date_prop(record.provenance.fetched_at),
        **provenance_properties(record.provenance),
    }
    _set(props, S.MASTER_PROP_MARKET, select_prop, record.market)
    _set(props, S.MASTER_PROP_SECTOR33, select_prop, record.sector33)
    _set(props, S.MASTER_PROP_SECTOR17, select_prop, record.sector17)
    _set(props, S.MASTER_PROP_EDINET_CODE, text_prop, record.edinet_code)
    if include_lifecycle:
        _set(props, S.MASTER_PROP_STATUS, select_prop, record.status)
        _set(props, S.MASTER_PROP_LISTING_DATE, date_prop, record.listing_date)
        _set(props, S.MASTER_PROP_DELISTING_DATE, date_prop, record.delisting_date)
    return props


# PriceTechnicalRecord のフィールド名 → ② プロパティ名 (すべて number)
_PRICE_FIELD_TO_PROP: dict[str, str] = {
    "open": S.PRICE_PROP_OPEN,
    "high": S.PRICE_PROP_HIGH,
    "low": S.PRICE_PROP_LOW,
    "close": S.PRICE_PROP_CLOSE,
    "prev_close_pct": S.PRICE_PROP_PREV_PCT,
    "volume": S.PRICE_PROP_VOLUME,
    "turnover": S.PRICE_PROP_TURNOVER,
    "market_cap": S.PRICE_PROP_MARKET_CAP,
    "week52_high": S.PRICE_PROP_W52_HIGH,
    "week52_low": S.PRICE_PROP_W52_LOW,
    "sma5": S.PRICE_PROP_SMA5,
    "sma25": S.PRICE_PROP_SMA25,
    "sma75": S.PRICE_PROP_SMA75,
    "sma200": S.PRICE_PROP_SMA200,
    "sma25_dev_pct": S.PRICE_PROP_SMA25_DEV,
    "rsi14": S.PRICE_PROP_RSI14,
    "macd": S.PRICE_PROP_MACD,
    "macd_signal": S.PRICE_PROP_MACD_SIGNAL,
    "macd_hist": S.PRICE_PROP_MACD_HIST,
    "bb_upper": S.PRICE_PROP_BB_UPPER,
    "bb_lower": S.PRICE_PROP_BB_LOWER,
    "atr14": S.PRICE_PROP_ATR14,
    "volume_ratio25": S.PRICE_PROP_VOL_RATIO25,
    "per": S.PRICE_PROP_PER,
    "pbr": S.PRICE_PROP_PBR,
    "dividend_yield_pct": S.PRICE_PROP_DIV_YIELD,
}


def price_technical_properties(
    record: PriceTechnicalRecord,
    master_page_id: str | None = None,
    extra_raw_page_ids: Iterable[str] | None = None,
) -> dict:
    """② 株価テクニカル。取得できなかった指標は空欄 (§3-1)。

    extra_raw_page_ids: 価格原本に加えて紐付ける ⑤ 行 (例: バリュエーション原本)。
    """
    props = {
        S.PRICE_PROP_CODE: title_prop(record.code),
        **provenance_properties(record.provenance),
    }
    for field_name, prop_name in _PRICE_FIELD_TO_PROP.items():
        _set(props, prop_name, number_prop, getattr(record, field_name))
    extra_ids = [rid for rid in (extra_raw_page_ids or []) if real_page_id(rid)]
    if extra_ids:
        existing = props.get(S.PROP_RAW_RELATION, {"relation": []})["relation"]
        ids = [r["id"] for r in existing] + extra_ids
        props[S.PROP_RAW_RELATION] = relation_prop(dict.fromkeys(ids))
    master_id = real_page_id(master_page_id)
    if master_id:
        props[S.PROP_MASTER_RELATION] = relation_prop([master_id])
    return props


# FinancialSummaryRecord のフィールド名 → ③ プロパティ名 (number)
_FIN_FIELD_TO_PROP: dict[str, str] = {
    "net_sales": S.FIN_PROP_NET_SALES,
    "operating_income": S.FIN_PROP_OPERATING_INCOME,
    "ordinary_income": S.FIN_PROP_ORDINARY_INCOME,
    "net_income": S.FIN_PROP_NET_INCOME,
    "eps": S.FIN_PROP_EPS,
    "bps": S.FIN_PROP_BPS,
    "roe_pct": S.FIN_PROP_ROE,
    "roa_pct": S.FIN_PROP_ROA,
    "equity_ratio_pct": S.FIN_PROP_EQUITY_RATIO,
    "cf_operating": S.FIN_PROP_CF_OPERATING,
    "cf_investing": S.FIN_PROP_CF_INVESTING,
    "cf_financing": S.FIN_PROP_CF_FINANCING,
    "dps_actual": S.FIN_PROP_DPS_ACTUAL,
    "dps_forecast": S.FIN_PROP_DPS_FORECAST,
    "forecast_net_sales": S.FIN_PROP_FC_NET_SALES,
    "forecast_operating_income": S.FIN_PROP_FC_OPERATING_INCOME,
    "forecast_ordinary_income": S.FIN_PROP_FC_ORDINARY_INCOME,
    "forecast_net_income": S.FIN_PROP_FC_NET_INCOME,
    "forecast_eps": S.FIN_PROP_FC_EPS,
}


def financial_summary_title(record: FinancialSummaryRecord) -> str:
    """③ タイトル規則 (§6.4 例: 7203 2026/03期 本決算)。"""
    pe = record.fiscal_period_end
    return f"{record.code} {pe.year}/{pe.month:02d}期 {record.disclosure_type}"


def financial_summary_properties(
    record: FinancialSummaryRecord, master_page_id: str | None = None
) -> dict:
    """③ 財務サマリ。None 項目は明示的な空値で送信し前回値をクリア (§3-1 完全置換)。"""
    props = {
        S.FIN_PROP_TITLE: title_prop(financial_summary_title(record)),
        S.FIN_PROP_CODE: text_prop(record.code),
        S.FIN_PROP_PERIOD_END: date_prop(record.fiscal_period_end),
        S.FIN_PROP_DISCLOSURE_TYPE: select_prop(record.disclosure_type),
        **provenance_properties(record.provenance),
    }
    _set(props, S.FIN_PROP_CONSOLIDATED, select_prop, record.consolidated)
    _set(props, S.FIN_PROP_STANDARD, select_prop, record.accounting_standard)
    for field_name, prop_name in _FIN_FIELD_TO_PROP.items():
        _set(props, prop_name, number_prop, getattr(record, field_name))
    _set(props, S.FIN_PROP_DISCLOSED_AT, date_prop, record.disclosed_at)
    master_id = real_page_id(master_page_id)
    if master_id:
        props[S.PROP_MASTER_RELATION] = relation_prop([master_id])
    return props


def disclosure_properties(
    record: DisclosureRecord, master_page_id: str | None = None
) -> dict:
    """④ 開示書類。None 項目は明示的な空値で送信し前回値をクリア (§3-1 完全置換)。"""
    props = {
        S.DISC_PROP_TITLE: title_prop(record.title),
        S.DISC_PROP_DISCLOSED_AT: date_prop(record.disclosed_at),
        S.DISC_PROP_DOC_TYPE: select_prop(record.doc_type),
        S.DISC_PROP_DOC_ID: text_prop(record.doc_id),
        S.DISC_PROP_HAS_XBRL: checkbox_prop(record.has_xbrl),
        **provenance_properties(record.provenance),
    }
    _set(props, S.DISC_PROP_CODE, text_prop, record.code)
    _set(props, S.DISC_PROP_URL, url_prop, record.source_url)
    _set(props, S.DISC_PROP_SPLIT_RATIO, text_prop, record.split_ratio)
    _set(props, S.DISC_PROP_SPLIT_FACTOR, number_prop, record.split_factor)
    _set(props, S.DISC_PROP_EFFECTIVE_DATE, date_prop, record.effective_date)
    master_id = real_page_id(master_page_id)
    if master_id:
        props[S.PROP_MASTER_RELATION] = relation_prop([master_id])
    return props


# ---------------------------------------------------------------------------
# キー filter (§8.1-6 冪等キー)
# ---------------------------------------------------------------------------


def stock_master_filter(code: str) -> dict:
    """① キー = 銘柄コード (rich_text equals)。"""
    return {"property": S.MASTER_PROP_CODE, "rich_text": {"equals": code}}


def price_technical_filter(code: str) -> dict:
    """② キー = 銘柄コード (title equals)。"""
    return {"property": S.PRICE_PROP_CODE, "title": {"equals": code}}


def financial_summary_filter(
    code: str, fiscal_period_end: date, disclosure_type: str
) -> dict:
    """③ 複合キー = 銘柄コード×決算期末×開示種別 の and フィルタ。"""
    return {
        "and": [
            {"property": S.FIN_PROP_CODE, "rich_text": {"equals": code}},
            {"property": S.FIN_PROP_PERIOD_END, "date": {"equals": fiscal_period_end.isoformat()}},
            {"property": S.FIN_PROP_DISCLOSURE_TYPE, "select": {"equals": disclosure_type}},
        ]
    }


def disclosure_filter(doc_id: str) -> dict:
    """④ キー = 書類管理番号 (docID)。"""
    return {"property": S.DISC_PROP_DOC_ID, "rich_text": {"equals": doc_id}}


def export_filter(dataset_name: str) -> dict:
    """⑥ キー = データセット名 (title equals)。"""
    return {"property": S.EXPORT_PROP_NAME, "title": {"equals": dataset_name}}


# ---------------------------------------------------------------------------
# 冪等 upsert (§8.1-6: キー検索 → update or create)
# ---------------------------------------------------------------------------


def _find_page(client: NotionClient, db_id: str, flt: dict) -> str | None:
    results = client.query_database(db_id, filter=flt, page_size=1, max_pages=1)
    return results[0]["id"] if results else None


def _upsert(client: NotionClient, db_id: str, flt: dict, props: dict) -> str:
    page_id = _find_page(client, db_id, flt)
    if page_id:
        client.update_page(page_id, props)
        return page_id
    return client.create_page(parent={"database_id": db_id}, properties=props)["id"]


def find_stock_master_page(client: NotionClient, settings: Settings, code: str) -> str | None:
    """① から銘柄コードで page_id を引く (②③④の relation 設定用ヘルパー)。"""
    return _find_page(client, settings.db_id("stock_master"), stock_master_filter(code))


def load_stock_master_map(client: NotionClient, settings: Settings) -> dict[str, str]:
    """① 全行の {銘柄コード: page_id} を一括取得する。

    全銘柄ループでの find_stock_master_page (1req/銘柄) を置き換え、
    §8.3 のレート試算 (②upsert=2req/銘柄) に収める。
    """
    pages = client.query_database(settings.db_id("stock_master"))
    out: dict[str, str] = {}
    for page in pages:
        rich = page.get("properties", {}).get(S.MASTER_PROP_CODE, {}).get("rich_text", [])
        if rich:
            code = rich[0].get("plain_text", "").strip()
            if code:
                out[code] = page["id"]
    return out


def upsert_stock_master(
    client: NotionClient,
    settings: Settings,
    record: StockMasterRecord,
    *,
    include_lifecycle: bool = True,
) -> str:
    """① 銘柄マスタへ冪等 upsert (キー=銘柄コード)。page_id を返す。

    master_sync(codelist 同期) は include_lifecycle=False で呼び、開示・消失が
    所有する 状態/上場日/上場廃止日 を上書きしない (§ Phase3 二重所有の回避)。
    """
    return _upsert(
        client,
        settings.db_id("stock_master"),
        stock_master_filter(record.code),
        stock_master_properties(record, include_lifecycle=include_lifecycle),
    )


# ① の状態 select 値 (schema.LISTING_STATUS_OPTIONS と一致)
STATUS_LISTED = "上場"
STATUS_DELISTED = "上場廃止"
# 一次開示で ① ライフサイクルを更新する「書類種別」(DOC_TYPES の値。状態名前空間
# とは独立。文字列の一致に依存せず doc_type として明示する § namespace 分離)
LIFECYCLE_DOC_TYPES: frozenset[str] = frozenset({"上場廃止", "新規上場"})


def mark_master_absent_from_codelist(
    client: NotionClient, settings: Settings, page_id: str
) -> None:
    """① 行を「EDINET上場リストから消えた」= listed=False にする。

    コードリストからの消失は listed=False（取得停止）の確定トリガ (§ Phase3)。
    ただし「状態=上場廃止」の確定はコードリストのヒューリスティックでは行わず、
    一次開示 (apply_disclosure_lifecycle) に一本化する: EDINET コードリストの
    一時的な揺らぎ（行スキップ・提出者要件の一時割れ等）で個別銘柄が誤検知された
    場合に、「上場廃止」という誤った権威的状態を ① へ書き込まない (§3-1 誤った
    権威的値は欠損より悪い / §3-7 状態は一次開示由来)。これにより listed=True かつ
    状態=上場廃止 という矛盾行も生じない（状態の所有は disclosure 側に一本化）。
    再上場時は次回 master_sync の upsert が listed=True へ自己修復する。
    部分更新（listed のみ。状態を含む他フィールドは直前の値を保持）。
    """
    client.update_page(page_id, {S.MASTER_PROP_LISTED: checkbox_prop(False)})


def apply_disclosure_lifecycle(
    client: NotionClient, settings: Settings, record: DisclosureRecord
) -> str | None:
    """上場廃止/新規上場 開示を ① のライフサイクル状態へ反映する (§ Phase3)。

    - 上場廃止(発表): 状態=上場廃止 / 上場廃止日=effective_date(判明時のみ)。
      **listed は触らない**: 効力発生まで売買は継続するため取得も継続する
      (§3-1 取得可能なデータを自動で止めない)。確定的な listed=False は
      コードリスト消失 (mark_master_absent_from_codelist) が担う。
    - 新規上場: 状態=上場 / 上場日=effective_date(判明時のみ)。
      listed は codelist 同期が所有するため触らない。

    開示日(発表日) ≠ 効力発生日のため、日付は effective_date がタイトルから取れた
    場合のみ書き、発表日を流用しない (§3-1)。**効力発生日が取れないときは日付キーを
    payload に含めない**（真の部分更新）: 先行開示で取り込んだ確定日付を、効力発生日を
    持たない後続の同種開示（例「上場廃止後の取り扱いに関するお知らせ」）が {date:None}
    で黙って消去しないようにする。状態(select)は確定値なので常に設定する。
    ① に該当銘柄が無ければ何もしない (None を返す)。部分更新。
    """
    if record.doc_type not in LIFECYCLE_DOC_TYPES or not record.code:
        return None
    page_id = find_stock_master_page(client, settings, record.code)
    if not page_id:
        return None
    if record.doc_type == STATUS_DELISTED:
        props = {S.MASTER_PROP_STATUS: select_prop(STATUS_DELISTED)}
        date_prop_name = S.MASTER_PROP_DELISTING_DATE
    else:  # 新規上場
        props = {S.MASTER_PROP_STATUS: select_prop(STATUS_LISTED)}
        date_prop_name = S.MASTER_PROP_LISTING_DATE
    # 効力発生日は取れた時のみ書く（None で既存の確定日付を上書き消去しない）
    if record.effective_date is not None:
        props[date_prop_name] = date_prop(record.effective_date)
    client.update_page(page_id, props)
    return page_id


def upsert_price_technical(
    client: NotionClient,
    settings: Settings,
    record: PriceTechnicalRecord,
    master_page_id: str | None = None,
    extra_raw_page_ids: Iterable[str] | None = None,
) -> str:
    """② 株価テクニカルへ冪等 upsert (キー=銘柄コード title equals)。"""
    return _upsert(
        client,
        settings.db_id("prices"),
        price_technical_filter(record.code),
        price_technical_properties(record, master_page_id, extra_raw_page_ids),
    )


def upsert_financial_summary(
    client: NotionClient,
    settings: Settings,
    record: FinancialSummaryRecord,
    master_page_id: str | None = None,
) -> str:
    """③ 財務サマリへ冪等 upsert (複合キー=銘柄コード×決算期末×開示種別)。"""
    return _upsert(
        client,
        settings.db_id("financials"),
        financial_summary_filter(record.code, record.fiscal_period_end, record.disclosure_type),
        financial_summary_properties(record, master_page_id),
    )


def upsert_disclosure(
    client: NotionClient,
    settings: Settings,
    record: DisclosureRecord,
    master_page_id: str | None = None,
) -> str:
    """④ 開示書類へ冪等 upsert (キー=書類管理番号 docID)。"""
    return _upsert(
        client,
        settings.db_id("disclosures"),
        disclosure_filter(record.doc_id),
        disclosure_properties(record, master_page_id),
    )


def write_job_log(
    client: NotionClient,
    settings: Settings,
    job_name: str,
    status: str,
    processed: int,
    failed: int,
    failed_codes: Iterable[str] | None = None,
    run_url: str | None = None,
    duration_secs: float | None = None,
) -> str:
    """⑦ 収集ジョブログへ1行作成 (§8.1-7)。ステータスは 成功/一部失敗/失敗。"""
    if status not in S.JOB_STATUSES:
        raise ValueError(f"不正なステータス: {status} (有効: {S.JOB_STATUSES})")
    props = {
        S.JOB_PROP_NAME: title_prop(job_name),
        S.JOB_PROP_RUN_AT: date_prop(now_jst()),
        S.JOB_PROP_STATUS: select_prop(status),
        S.JOB_PROP_PROCESSED: number_prop(processed),
        S.JOB_PROP_FAILED: number_prop(failed),
    }
    codes = ", ".join(failed_codes) if failed_codes else ""
    _set(props, S.JOB_PROP_FAILED_CODES, text_prop, codes or None)
    _set(props, S.JOB_PROP_RUN_URL, url_prop, run_url)
    _set(props, S.JOB_PROP_DURATION, number_prop, duration_secs)
    return client.create_page(
        parent={"database_id": settings.db_id("job_log")}, properties=props
    )["id"]


def create_export_row(
    client: NotionClient,
    settings: Settings,
    dataset_name: str,
    period: str | None,
    row_count: int | None,
    schema_desc: str | None,
    provenance: Provenance | None = None,
    file_uploads: Iterable[tuple[str, str]] | None = None,
    updated_on: date | None = None,
) -> str:
    """⑥ 時系列エクスポートへ冪等に作成/更新 (キー=データセット名)。

    file_uploads は (file_upload_id, ファイル名) のリスト
    (file_upload.py でアップロード済みのもの)。更新日は実際の更新時刻
    (now_jst) を既定とする — 運用メタデータであり推定値ではない。
    """
    props: dict = {
        S.EXPORT_PROP_NAME: title_prop(dataset_name),
        S.EXPORT_PROP_UPDATED_ON: date_prop(updated_on or now_jst().date()),
    }
    _set(props, S.EXPORT_PROP_PERIOD, text_prop, period)
    _set(props, S.EXPORT_PROP_ROW_COUNT, number_prop, row_count)
    _set(props, S.EXPORT_PROP_SCHEMA_DESC, text_prop, schema_desc)
    if file_uploads:
        props[S.EXPORT_PROP_FILES] = files_prop(file_uploads)
    if provenance is not None:
        props.update(provenance_properties(provenance))
    return _upsert(client, settings.db_id("exports"), export_filter(dataset_name), props)
