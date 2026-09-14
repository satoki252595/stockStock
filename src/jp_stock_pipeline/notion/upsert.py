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
  キー = 銘柄コード(①) / 銘柄コード×決算期末×開示種別(③) / docID(④)
- 原本 page_id が dry-run 合成ID ("dry-run-" 始まり) の場合は relation を設定せず
  スキップする (本番DBへテストデータ・無効IDを書かない §3-6)

プロパティ名は schema.py の定数を唯一の正として import する。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from ..config import Settings
from ..models import (
    JST,
    DisclosureRecord,
    FinancialSummaryRecord,
    Provenance,
    StockMasterRecord,
)
from notion_client.errors import APIResponseError

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
    現行履歴DB ID / 履歴シャード番号 / 履歴行数（本番DBに残る廃止済み列）は
    この payload に含めない（月次同期で既存値を消さない）。
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


# ③ の旧開示種別。書き込み前のキー検索で、ラベル変更前の行も拾うために使う。
# 決算期末 >= 2024-06-30 の第2四半期は、以前は「2Q」で書いていた
# (transform/normalize.interim_disclosure_type)。既存行の移行 (2026-09 実施済み)
# より先にラベル変更が本番に出ても、書き手が旧行を採用して「中間」へ書き直すので、
# 同じ期が 2 行に割れない。
# 期末 >= 2024-06-30 の「2Q」は新しいコードでは作られないので、移行後も害は無い。
# normalize を import しないのは、upsert を pandas に依存させないため。
LEGACY_DISCLOSURE_TYPES: dict[str, tuple[str, ...]] = {"中間": ("2Q",)}


def _select_equals(prop: str, value: str) -> dict:
    return {"property": prop, "select": {"equals": value}}


def _consolidated_condition(consolidated: str | None) -> dict:
    """連結単体の条件。判定できなかったレコード (None) は「空」の行をキーにする。"""
    if consolidated:
        return _select_equals(S.FIN_PROP_CONSOLIDATED, consolidated)
    return {"property": S.FIN_PROP_CONSOLIDATED, "select": {"is_empty": True}}


def financial_summary_filter(
    code: str, fiscal_period_end: date, disclosure_type: str, consolidated: str | None
) -> dict:
    """③ 複合キー = 銘柄コード×決算期末×開示種別×連結単体 の and フィルタ。

    連結単体をキーに含めるのは、同じ期末・同じ開示種別の連結と単体を後勝ちで
    潰さないため（D1 `jss_financials` は PR #39 で PK に足した）。判定できなかった
    レコード (None) は「連結単体が空」の行をキーにする。D1 の '不明' に当たるが、
    Notion の select に選択肢を増やさず空で表す。
    """
    return {
        "and": [
            {"property": S.FIN_PROP_CODE, "rich_text": {"equals": code}},
            {"property": S.FIN_PROP_PERIOD_END, "date": {"equals": fiscal_period_end.isoformat()}},
            _select_equals(S.FIN_PROP_DISCLOSURE_TYPE, disclosure_type),
            _consolidated_condition(consolidated),
        ]
    }


def financial_summary_lookup_filter(
    code: str, fiscal_period_end: date, disclosure_type: str, consolidated: str | None
) -> dict:
    """③ の書き込み前のキー検索。正確なキーの行と、採用してよい旧キーの行を 1 回で引く。

    旧キーの行は 2 種類ある:
    - 連結単体が空の行: キーに連結単体が無かった頃の行。判定できたレコードが採用する。
    - 旧開示種別の行: LEGACY_DISCLOSURE_TYPES（「中間」に対する「2Q」）。
    どれに書くかは pick_financial_page が決める。リクエストは従来どおり 1 回で済む
    （Notion の複合フィルタは and → or → 条件 の 2 段までネストできる）。

    採用しなかった案: 正確なキーで検索して無ければ旧キーで再検索する。作成のたびに
    リクエストが 1 回増えるので採らない。
    """
    types = (disclosure_type, *LEGACY_DISCLOSURE_TYPES.get(disclosure_type, ()))
    if len(types) == 1:
        type_condition = _select_equals(S.FIN_PROP_DISCLOSURE_TYPE, disclosure_type)
    else:
        type_condition = {
            "or": [_select_equals(S.FIN_PROP_DISCLOSURE_TYPE, t) for t in types]
        }
    if consolidated:
        consolidated_condition = {
            "or": [_consolidated_condition(consolidated), _consolidated_condition(None)]
        }
    else:
        consolidated_condition = _consolidated_condition(None)
    return {
        "and": [
            {"property": S.FIN_PROP_CODE, "rich_text": {"equals": code}},
            {"property": S.FIN_PROP_PERIOD_END, "date": {"equals": fiscal_period_end.isoformat()}},
            type_condition,
            consolidated_condition,
        ]
    }


def disclosure_filter(doc_id: str) -> dict:
    """④ キー = 書類管理番号 (docID)。"""
    return {"property": S.DISC_PROP_DOC_ID, "rich_text": {"equals": doc_id}}


# ---------------------------------------------------------------------------
# 冪等 upsert (§8.1-6: キー検索 → update or create)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 同じキーの重複ページの扱い (#13)
#
# Notion の DB には一意制約が無い。別プロセスが同時に「未存在」を読むと、それぞれが
# create して同じキーのページが 2 つできる（Actions の concurrency はジョブ別で、
# edinet_daily と tdnet_hourly は ③ のキーを共有する。手動 CLI や別ホストも排他しない）。
# 共有ロック（concurrency group の共有）は採らない: GitHub は同じ group の保留中の
# 実行を後から来た実行で取り消すので、1 日 1 回の edinet_daily が毎時の tdnet_hourly に
# 取り消されうる。
#
# 代わりに次の 2 つで「重複ができても 1 つに収束する」ようにする。
# 1. 正のページを決め打つ: キー検索は created_time 昇順で取り、最古を正とする。
#    Notion の created_time は分単位に丸められるので、同じ時刻なら page id の辞書順で
#    最小のものを正とする。すべての書き手が同じ規則で選ぶので、既に重複があっても
#    更新先が書き手ごとに割れない。
# 2. 作成直後に 1 回だけ再確認する: 自分が create したときだけ同じキーで再検索し、
#    自分より正しいページがあれば、内容をそちらへ書き直してから自分が作ったページだけを
#    archive する（復元可能）。呼び出し前から存在したページは archive しない。
#    書き直しや archive に失敗しても例外にしない（重複が残るだけで、データは失わない）。
# ---------------------------------------------------------------------------

# キー検索の並び順。最古のページを先頭に置く。
KEY_QUERY_SORTS: list[dict] = [{"timestamp": "created_time", "direction": "ascending"}]
# キー検索 1 回で取る件数。同じ分に作られた重複を並べて page id で決め打つために
# 複数件取る（リクエスト回数は 1 回のまま。重複が無ければ返るのは 1 件）。
KEY_QUERY_PAGE_SIZE = 10


@dataclass(frozen=True)
class UpsertOutcome:
    """_upsert_outcome の結果。重複の収束に関する情報を呼び出し側とテストへ返す (#13)。"""

    page_id: str  # 以後使う page_id（正のページ）
    created: bool = False  # この呼び出しで create したか
    created_page_id: str | None = None  # create したページ（archive されていても入る）
    duplicate_found: bool = False  # 自分より正しい同じキーのページが見つかった
    rewritten: bool = False  # 正のページへ内容を書き直した
    archived: bool = False  # 自分が作ったページを archive した
    warning: str | None = None  # 収束できなかった理由（重複が残っている）


def _page_order_key(page: dict) -> tuple[str, str] | None:
    """正のページを決める並びのキー (created_time, 正規化した page id)。

    created_time を持たない（テストダブル等）なら None。Notion は常に返す。
    created_time は Notion が同じ書式 (ISO 8601, UTC) で返すので文字列で比べられる。
    """
    created = page.get("created_time")
    page_id = page.get("id")
    if not created or not page_id:
        return None
    return (str(created), _normalize_page_id(page_id))


def _normalize_page_id(page_id: str) -> str:
    return str(page_id).replace("-", "").lower()


def _is_older(page: dict, other: dict) -> bool:
    """page が other より正しい（古い）か。どちらかに並びのキーが無ければ True。

    キーが無いときに True を返すのは、事前マップで従来どおり「後から来た行」を
    採るため（created_time を返さないテストダブルでの挙動を変えない）。
    """
    page_key = _page_order_key(page)
    other_key = _page_order_key(other)
    if page_key is None or other_key is None:
        return True
    return page_key < other_key


def oldest_page(pages: list[dict]) -> dict | None:
    """同じキーのページ群から正のページ（最古。同じ時刻なら page id が最小）を選ぶ。

    どれかが created_time を持たなければ並びを判断できないので先頭を返す
    （クエリは created_time 昇順で頼んでいるので、先頭が最古の候補）。
    """
    if not pages:
        return None
    keys = [_page_order_key(page) for page in pages]
    if any(key is None for key in keys):
        return pages[0]
    best = min(range(len(pages)), key=lambda i: keys[i])
    return pages[best]


def _oldest_page_ids(pages: Iterable[dict], key_of) -> dict[str, str]:
    """ページ配列から {キー: 正のページの page_id} を組み立てる（事前マップ用）。

    同じキーのページが複数あれば、キー検索と同じ規則（最古）で 1 つに決める。
    事前マップと per-record 検索で更新先が食い違わないようにするため (#13)。
    """
    chosen: dict[str, dict] = {}
    for page in pages:
        key = key_of(page)
        if not key:
            continue
        current = chosen.get(key)
        if current is None or _is_older(page, current):
            chosen[key] = page
    return {key: page["id"] for key, page in chosen.items()}


def _query_key(client: NotionClient, db_id: str, flt: dict) -> list[dict]:
    return client.query_database(
        db_id, filter=flt, sorts=KEY_QUERY_SORTS,
        page_size=KEY_QUERY_PAGE_SIZE, max_pages=1,
    )


def _find_page_full(client: NotionClient, db_id: str, flt: dict) -> dict | None:
    """キー検索で正のページ全体（properties込み）を返す。

    _find_page は id だけを返す。更新前に既存プロパティを読んで比較したい場合
    （#14: 開示日時ガード等）はこちらを使う。
    同じキーのページが複数あれば最古を返す (#13。全書き手が同じページを更新する)。
    """
    return oldest_page(_query_key(client, db_id, flt))


def _find_page(client: NotionClient, db_id: str, flt: dict) -> str | None:
    page = _find_page_full(client, db_id, flt)
    return page["id"] if page else None


def _read_date_prop(page: dict, prop_name: str) -> datetime | None:
    """page から date 型プロパティを datetime で読む。無ければ None。"""
    prop = page.get("properties", {}).get(prop_name) or {}
    date_obj = prop.get("date")
    if not date_obj or not date_obj.get("start"):
        return None
    try:
        return datetime.fromisoformat(date_obj["start"])
    except ValueError:
        return None


def _upsert(
    client: NotionClient,
    db_id: str,
    flt: dict,
    props: dict,
    *,
    existing_page_id: str | None = None,
    page_resolved: bool = False,
    rewrite_allowed: Callable[[dict], bool] | None = None,
) -> str:
    """キー検索 → update or create で冪等に書く (§8.1-6)。

    page_resolved=True のとき existing_page_id を正として per-record の検索クエリを
    省く（全銘柄ループでの 1req/銘柄 を削減 §8.3。事前に DB 全行マップを一括取得して
    渡す運用）。事前マップは **all-or-nothing** で渡すこと: 部分マップを True で渡すと
    未収録キーが create され重複行になる（§8.1-6 冪等性違反）。マップ取得に失敗した
    ときは page_resolved=False にして従来の per-record 検索へフォールバックする。
    existing_page_id が（削除済み等で）実在しない場合は object_not_found を握って
    create へフォールバックし、事前マップと実DBのズレを自己修復する。

    create したときだけ、同じキーで 1 回再検索して重複を収束させる
    (#13, converge_created_page)。update の経路では追加の問い合わせをしない。
    rewrite_allowed は収束時に正のページへ書き直してよいかの判定（③の開示日時ガード）。
    """
    return _upsert_outcome(
        client, db_id, flt, props,
        existing_page_id=existing_page_id, page_resolved=page_resolved,
        rewrite_allowed=rewrite_allowed,
    ).page_id


def _upsert_outcome(
    client: NotionClient,
    db_id: str,
    flt: dict,
    props: dict,
    *,
    existing_page_id: str | None = None,
    page_resolved: bool = False,
    rewrite_allowed: Callable[[dict], bool] | None = None,
) -> UpsertOutcome:
    """_upsert の本体。重複の収束結果まで返す。"""
    page_id = existing_page_id if page_resolved else _find_page(client, db_id, flt)
    if page_id:
        try:
            client.update_page(page_id, props)
            return UpsertOutcome(page_id=page_id)
        except APIResponseError as exc:
            # 事前マップの page_id が実在しない（削除済み等）→ create で自己修復
            if not (page_resolved and getattr(exc, "code", "") == "object_not_found"):
                raise
    created = client.create_page(parent={"database_id": db_id}, properties=props)
    return converge_created_page(
        client, db_id, flt, created,
        rewrite_props=props, rewrite_allowed=rewrite_allowed,
    )


def converge_created_page(
    client: NotionClient,
    db_id: str,
    flt: dict,
    created: dict,
    *,
    rewrite_props: dict | None,
    rewrite_allowed: Callable[[dict], bool] | None = None,
) -> UpsertOutcome:
    """create した直後に同じキーで 1 回だけ再検索し、重複を 1 つに収束させる (#13)。

    - 自分が正（最古。同じ時刻なら page id が最小）なら何もしない。自分より新しい
      重複があっても触らない（それを作った書き手が自分で archive する）。
    - 自分より正しいページがあれば、rewrite_props をそのページへ update で書き直し、
      **この呼び出しで作ったページだけ**を archive する（復元可能）。呼び出し前から
      あったページや、他の書き手が作ったページは archive しない。
      rewrite_allowed が False を返したら書き直さない（③: 正のページの方が新しい開示）。
      rewrite_props=None なら書き直さず archive だけする（⑤: 内容が同じと分かっている）。
    - 書き直しに失敗したら archive しない（自分のページに値が残る）。archive に失敗
      したら正のページを返す。どちらも例外にせず警告ログと warning で知らせる。
    - dry-run 合成ID は何も書いていないので確認しない（追加の問い合わせもしない）。

    同時に 2 つの書き手が作ったとき、両方が互いを見れば同じ規則で同じ正を選ぶので、
    archive されるのは正でない側だけになる。
    """
    created_id = created["id"]
    base = UpsertOutcome(page_id=created_id, created=True, created_page_id=created_id)
    if real_page_id(created_id) is None:
        return base
    try:
        results = _query_key(client, db_id, flt)
    except Exception as exc:  # noqa: BLE001 - 確認できないだけ。書き込み自体は成功している
        return _converge_warning(base, f"作成直後の再確認に失敗（重複の有無は未確認）: {exc}")

    own_norm = _normalize_page_id(created_id)
    own = next(
        (page for page in results if _normalize_page_id(page.get("id", "")) == own_norm),
        created,
    )
    others = [page for page in results if _normalize_page_id(page.get("id", "")) != own_norm]
    if not others:
        return base
    pool = [own, *others]
    if any(_page_order_key(page) is None for page in pool):
        return _converge_warning(
            base, "作成直後の再確認: created_time が無く正のページを決められない"
        )
    canonical = oldest_page(pool)
    if canonical is own:
        logger.info(
            "同じキーの新しいページがある（作った側が収束させる）: db=%s 正=%s 他=%s",
            db_id, created_id, [page["id"] for page in others],
        )
        return base

    canonical_id = canonical["id"]
    rewritten = False
    if rewrite_props is not None and (rewrite_allowed is None or rewrite_allowed(canonical)):
        try:
            client.update_page(canonical_id, rewrite_props)
            rewritten = True
        except Exception as exc:  # noqa: BLE001 - 自分のページに値が残るので落とさない
            return _converge_warning(
                UpsertOutcome(
                    page_id=created_id, created=True, created_page_id=created_id,
                    duplicate_found=True,
                ),
                f"重複の収束: 正のページ {canonical_id} への書き直しに失敗。"
                f"作ったページ {created_id} を残す: {exc}",
            )
    try:
        client.archive_page(created_id)
    except Exception as exc:  # noqa: BLE001 - 値は正のページにあるので落とさない
        return _converge_warning(
            UpsertOutcome(
                page_id=canonical_id, created=True, created_page_id=created_id,
                duplicate_found=True, rewritten=rewritten,
            ),
            f"重複の収束: 作ったページ {created_id} の archive に失敗（重複が残る）: {exc}",
        )
    logger.warning(
        "同じキーのページが既にあったため、作ったページを archive して正へ寄せた: "
        "db=%s 作成=%s 正=%s 書き直し=%s",
        db_id, created_id, canonical_id, rewritten,
    )
    return UpsertOutcome(
        page_id=canonical_id, created=True, created_page_id=created_id,
        duplicate_found=True, rewritten=rewritten, archived=True,
    )


def _converge_warning(outcome: UpsertOutcome, message: str) -> UpsertOutcome:
    logger.warning("%s", message)
    return replace(outcome, warning=message)


def find_stock_master_page(client: NotionClient, settings: Settings, code: str) -> str | None:
    """① から銘柄コードで page_id を引く (③④の relation 設定用ヘルパー)。"""
    return _find_page(client, settings.db_id("stock_master"), stock_master_filter(code))


def load_stock_master_map(client: NotionClient, settings: Settings) -> dict[str, str]:
    """① 全行の {銘柄コード: page_id} を一括取得する。

    全銘柄ループでの find_stock_master_page (1req/銘柄) を置き換える (§8.3)。
    """
    return _master_map_from_pages(client.query_database(settings.db_id("stock_master")))


def _master_code_of(page: dict) -> str:
    """① ページの銘柄コードを読む（事前マップのキー）。"""
    rich = page.get("properties", {}).get(S.MASTER_PROP_CODE, {}).get("rich_text", [])
    return rich[0].get("plain_text", "").strip() if rich else ""


def _master_map_from_pages(pages: list[dict]) -> dict[str, str]:
    """① のページ配列から {銘柄コード: page_id} を組み立てる（純粋関数）。

    同じスキャン結果から EDINETコード逆引き (_edinet_map_from_pages) も作れるよう、
    取得と組み立てを分けている。
    """
    # 同じコードの重複ページはキー検索と同じ規則（最古）で 1 つに決める (#13)
    return _oldest_page_ids(pages, _master_code_of)


def _master_entries_from_pages(pages: list[dict]) -> dict[str, tuple[str, dict]]:
    """① のページ配列から {銘柄コード: (page_id, properties)} を組む（純粋関数）。

    L-19 の同値 skip 用。`_master_map_from_pages` と同じ最古勝ちで、
    page ごとの properties も保持する（比較に使う）。
    """
    chosen: dict[str, dict] = {}
    for page in pages:
        key = _master_code_of(page)
        if not key:
            continue
        current = chosen.get(key)
        if current is None or _is_older(page, current):
            chosen[key] = page
    return {
        key: (page["id"], page.get("properties", {}))
        for key, page in chosen.items()
    }


def load_stock_master_entries(
    client: NotionClient, settings: Settings
) -> dict[str, tuple[str, dict]]:
    """① 全行の {銘柄コード: (page_id, properties)} を一括取得する。

    `load_stock_master_map` の properties 付き版。master_sync の同値 skip
    （L-19）が既存行との比較に使う。余分な req は出ない（同じ 1 スキャン）。
    """
    return _master_entries_from_pages(
        client.query_database(settings.db_id("stock_master"))
    )


def _read_text_value(properties: dict, name: str) -> str:
    """title / rich_text の plain_text を連結して読む。読めなければ ""。"""
    blocks = properties.get(name, {}).get(
        "title", properties.get(name, {}).get("rich_text", [])
    )
    if not isinstance(blocks, list):
        return ""
    return "".join(
        b.get("plain_text", "") for b in blocks if isinstance(b, dict)
    ).strip()


def _read_select_name(properties: dict, name: str) -> str | None:
    """select の選択名を読む。未選択・読めなければ None。"""
    sel = properties.get(name, {}).get("select")
    if not isinstance(sel, dict):
        return None
    return sel.get("name")


def stock_master_matches_page(
    properties: dict, record: StockMasterRecord
) -> bool:
    """① の既存行が record と同値か（L-19。月次 3,841 PATCH → 差分のみ）。

    比較するのは codelist 所有の意味フィールド（名称/コード/上場状態/
    市場/33業種/17業種/EDINETコード）だけ。次は見ない:

    - 時刻系（最終データ更新日/取得日時/データ基準日）: 毎 run 変わるので
      見ると skip が永遠に発火しない。意味が変わった run の PATCH で更新される
    - 由来系（ソース/ライセンス/品質/原本）: master_sync では実行ごとに同一
    - ライフサイクル 3 項目: 開示・消失が所有し master_sync は書かない

    読めない形の行は False（書く側に倒す。欠損より二重 PATCH がまし）。
    """
    want_name = _clip(record.name)
    if _read_text_value(properties, S.MASTER_PROP_NAME) != want_name:
        return False
    if _read_text_value(properties, S.MASTER_PROP_CODE) != record.code:
        return False
    if bool(properties.get(S.MASTER_PROP_LISTED, {}).get("checkbox", False)) != bool(
        record.listed
    ):
        return False
    for prop, want in (
        (S.MASTER_PROP_MARKET, record.market),
        (S.MASTER_PROP_SECTOR33, record.sector33),
        (S.MASTER_PROP_SECTOR17, record.sector17),
    ):
        if _read_select_name(properties, prop) != want:
            return False
    want_edinet = record.edinet_code or ""
    return _read_text_value(properties, S.MASTER_PROP_EDINET_CODE) == want_edinet


def _edinet_code_of(properties: dict) -> str:
    """① ページの properties から EDINETコードを読む。無ければ ""。"""
    rich = properties.get(S.MASTER_PROP_EDINET_CODE, {}).get("rich_text", [])
    return rich[0].get("plain_text", "").strip() if rich else ""


def _edinet_map_from_pages(pages: list[dict]) -> dict[str, str]:
    """① のページ配列から {EDINETコード: 銘柄コード} を組み立てる（純粋関数）。

    大量保有報告書 (350/360) は**保有者が提出する**ため `secCode` が入らない
    (実測 992 件中 956 件が空)。対象会社は `issuerEdinetCode` にしか出ないので、
    EDINETコードから銘柄コードへ引くための逆引きが要る。
    """
    out: dict[str, str] = {}
    for page in pages:
        props = page.get("properties", {})
        code = _master_code_of(page)
        edinet_code = _edinet_code_of(props)
        if code and edinet_code:
            out[edinet_code] = code
    return out


def _jst_day_start(day: date) -> str:
    """JST のその日の 0:00 を、オフセット付きの ISO 8601 で返す（④ の事前マップの窓の境界）。

    ④ の開示日時は `2026-09-11T08:00:00+09:00` のように JST のオフセット付きで入る。
    ここへ日付だけ（`"2026-09-11"`）を渡すと、Notion はそれを **UTC の 0:00** として
    比べるので、**JST 0:00〜9:00 の開示が前日扱いになって窓から漏れる**。一方の呼び出し側は
    `record.disclosed_at.date()`（JST の日付）で「窓の内側」と判定して `page_resolved=True`
    を立てるので、マップに無い = 新規とみなして**検索せずに create する**。毎時の
    `tdnet_hourly` がその日に走るたびに 1 ページずつ増えていた。

    2026-09-13 の読み取り専用監査で、④ の重複 423 docID / 余分 1,312 行のうち
    366 docID / 1,253 行（95%）がこの形（実行ごとに 30 分以上あけて増える、開示日時は
    JST 08:00 / 08:30 など 9 時前）だった。同じ分に 2 ページできる競合・再送型は 40 行。
    """
    return datetime(day.year, day.month, day.day, tzinfo=JST).isoformat()


def _disclosure_doc_id_of(page: dict) -> str:
    """④ ページの書類管理番号を読む（事前マップのキー）。"""
    rich = page.get("properties", {}).get(S.DISC_PROP_DOC_ID, {}).get("rich_text", [])
    return rich[0].get("plain_text", "").strip() if rich else ""


def _query_disclosure_pages(
    client: NotionClient, settings: Settings, disclosed_date: date | None
) -> list[dict]:
    """④ を対象日スコープで 1 回スキャンする（L-20。map/entries で共有）。

    ④ は無制限に増える追記型のため、disclosed_date を渡して **その日の開示のみ** に
    絞る（JST の [d 0:00, d+1 0:00) の半開区間。境界の理由は `_jst_day_start`）。
    disclosed_date=None なら全件（小規模時のみ）。
    """
    flt = None
    if disclosed_date is not None:
        nxt = disclosed_date + timedelta(days=1)
        flt = {
            "and": [
                {"property": S.DISC_PROP_DISCLOSED_AT, "date": {"on_or_after": _jst_day_start(disclosed_date)}},
                {"property": S.DISC_PROP_DISCLOSED_AT, "date": {"before": _jst_day_start(nxt)}},
            ]
        }
    return client.query_database(settings.db_id("disclosures"), filter=flt)


def load_disclosure_page_map(
    client: NotionClient, settings: Settings, *, disclosed_date: date | None = None
) -> dict[str, str]:
    """④ の {書類管理番号(docID): page_id} を一括取得する (§8.3 per-record 検索排除)。

    開示ジョブは対象日の一覧を処理し doc_id は開示日に生成されるので、その日の
    ウィンドウに対象 doc_id の既存行が必ず入る＝date-scoped でも create/update を
    取り違えない。呼び出し側は record.disclosed_at.date() が disclosed_date と一致する
    レコードにのみ page_resolved を立てること（ウィンドウ外は per-record 検索へ
    フォールバック=重複防止）。キーは DISC_PROP_DOC_ID(rich_text)。
    """
    pages = _query_disclosure_pages(client, settings, disclosed_date)
    # 同じ docID の重複ページはキー検索と同じ規則（最古）で 1 つに決める (#13)
    return _oldest_page_ids(pages, _disclosure_doc_id_of)


def _disclosure_entries_from_pages(pages: list[dict]) -> dict[str, tuple[str, dict]]:
    """④ のページ配列から {doc_id: (page_id, properties)} を組む（純粋関数）。

    L-20 の同値 skip 用。`load_disclosure_page_map` と同じ最古勝ちで、
    page ごとの properties も保持する（比較に使う）。
    """
    chosen: dict[str, dict] = {}
    for page in pages:
        key = _disclosure_doc_id_of(page)
        if not key:
            continue
        current = chosen.get(key)
        if current is None or _is_older(page, current):
            chosen[key] = page
    return {
        key: (page["id"], page.get("properties", {}))
        for key, page in chosen.items()
    }


def load_disclosure_page_entries(
    client: NotionClient, settings: Settings, *, disclosed_date: date | None = None
) -> dict[str, tuple[str, dict]]:
    """④ の {doc_id: (page_id, properties)} を対象日スコープで一括取得する。

    `load_disclosure_page_map` の properties 付き版。同値 skip（L-20）が
    既存行との比較に使う。余分な req は出ない（同じ 1 スキャン）。
    """
    return _disclosure_entries_from_pages(
        _query_disclosure_pages(client, settings, disclosed_date)
    )


def _read_date_start(properties: dict, name: str) -> str | None:
    """date の start 文字列を読む。未設定・読めなければ None。"""
    dt = properties.get(name, {}).get("date")
    if not isinstance(dt, dict):
        return None
    start = dt.get("start")
    return start if isinstance(start, str) else None


def _read_relation_ids(properties: dict, name: str) -> list[str]:
    """relation の id 一覧を読む。読めなければ []。"""
    rel = properties.get(name, {}).get("relation", [])
    if not isinstance(rel, list):
        return []
    return [r.get("id", "") for r in rel if isinstance(r, dict)]


def _same_moment(want_iso: str, got_start: str | None) -> bool:
    """日時の同値判定。表記揺れ（+09:00 / Z）は時刻として吸収する。

    Notion が start を正規化して返しても skip が死なないようにする。
    読めなければ False（書く側に倒す）。
    """
    if got_start is None:
        return False
    if want_iso == got_start:
        return True
    try:
        return datetime.fromisoformat(want_iso) == datetime.fromisoformat(got_start)
    except ValueError:
        return False


def disclosure_matches_page(
    properties: dict, record: DisclosureRecord, master_page_id: str | None
) -> bool:
    """④ の既存行が record と同値か（L-20。毎時再 PATCH → 差分のみ）。

    比較するのは `disclosure_properties` の意味フィールド（タイトル/開示日時/
    書類種別/管理番号/XBRL有無/銘柄コード/URL/分割 3 項目）+ 2 つの relation。
    次は見ない:

    - 取得日時（fetched_at）: 毎 run 変わるので見ると skip が死ぬ
    - データ基準日・由来系（ソース/ライセンス/品質）: 同一書類では実行ごとに同一

    relation は「書きたい値があるときだけ」比べる。`master_page_id` が None の
    run は payload に relation を含めないので、既存行に relation があっても
    書き直す意味が無い（比べると毎回不一致で skip が死ぬ）。原本 relation も同じ。
    読めない形の行は False（書く側に倒す）。
    """
    if _read_text_value(properties, S.DISC_PROP_TITLE) != _clip(record.title):
        return False
    if not _same_moment(
        record.disclosed_at.isoformat(),
        _read_date_start(properties, S.DISC_PROP_DISCLOSED_AT),
    ):
        return False
    if _read_select_name(properties, S.DISC_PROP_DOC_TYPE) != record.doc_type:
        return False
    if _read_text_value(properties, S.DISC_PROP_DOC_ID) != record.doc_id:
        return False
    if bool(properties.get(S.DISC_PROP_HAS_XBRL, {}).get("checkbox", False)) != bool(
        record.has_xbrl
    ):
        return False
    if _read_text_value(properties, S.DISC_PROP_CODE) != (record.code or ""):
        return False
    got_url = properties.get(S.DISC_PROP_URL, {}).get("url")
    if (got_url or None) != (record.source_url or None):
        return False
    if _read_text_value(properties, S.DISC_PROP_SPLIT_RATIO) != (record.split_ratio or ""):
        return False
    got_factor = properties.get(S.DISC_PROP_SPLIT_FACTOR, {}).get("number")
    if (got_factor if got_factor is not None else None) != record.split_factor:
        return False
    want_effective = record.effective_date.isoformat() if record.effective_date else None
    if _read_date_start(properties, S.DISC_PROP_EFFECTIVE_DATE) != want_effective:
        return False
    if master_page_id is not None and _read_relation_ids(
        properties, S.PROP_MASTER_RELATION
    ) != [master_page_id]:
        return False
    raw_id = real_page_id(record.provenance.raw_page_id)
    if raw_id is not None and _read_relation_ids(
        properties, S.PROP_RAW_RELATION
    ) != [raw_id]:
        return False
    return True


def upsert_stock_master(
    client: NotionClient,
    settings: Settings,
    record: StockMasterRecord,
    *,
    include_lifecycle: bool = True,
    existing_page_id: str | None = None,
    page_resolved: bool = False,
) -> str:
    """① 銘柄マスタへ冪等 upsert (キー=銘柄コード)。page_id を返す。

    master_sync(codelist 同期) は include_lifecycle=False で呼び、開示・消失が
    所有する 状態/上場日/上場廃止日 を上書きしない (§ Phase3 二重所有の回避)。

    全銘柄ループでは load_stock_master_map で得た {code: page_id} を
    existing_page_id に渡し page_resolved=True にすると per-record 検索を省ける
    (§8.3)。事前マップは all-or-nothing で渡すこと (_upsert 参照)。
    """
    return _upsert(
        client,
        settings.db_id("stock_master"),
        stock_master_filter(record.code),
        stock_master_properties(record, include_lifecycle=include_lifecycle),
        existing_page_id=existing_page_id,
        page_resolved=page_resolved,
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
    client: NotionClient,
    settings: Settings,
    record: DisclosureRecord,
    *,
    master_page_id: str | None = None,
    master_resolved: bool = False,
) -> str | None:
    """上場廃止/新規上場 開示を ① のライフサイクル状態へ反映する (§ Phase3)。

    master_resolved=True のとき master_page_id を ① の page_id として使い、内部の
    find_stock_master_page(① per-record 検索)を省く (§8.3。開示ジョブが ① マップを
    事前ロードして渡す)。master_resolved=True かつ master_page_id=None は「① に該当
    銘柄なし」を意味し、従来同様 None を返して何もしない（事前マップが正なので
    per-record 検索へは戻らない）。

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
    page_id = master_page_id if master_resolved else find_stock_master_page(
        client, settings, record.code
    )
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


def _select_name(page: dict, prop: str) -> str | None:
    value = (page.get("properties", {}).get(prop) or {}).get("select") or {}
    return value.get("name") or None


def pick_financial_page(
    pages: list[dict], disclosure_type: str, consolidated: str | None
) -> dict | None:
    """③ のキー検索結果 (financial_summary_lookup_filter) から書き込み先を 1 つ選ぶ。

    優先順:
    1. 正確なキーの行
    2. 旧開示種別（「中間」に対する「2Q」）で、連結単体が合う行
    3. 連結単体が空の行
    4. 旧開示種別で、連結単体が空の行
    連結単体が合うことを開示種別より優先する。「2Q」→「中間」は同じ期の呼び名を
    変えただけだが、連結単体が空の行はどちらの測定範囲の値か分からないため。
    同じ順位に複数あれば最古を正とする (#13)。
    """
    if not pages:
        return None

    def rank(page: dict) -> tuple[bool, bool]:
        return (
            _select_name(page, S.FIN_PROP_CONSOLIDATED) != (consolidated or None),
            _select_name(page, S.FIN_PROP_DISCLOSURE_TYPE) != disclosure_type,
        )

    best = min(rank(page) for page in pages)
    return oldest_page([page for page in pages if rank(page) == best])


def upsert_financial_summary(
    client: NotionClient,
    settings: Settings,
    record: FinancialSummaryRecord,
    master_page_id: str | None = None,
) -> str:
    """③ 財務サマリへ冪等 upsert (複合キー=銘柄コード×決算期末×開示種別×連結単体)。

    **旧キーの行の採用**: 正確なキーの行が無ければ、連結単体が空の行や、ラベル変更前の
    「2Q」の行（今回が「中間」のとき）を採用して書き込む（pick_financial_page）。
    ③ は完全置換なので、採用した行の連結単体・開示種別・タイトルも今回の値に替わる。
    キーを変えても既存行を重複させないため。作成後の重複確認 (#13) は正確なキーで行う。

    **開示日時ガード (#14)**: 既存行の開示日 (FIN_PROP_DISCLOSED_AT) が今回の
    record.disclosed_at より新しければ上書きしない。古い原報告（またはその
    backfill・再実行）を訂正版の後に流しても、訂正後の値を巻き戻さないため。
    どちらかの開示日が不明 (None) なら順序を判断できないので、推測で
    ブロックせず従来どおり完全置換する（§3-1 推測で守らない）。日時の型不一致等
    で比較自体に失敗した場合も同様に安全側＝上書き許可へ倒す。

    副次効果: TDnet短信(先行)とEDINET有報(後発、より監査済み)が同一キーを
    共有する既知の課題（Issue #14 派生）でも、有報の開示日は短信より後になる
    のが通常のため、有報着地後に短信バッチが再実行されても有報値を守れる。
    ただしキー自体にソースを含めていないため、これは緩和であり根治ではない。
    """
    db_id = settings.db_id("financials")
    key = (record.code, record.fiscal_period_end, record.disclosure_type, record.consolidated)
    flt = financial_summary_filter(*key)
    existing = pick_financial_page(
        _query_key(client, db_id, financial_summary_lookup_filter(*key)),
        record.disclosure_type, record.consolidated,
    )
    if existing is not None and (
        _select_name(existing, S.FIN_PROP_DISCLOSURE_TYPE) != record.disclosure_type
        or _select_name(existing, S.FIN_PROP_CONSOLIDATED) != (record.consolidated or None)
    ):
        logger.info(
            "③ 財務サマリ: 旧キーの行を採用して新しいキーで書き直す: page=%s 旧=(%s, %s) "
            "新=(%s, %s) %s %s",
            existing.get("id"),
            _select_name(existing, S.FIN_PROP_DISCLOSURE_TYPE),
            _select_name(existing, S.FIN_PROP_CONSOLIDATED),
            record.disclosure_type, record.consolidated, record.code, record.fiscal_period_end,
        )
    if existing is not None and not financial_overwrite_allowed(existing, record):
        return existing["id"]
    # existing は直前の _find_page_full で確定済み（prefetch マップ経由ではない
    # just-in-time の単発検索）なので、None の場合も含め page_resolved=True で渡し、
    # _upsert 内での _find_page 再クエリ（二重問い合わせ）を避ける。
    # create 後の重複収束 (#13) で正のページへ書き直すときも同じガードを通す
    # （同時に作られた別の書き手のページの方が新しい開示なら巻き戻さない）。
    return _upsert(
        client,
        db_id,
        flt,
        financial_summary_properties(record, master_page_id),
        existing_page_id=existing["id"] if existing else None,
        page_resolved=True,
        rewrite_allowed=lambda page: financial_overwrite_allowed(page, record),
    )


def financial_overwrite_allowed(existing: dict, record: FinancialSummaryRecord) -> bool:
    """③ の既存ページを record で上書きしてよいか（開示日時ガード #14）。

    既存の開示日が record より新しければ False。どちらかが不明、または比較できない
    （tz aware/naive 混在等）なら推測でブロックせず True（§3-1）。
    """
    if record.disclosed_at is None:
        return True
    existing_disclosed_at = _read_date_prop(existing, S.FIN_PROP_DISCLOSED_AT)
    if existing_disclosed_at is None:
        return True
    try:
        is_older = existing_disclosed_at > record.disclosed_at
    except TypeError:
        # tz aware/naive 混在等で比較不能。安全側＝上書き許可のまま進む。
        logger.warning(
            "③ 財務サマリ: 開示日の比較に失敗（型不一致）。上書きを許可して継続: "
            "%s %s %s 既存=%r 今回=%r",
            record.code, record.fiscal_period_end, record.disclosure_type,
            existing_disclosed_at, record.disclosed_at,
        )
        return True
    if is_older:
        logger.info(
            "③ 財務サマリ: 既存(開示日=%s)より古い報告(開示日=%s)のため上書きしない: "
            "%s %s %s",
            existing_disclosed_at, record.disclosed_at,
            record.code, record.fiscal_period_end, record.disclosure_type,
        )
        return False
    return True


def upsert_disclosure(
    client: NotionClient,
    settings: Settings,
    record: DisclosureRecord,
    master_page_id: str | None = None,
    *,
    existing_page_id: str | None = None,
    page_resolved: bool = False,
) -> str:
    """④ 開示書類へ冪等 upsert (キー=書類管理番号 docID)。

    開示ジョブのループでは load_disclosure_page_map(disclosed_date=対象日) で得た
    {doc_id: page_id} を existing_page_id に渡し page_resolved=True で per-record 検索を
    省ける (§8.3)。all-or-nothing で渡すこと (_upsert 参照)。master_page_id は ① relation。
    """
    return _upsert(
        client,
        settings.db_id("disclosures"),
        disclosure_filter(record.doc_id),
        disclosure_properties(record, master_page_id),
        existing_page_id=existing_page_id,
        page_resolved=page_resolved,
    )


# ⑥時系列エクスポート・⑦収集ジョブログは廃止した。
# ⑥の配布物は作らない。⑦の実行履歴は D1 jss_job_runs に一本化した
# (jobs/runner.py が safe_record_job_run で書く)。
