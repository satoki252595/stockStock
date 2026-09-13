"""Notion 7DB+データカタログの冪等セットアップ (DESIGN.md §6, §7, P0)。

「株式情報」親ページ (settings.notion_parent_page_id) 配下に §6.2 の 7DB と
「📖 データカタログ」ページを冪等に作成する。DB論理キーとタイトルは
config.DB_REGISTRY を唯一の正とする。

共通プロパティ (§6.3) の適用範囲:
- ①銘柄マスタ/②株価テクニカル/③財務サマリ/④開示書類/⑥時系列エクスポート:
  全共通プロパティ（ソース/ライセンスタグ/データ基準日/取得日時/原本 relation→⑤/データ品質）
- ⑤原本ファイル: ソース/ライセンスタグ/データ基準日/取得日時 のみ。
  自分自身への「原本」relation と「データ品質」は持たない。
  代わりに固有の「変換状態」select (§5.2) を持つ
- ⑦収集ジョブログ: ジョブ運用ログのため共通プロパティ対象外

リレーション (§6.2):
- 作成順: ⑤ → ① → ②③④⑥⑦ → 最後に ⑤へ「関連銘柄」relation→① を後付け
  （⑤は①より先に作るため、①へのrelationは作成時に定義できない）
- relation はすべて **dual_property** で定義する。これにより①側に
  ②③④⑤からの逆向きプロパティが自動生成され、銘柄ページから株価・財務・
  開示・原本をすべて辿れる (§6.2「①をハブに」)

冪等性:
- 親ページの子ブロックを list_child_blocks() で列挙し、child_database の
  タイトル一致で既存DBを発見。既存なら create せず retrieve_database で
  現プロパティを取得し、**不足プロパティのみ** update_database で追加する
  （既存プロパティの型変更・削除は一切しない）
- データカタログページも子ページのタイトル一致で冪等に作成する

ビューについて (§7):
- 公開 Notion REST API はビュー（フィルタ/ソート済み表示）の作成に未対応のため、
  推奨ビュー一覧を定数 RECOMMENDED_VIEWS として定義し、データカタログページに
  手動作成手順を記載し、実行ログにも案内を出す

CLI: python -m jp_stock_pipeline.notion.schema [--dry-run] [--json-out db_ids.json]
実行後 {論理キー: DB ID} を db_ids.json に書き出し stdout にも表示する
（dry-run 時は合成IDのためファイル書き出しはスキップし stdout のみ）。
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DB_REGISTRY, Settings, load_settings
from ..licensing import ATTRIBUTION, LicenseTag
from ..models import ConvertStatus, DataQuality, Source
from .client import NotionClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# プロパティ名定数 (§6.3 / §6.4)。upsert.py / file_upload.py から import する。
# ---------------------------------------------------------------------------

# 共通プロパティ (§6.3)
PROP_SOURCE = "ソース"
PROP_LICENSE_TAG = "ライセンスタグ"
PROP_DATA_DATE = "データ基準日"
PROP_FETCHED_AT = "取得日時"
PROP_RAW_RELATION = "原本"
PROP_QUALITY = "データ品質"

# ②③④⑤ → ① へのリレーション名
PROP_MASTER_RELATION = "銘柄マスタ"

# ① 銘柄マスタ (§6.4)
MASTER_PROP_NAME = "銘柄名"  # title
MASTER_PROP_CODE = "銘柄コード"
MASTER_PROP_MARKET = "市場区分"
MASTER_PROP_SECTOR33 = "33業種"
MASTER_PROP_SECTOR17 = "17業種"
MASTER_PROP_EDINET_CODE = "EDINETコード"
MASTER_PROP_LISTED = "上場状態"  # checkbox: 現在上場しているか
MASTER_PROP_STATUS = "状態"  # select: 上場/監理/整理/上場廃止
MASTER_PROP_LISTING_DATE = "上場日"
MASTER_PROP_DELISTING_DATE = "上場廃止日"
MASTER_PROP_LAST_UPDATED = "最終データ更新日"
# ① 配下の株価テクニカル履歴子DBへのポインタ。master_sync は触らない
# （prices_daily が所有。完全置換 payload に含めない）。
MASTER_PROP_HISTORY_DB_ID = "現行履歴DB ID"
MASTER_PROP_HISTORY_SHARD = "履歴シャード番号"
MASTER_PROP_HISTORY_ROW_COUNT = "履歴行数"
HISTORY_DB_TITLE = "株価テクニカル履歴"
HISTORY_PROP_DATE_TITLE = "基準日"  # 履歴子DBの title。キー=データ基準日
HISTORY_SHARD_THRESHOLD = 8000  # API 1クエリ1万件の手前で次DBへ

# ② 株価テクニカル (§6.4)
PRICE_PROP_CODE = "銘柄コード"  # title
PRICE_PROP_OPEN = "始値"
PRICE_PROP_HIGH = "高値"
PRICE_PROP_LOW = "安値"
PRICE_PROP_CLOSE = "終値"
PRICE_PROP_PREV_PCT = "前日比率%"
PRICE_PROP_VOLUME = "出来高"
PRICE_PROP_TURNOVER = "売買代金"
PRICE_PROP_MARKET_CAP = "時価総額"
PRICE_PROP_W52_HIGH = "52週高値"
PRICE_PROP_W52_LOW = "52週安値"
PRICE_PROP_SMA5 = "SMA5"
PRICE_PROP_SMA25 = "SMA25"
PRICE_PROP_SMA75 = "SMA75"
PRICE_PROP_SMA200 = "SMA200"
PRICE_PROP_SMA25_DEV = "SMA25乖離率%"
PRICE_PROP_RSI14 = "RSI14"
PRICE_PROP_MACD = "MACD"
PRICE_PROP_MACD_SIGNAL = "MACDシグナル"
PRICE_PROP_MACD_HIST = "MACDヒストグラム"
PRICE_PROP_BB_UPPER = "BB+2σ"
PRICE_PROP_BB_LOWER = "BB-2σ"
PRICE_PROP_ATR14 = "ATR14"
PRICE_PROP_VOL_RATIO25 = "出来高25日平均比"
PRICE_PROP_PER = "PER"
PRICE_PROP_PBR = "PBR"
PRICE_PROP_DIV_YIELD = "配当利回り%"

# ③ 財務サマリ (§6.4)
FIN_PROP_TITLE = "タイトル"  # title 例: 7203 2026/03期 本決算
FIN_PROP_CODE = "銘柄コード"
FIN_PROP_PERIOD_END = "決算期末"
FIN_PROP_DISCLOSURE_TYPE = "開示種別"
FIN_PROP_CONSOLIDATED = "連結単体"
FIN_PROP_STANDARD = "会計基準"
FIN_PROP_NET_SALES = "売上高"
FIN_PROP_OPERATING_INCOME = "営業利益"
FIN_PROP_ORDINARY_INCOME = "経常利益"
FIN_PROP_NET_INCOME = "純利益"
FIN_PROP_EPS = "EPS"
FIN_PROP_BPS = "BPS"
FIN_PROP_ROE = "ROE%"
FIN_PROP_ROA = "ROA%"
FIN_PROP_EQUITY_RATIO = "自己資本比率%"
FIN_PROP_CF_OPERATING = "営業CF"
FIN_PROP_CF_INVESTING = "投資CF"
FIN_PROP_CF_FINANCING = "財務CF"
FIN_PROP_DPS_ACTUAL = "1株配当(実績)"
FIN_PROP_DPS_FORECAST = "1株配当(予想)"
FIN_PROP_FC_NET_SALES = "来期予想売上高"
FIN_PROP_FC_OPERATING_INCOME = "来期予想営業利益"
FIN_PROP_FC_ORDINARY_INCOME = "来期予想経常利益"
FIN_PROP_FC_NET_INCOME = "来期予想純利益"
FIN_PROP_FC_EPS = "来期予想EPS"
FIN_PROP_DISCLOSED_AT = "開示日"

# ④ 開示書類 (§6.4)
DISC_PROP_TITLE = "開示タイトル"  # title
DISC_PROP_DISCLOSED_AT = "開示日時"
DISC_PROP_DOC_TYPE = "書類種別"
DISC_PROP_DOC_ID = "書類管理番号"
DISC_PROP_CODE = "銘柄コード"
DISC_PROP_URL = "取得元URL"
DISC_PROP_HAS_XBRL = "XBRL有無"
# コーポレートアクション属性 (分割/併合の開示で設定。§ Phase2/4)
DISC_PROP_SPLIT_RATIO = "分割比率"  # 例 "1:3"
DISC_PROP_SPLIT_FACTOR = "分割係数"  # 例 3.0 / 0.2 (新株数/旧株数。Phase4 価格調整用)
DISC_PROP_EFFECTIVE_DATE = "効力発生日"

# ⑤ 原本ファイル (§6.4)
RAW_PROP_FILENAME = "ファイル名"  # title (命名規則名 §5.2)
RAW_PROP_FILES = "ファイル"
RAW_PROP_DATATYPE = "データ種別"
RAW_PROP_SCOPE = "対象銘柄コード"  # 一括は "ALL"
RAW_PROP_PERIOD = "対象期間"
RAW_PROP_URL = "取得URL"
RAW_PROP_SHA256 = "SHA256"
RAW_PROP_SIZE = "サイズ"
RAW_PROP_CONVERT_STATUS = "変換状態"
RAW_PROP_RELATED_MASTER = "関連銘柄"

# ⑥ 時系列エクスポート (§6.4)
EXPORT_PROP_NAME = "データセット名"  # title
EXPORT_PROP_FILES = "ファイル"
EXPORT_PROP_PERIOD = "対象期間"
EXPORT_PROP_ROW_COUNT = "行数"
EXPORT_PROP_SCHEMA_DESC = "スキーマ説明"
EXPORT_PROP_UPDATED_ON = "更新日"

# ⑦ 収集ジョブログ (§6.4)
JOB_PROP_NAME = "ジョブ名"  # title
JOB_PROP_RUN_AT = "実行日時"
JOB_PROP_STATUS = "ステータス"
JOB_PROP_PROCESSED = "処理件数"
JOB_PROP_FAILED = "失敗件数"
JOB_PROP_FAILED_CODES = "失敗銘柄"
JOB_PROP_RUN_URL = "GitHub Run URL"
JOB_PROP_DURATION = "所要時間(秒)"

# select の固定選択肢 (§6.4)。enum 由来のものは models / licensing と一致させる。
DISCLOSURE_TYPES: tuple[str, ...] = ("本決算", "1Q", "2Q", "3Q", "修正", "予想")
CONSOLIDATED_TYPES: tuple[str, ...] = ("連結", "単体")
ACCOUNTING_STANDARDS: tuple[str, ...] = ("日本基準", "IFRS", "US-GAAP", "その他")
DOC_TYPES: tuple[str, ...] = (
    "短信", "有報", "四半期報告", "業績修正", "配当修正", "大量保有", "自社株買い",
    "株式分割", "株式併合", "上場廃止", "新規上場", "優待", "その他",
)
# ① 状態 select。上場=通常 / 監理・整理=上場廃止前段階 / 上場廃止=廃止済み
LISTING_STATUS_OPTIONS: tuple[str, ...] = ("上場", "監理", "整理", "上場廃止")
JOB_STATUSES: tuple[str, ...] = ("成功", "一部失敗", "失敗")

CATALOG_TITLE = "📖 データカタログ"

# §7: 公開 REST API がビュー作成未対応のため、手動作成を案内する推奨ビュー定義
RECOMMENDED_VIEWS: tuple[dict[str, str], ...] = (
    {
        "name": "高ROEランキング",
        "db": "financials",
        "setup": "③財務サマリで テーブルビュー作成 → ROE% 降順ソート + 開示種別=本決算 フィルタ",
    },
    {
        "name": "業種別",
        "db": "stock_master",
        "setup": "①銘柄マスタで テーブル/ボードビュー作成 → 33業種でグループ化",
    },
    {
        "name": "直近開示",
        "db": "disclosures",
        "setup": "④開示書類で テーブルビュー作成 → 開示日時 降順ソート",
    },
    {
        "name": "決算カレンダー",
        "db": "financials",
        "setup": "③財務サマリで カレンダービュー作成 → 日付プロパティ=開示日",
    },
    {
        "name": "公開用 commercial-ok フィルタ",
        "db": "stock_master",
        "setup": "公開対象の各DBで ライセンスタグ=commercial-ok フィルタのビューを作成"
        "（factual-cite はメタデータ+リンク列のみ表示。personal-only は公開ビューに含めない §2.2）",
    },
)


# ---------------------------------------------------------------------------
# プロパティスキーマ構築 (Notion API property schema objects)
# ---------------------------------------------------------------------------


def _select_schema(options: tuple[str, ...] | list[str] = ()) -> dict:
    return {"select": {"options": [{"name": o} for o in options]}}


def _relation_schema(database_id: str) -> dict:
    """dual_property relation (§6.2: ①側に逆向きプロパティを自動生成させる)。"""
    return {"relation": {"database_id": database_id, "type": "dual_property", "dual_property": {}}}


def _relation_schema_one_way(database_id: str) -> dict:
    """単方向 relation。銘柄ごとの履歴子DBから⑤へ張る。

    dual_property にすると ⑤ 側に銘柄数ぶんの逆向きプロパティが付くため使わない。
    """
    return {
        "relation": {
            "database_id": database_id,
            "type": "single_property",
            "single_property": {},
        }
    }


_TITLE = {"title": {}}
_RICH_TEXT = {"rich_text": {}}
_NUMBER = {"number": {}}
_DATE = {"date": {}}
_URL = {"url": {}}
_CHECKBOX = {"checkbox": {}}
_FILES = {"files": {}}

SOURCE_OPTIONS: tuple[str, ...] = tuple(s.value for s in Source)
LICENSE_OPTIONS: tuple[str, ...] = tuple(t.value for t in LicenseTag)
QUALITY_OPTIONS: tuple[str, ...] = tuple(q.value for q in DataQuality)
CONVERT_STATUS_OPTIONS: tuple[str, ...] = tuple(c.value for c in ConvertStatus)


def common_properties_schema(
    raw_db_id: str | None, *, include_raw_relation: bool = True, include_quality: bool = True
) -> dict:
    """§6.3 共通プロパティのスキーマ。⑤には raw relation / 品質を含めない。"""
    props: dict = {
        PROP_SOURCE: _select_schema(SOURCE_OPTIONS),
        PROP_LICENSE_TAG: _select_schema(LICENSE_OPTIONS),
        PROP_DATA_DATE: _DATE,
        PROP_FETCHED_AT: _DATE,
    }
    if include_quality:
        props[PROP_QUALITY] = _select_schema(QUALITY_OPTIONS)
    if include_raw_relation and raw_db_id:
        props[PROP_RAW_RELATION] = _relation_schema(raw_db_id)
    return props


def raw_files_schema() -> dict:
    """⑤ 原本ファイル (§6.4)。「関連銘柄」relation は①作成後に後付けする。"""
    return {
        RAW_PROP_FILENAME: _TITLE,
        RAW_PROP_FILES: _FILES,
        RAW_PROP_DATATYPE: _RICH_TEXT,
        RAW_PROP_SCOPE: _RICH_TEXT,
        RAW_PROP_PERIOD: _RICH_TEXT,
        RAW_PROP_URL: _URL,
        RAW_PROP_SHA256: _RICH_TEXT,
        RAW_PROP_SIZE: _NUMBER,
        RAW_PROP_CONVERT_STATUS: _select_schema(CONVERT_STATUS_OPTIONS),
        **common_properties_schema(None, include_raw_relation=False, include_quality=False),
    }


def stock_master_schema(raw_db_id: str) -> dict:
    """① 銘柄マスタ (§6.4)。②③④⑤からの逆relationは dual_property で自動生成。"""
    return {
        MASTER_PROP_NAME: _TITLE,
        MASTER_PROP_CODE: _RICH_TEXT,
        MASTER_PROP_MARKET: _select_schema(),
        MASTER_PROP_SECTOR33: _select_schema(),
        MASTER_PROP_SECTOR17: _select_schema(),
        MASTER_PROP_EDINET_CODE: _RICH_TEXT,
        MASTER_PROP_LISTED: _CHECKBOX,
        MASTER_PROP_STATUS: _select_schema(LISTING_STATUS_OPTIONS),
        MASTER_PROP_LISTING_DATE: _DATE,
        MASTER_PROP_DELISTING_DATE: _DATE,
        MASTER_PROP_LAST_UPDATED: _DATE,
        MASTER_PROP_HISTORY_DB_ID: _RICH_TEXT,
        MASTER_PROP_HISTORY_SHARD: _NUMBER,
        MASTER_PROP_HISTORY_ROW_COUNT: _NUMBER,
        **common_properties_schema(raw_db_id),
    }


def history_pointer_properties_schema() -> dict:
    """① に後付けする履歴ポインタ列。既存DBへ不足分だけ追加する。"""
    return {
        MASTER_PROP_HISTORY_DB_ID: _RICH_TEXT,
        MASTER_PROP_HISTORY_SHARD: _NUMBER,
        MASTER_PROP_HISTORY_ROW_COUNT: _NUMBER,
    }


def _price_numeric_prop_names() -> tuple[str, ...]:
    return (
        PRICE_PROP_OPEN, PRICE_PROP_HIGH, PRICE_PROP_LOW, PRICE_PROP_CLOSE,
        PRICE_PROP_PREV_PCT, PRICE_PROP_VOLUME, PRICE_PROP_TURNOVER, PRICE_PROP_MARKET_CAP,
        PRICE_PROP_W52_HIGH, PRICE_PROP_W52_LOW,
        PRICE_PROP_SMA5, PRICE_PROP_SMA25, PRICE_PROP_SMA75, PRICE_PROP_SMA200,
        PRICE_PROP_SMA25_DEV, PRICE_PROP_RSI14,
        PRICE_PROP_MACD, PRICE_PROP_MACD_SIGNAL, PRICE_PROP_MACD_HIST,
        PRICE_PROP_BB_UPPER, PRICE_PROP_BB_LOWER, PRICE_PROP_ATR14, PRICE_PROP_VOL_RATIO25,
        PRICE_PROP_PER, PRICE_PROP_PBR, PRICE_PROP_DIV_YIELD,
    )


def prices_schema(master_db_id: str, raw_db_id: str) -> dict:
    """② 株価テクニカル (§6.4)。テクニカル列は計算値 (§3-4 加工の明示はカタログに記載)。"""
    return {
        PRICE_PROP_CODE: _TITLE,
        **{name: _NUMBER for name in _price_numeric_prop_names()},
        PROP_MASTER_RELATION: _relation_schema(master_db_id),
        **common_properties_schema(raw_db_id),
    }


def history_prices_schema(raw_db_id: str) -> dict:
    """①銘柄ページ配下の株価テクニカル履歴子DB。1行=1営業日。

    原本 relation は single_property（⑤に銘柄数ぶんの逆向き列を作らない）。
    銘柄マスタ relation は不要（親ページが①行そのもの）。
    """
    return {
        HISTORY_PROP_DATE_TITLE: _TITLE,
        **{name: _NUMBER for name in _price_numeric_prop_names()},
        **common_properties_schema(None, include_raw_relation=False, include_quality=True),
        PROP_RAW_RELATION: _relation_schema_one_way(raw_db_id),
    }


def financials_schema(master_db_id: str, raw_db_id: str) -> dict:
    """③ 財務サマリ (§6.4)。キー=銘柄コード×決算期末×開示種別。"""
    numeric = (
        FIN_PROP_NET_SALES, FIN_PROP_OPERATING_INCOME, FIN_PROP_ORDINARY_INCOME,
        FIN_PROP_NET_INCOME, FIN_PROP_EPS, FIN_PROP_BPS, FIN_PROP_ROE, FIN_PROP_ROA,
        FIN_PROP_EQUITY_RATIO, FIN_PROP_CF_OPERATING, FIN_PROP_CF_INVESTING,
        FIN_PROP_CF_FINANCING, FIN_PROP_DPS_ACTUAL, FIN_PROP_DPS_FORECAST,
        FIN_PROP_FC_NET_SALES, FIN_PROP_FC_OPERATING_INCOME, FIN_PROP_FC_ORDINARY_INCOME,
        FIN_PROP_FC_NET_INCOME, FIN_PROP_FC_EPS,
    )
    return {
        FIN_PROP_TITLE: _TITLE,
        FIN_PROP_CODE: _RICH_TEXT,
        FIN_PROP_PERIOD_END: _DATE,
        FIN_PROP_DISCLOSURE_TYPE: _select_schema(DISCLOSURE_TYPES),
        FIN_PROP_CONSOLIDATED: _select_schema(CONSOLIDATED_TYPES),
        FIN_PROP_STANDARD: _select_schema(ACCOUNTING_STANDARDS),
        **{name: _NUMBER for name in numeric},
        FIN_PROP_DISCLOSED_AT: _DATE,
        PROP_MASTER_RELATION: _relation_schema(master_db_id),
        **common_properties_schema(raw_db_id),
    }


def disclosures_schema(master_db_id: str, raw_db_id: str) -> dict:
    """④ 開示書類 (§6.4)。キー=書類管理番号(docID)。"""
    return {
        DISC_PROP_TITLE: _TITLE,
        DISC_PROP_DISCLOSED_AT: _DATE,
        DISC_PROP_DOC_TYPE: _select_schema(DOC_TYPES),
        DISC_PROP_DOC_ID: _RICH_TEXT,
        DISC_PROP_CODE: _RICH_TEXT,
        DISC_PROP_URL: _URL,
        DISC_PROP_HAS_XBRL: _CHECKBOX,
        DISC_PROP_SPLIT_RATIO: _RICH_TEXT,
        DISC_PROP_SPLIT_FACTOR: _NUMBER,
        DISC_PROP_EFFECTIVE_DATE: _DATE,
        PROP_MASTER_RELATION: _relation_schema(master_db_id),
        **common_properties_schema(raw_db_id),
    }


def exports_schema(raw_db_id: str) -> dict:
    """⑥ 時系列エクスポート (§6.4)。1行=1データセット (バルク配布物)。"""
    return {
        EXPORT_PROP_NAME: _TITLE,
        EXPORT_PROP_FILES: _FILES,
        EXPORT_PROP_PERIOD: _RICH_TEXT,
        EXPORT_PROP_ROW_COUNT: _NUMBER,
        EXPORT_PROP_SCHEMA_DESC: _RICH_TEXT,
        EXPORT_PROP_UPDATED_ON: _DATE,
        **common_properties_schema(raw_db_id),
    }


def job_log_schema() -> dict:
    """⑦ 収集ジョブログ (§6.4)。運用ログのため共通プロパティ対象外 (§6.3)。"""
    return {
        JOB_PROP_NAME: _TITLE,
        JOB_PROP_RUN_AT: _DATE,
        JOB_PROP_STATUS: _select_schema(JOB_STATUSES),
        JOB_PROP_PROCESSED: _NUMBER,
        JOB_PROP_FAILED: _NUMBER,
        JOB_PROP_FAILED_CODES: _RICH_TEXT,
        JOB_PROP_RUN_URL: _URL,
        JOB_PROP_DURATION: _NUMBER,
    }


# ---------------------------------------------------------------------------
# 冪等セットアップ
# ---------------------------------------------------------------------------


def missing_properties(desired: dict, existing: dict) -> dict:
    """既存DBに無いプロパティのみ返す差分計算。

    冪等性ルール: 同名プロパティが既に存在する場合は型が違っても触らない
    （既存プロパティの型変更・削除はしない）。追加のみを返す。
    """
    return {name: schema for name, schema in desired.items() if name not in existing}


@dataclass
class EnsuredDB:
    """ensure_database の結果 (後段の relation 後付け判定に使う)。"""

    key: str
    db_id: str
    prop_names: set[str] = field(default_factory=set)
    created: bool = False


class SchemaSetup:
    """親ページ配下のDB/ページを冪等に整備するヘルパー。"""

    def __init__(self, client: NotionClient, settings: Settings):
        self.client = client
        self.settings = settings
        self._children: list[dict] | None = None

    def _parent_children(self) -> list[dict]:
        if self._children is None:
            self._children = self.client.list_child_blocks(self.settings.notion_parent_page_id)
        return self._children

    def find_child_database(self, title: str) -> str | None:
        """親ページ直下の child_database をタイトル完全一致で探す。"""
        for block in self._parent_children():
            if block.get("type") == "child_database" and (
                block.get("child_database", {}).get("title") == title
            ):
                return block["id"]
        return None

    def find_child_page(self, title: str) -> str | None:
        """親ページ直下の child_page をタイトル完全一致で探す。"""
        for block in self._parent_children():
            if block.get("type") == "child_page" and (
                block.get("child_page", {}).get("title") == title
            ):
                return block["id"]
        return None

    def ensure_database(self, key: str, properties: dict) -> EnsuredDB:
        """DBを冪等に作成。既存なら不足プロパティのみ追加 (型変更・削除なし)。"""
        _env_name, title = DB_REGISTRY[key]
        db_id = self.settings.db_ids.get(key) or self.find_child_database(title)
        if db_id is None:
            resp = self.client.create_database(
                parent_page_id=self.settings.notion_parent_page_id,
                title=title,
                properties=properties,
            )
            logger.info("DB作成: %s -> %s", title, resp["id"])
            return EnsuredDB(key=key, db_id=resp["id"], prop_names=set(properties), created=True)
        existing = self.client.retrieve_database(db_id).get("properties", {})
        missing = missing_properties(properties, existing)
        if missing:
            self.client.update_database(db_id, properties=missing)
            logger.info("DB更新 (%s): 不足プロパティ追加 %s", title, sorted(missing))
        else:
            logger.info("DB既存 (%s): 変更なし", title)
        return EnsuredDB(
            key=key, db_id=db_id, prop_names=set(existing) | set(missing), created=False
        )

    def add_missing_properties(self, ensured: EnsuredDB, properties: dict) -> None:
        """確保済みDBへ不足プロパティを後付けする (⑤の「関連銘柄」relation 用)。"""
        missing = {k: v for k, v in properties.items() if k not in ensured.prop_names}
        if not missing:
            return
        self.client.update_database(ensured.db_id, properties=missing)
        ensured.prop_names |= set(missing)
        logger.info("DB更新 (%s): 後付けプロパティ %s", DB_REGISTRY[ensured.key][1], sorted(missing))


def ensure_all(client: NotionClient, settings: Settings) -> dict[str, str]:
    """7DB+データカタログを冪等に整備し {論理キー: DB ID} を返す (§6, P0)。

    作成順 (§6.2): ⑤ → ① → ②③④ → ⑥⑦。relation は dual_property のため
    ①側に逆向きプロパティが自動生成される。最後に⑤へ「関連銘柄」relation→①を
    後付けする (⑤作成時点では①が存在しないため)。
    """
    setup = SchemaSetup(client, settings)

    raw = setup.ensure_database("raw_files", raw_files_schema())
    master = setup.ensure_database("stock_master", stock_master_schema(raw.db_id))
    ensured = [
        raw,
        master,
        setup.ensure_database("prices", prices_schema(master.db_id, raw.db_id)),
        setup.ensure_database("financials", financials_schema(master.db_id, raw.db_id)),
        setup.ensure_database("disclosures", disclosures_schema(master.db_id, raw.db_id)),
        setup.ensure_database("exports", exports_schema(raw.db_id)),
        setup.ensure_database("job_log", job_log_schema()),
    ]
    # ⑤ → ① の「関連銘柄」relation を後付け (§6.4 ⑤)
    setup.add_missing_properties(raw, {RAW_PROP_RELATED_MASTER: _relation_schema(master.db_id)})

    db_ids = {e.key: e.db_id for e in ensured}
    ensure_catalog_page(client, settings, setup, db_ids)

    # ビューは公開APIで作成できないため案内のみ (§7)
    logger.info("推奨ビューは Notion UI で手動作成してください (API未対応):")
    for view in RECOMMENDED_VIEWS:
        logger.info("  - %s [%s]: %s", view["name"], DB_REGISTRY[view["db"]][1], view["setup"])

    return {key: db_ids[key] for key in DB_REGISTRY}


# ---------------------------------------------------------------------------
# データカタログページ (§7)
# ---------------------------------------------------------------------------


def _text(content: str) -> dict:
    return {"type": "text", "text": {"content": content}}


def _heading(level: int, content: str) -> dict:
    kind = f"heading_{level}"
    return {"object": "block", "type": kind, kind: {"rich_text": [_text(content)]}}


def _para(content: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [_text(content)]}}


def _bullet(content: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_text(content)]},
    }


# スキーマ辞書 (§7: 項目名・型・単位・算式・ソース・ライセンス・更新頻度)
_CATALOG_DICTIONARY: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "stock_master",
        "ソース: EDINETコードリスト (commercial-ok) / 更新頻度: 月1 (master_sync)",
        (
            "銘柄名 (title)",
            "銘柄コード (text): 証券コード4桁。ユニークキー",
            "市場区分 / 33業種 / 17業種 (select)",
            "EDINETコード (text)",
            "上場状態 (checkbox): チェック=上場中 (master_sync=コードリスト所有)",
            "状態 (select: 上場/監理/整理/上場廃止) / 上場日 (date) / 上場廃止日 (date)"
            "。一次開示由来 (tdnet_hourly が所有) で listed と二重所有を回避 (§3-7)",
            "最終データ更新日 (date)",
            "現行履歴DB ID (text) / 履歴シャード番号 (number) / 履歴行数 (number): "
            "①ページ配下の株価テクニカル履歴子DBへのポインタ (prices_daily が所有)",
            "②③④⑤への relation は dual_property により自動生成 (銘柄ページから全情報を辿れる)",
        ),
    ),
    (
        "prices",
        "ソース: yfinance/stooq+計算 (personal-only) / 更新頻度: 毎営業日19:30 (prices_daily)。"
        "土曜は reconcile_weekly が stooq と終値突合しデータ品質=要確認を更新 (値は書換えない §3-5)。"
        "②は最新スナップショット。日次の計算済みテクニカルとバリュエーションは"
        "①銘柄ページ配下の「株価テクニカル履歴」子DBへ追記 (8,000行で次シャード)。"
        "横断の全履歴OHLCVは⑥のParquet/CSVを利用",
        (
            "銘柄コード (title)",
            "始値/高値/安値/終値 (number, 円)",
            "前日比率% (number, %): 計算値 (終値/前日終値-1)×100",
            "出来高 (number, 株) / 売買代金 (number, 円) / 時価総額 (number, 円)",
            "52週高値/52週安値 (number, 円)",
            "SMA5/SMA25/SMA75/SMA200 (number, 円): 計算値 単純移動平均",
            "SMA25乖離率% (number, %): 計算値 (終値/SMA25-1)×100",
            "RSI14 (number): 計算値 RSI(14日)",
            "MACD/MACDシグナル/MACDヒストグラム (number): 計算値 MACD(12,26,9)",
            "BB+2σ/BB-2σ (number, 円): 計算値 ボリンジャーバンド(20日,2σ)",
            "ATR14 (number, 円): 計算値 ATR(14日)",
            "出来高25日平均比 (number): 計算値 出来高/25日平均出来高",
            "PER (number, 倍) / PBR (number, 倍) / 配当利回り% (number, %)",
        ),
    ),
    (
        "financials",
        "ソース: EDINET XBRL/CSV・短信XBRL (commercial-ok / factual-cite) / "
        "更新頻度: 毎営業日21:00 (edinet_daily)・毎時 (tdnet_hourly)",
        (
            "タイトル (title): 例 7203 2026/03期 本決算",
            "銘柄コード (text) / 決算期末 (date) / 開示種別 (select: 本決算/1Q/2Q/3Q/修正/予想) — 複合キー",
            "連結単体 (select) / 会計基準 (select)",
            "売上高/営業利益/経常利益/純利益 (number, 円)",
            "EPS/BPS (number, 円) / ROE%/ROA%/自己資本比率% (number, %)",
            "営業CF/投資CF/財務CF (number, 円)",
            "1株配当(実績)/1株配当(予想) (number, 円)",
            "来期予想売上高〜来期予想EPS (number): 会社予想 (短信記載値)",
            "開示日 (date)",
        ),
    ),
    (
        "disclosures",
        "ソース: TDnet+EDINET (factual-cite / commercial-ok) / 更新頻度: 平日毎時 (tdnet_hourly)・"
        "毎営業日21:00 (edinet_daily)",
        (
            "開示タイトル (title) / 開示日時 (date)",
            "書類種別 (select: 短信/有報/四半期報告/業績修正/配当修正/大量保有/自社株買い/"
            "株式分割/株式併合/上場廃止/新規上場/優待/その他)",
            "分割比率 (text: 例 1:3) / 分割係数 (number) / 効力発生日 (date): コーポレート"
            "アクション属性。人間の判断材料に留め株価の自動調整はしない (§3-7)",
            "書類管理番号 (text): docID。ユニークキー",
            "銘柄コード (text) / 取得元URL (url) / XBRL有無 (checkbox)",
        ),
    ),
    (
        "raw_files",
        "全ソース / 1行=1取得単位 (APIコール1回=1原本 §5.1)。原本+変換版を同一行に添付",
        (
            "ファイル名 (title): 命名規則 {source}_{datatype}_{scope}_{YYYYMMDD}.{ext} (§5.2)",
            "ファイル (files): 原本 (無加工) + 変換版 (_converted.csv/parquet/txt)。変換は値不変 (§5.2)",
            "データ種別 (text) / 対象銘柄コード (text, 一括は ALL) / 対象期間 (text)",
            "取得URL (url) / SHA256 (text, 重複スキップキー) / サイズ (number, bytes)",
            "変換状態 (select: 完了/失敗/対象外)",
            "関連銘柄 (relation→①)",
            "共通プロパティはソース/ライセンスタグ/データ基準日/取得日時のみ (原本relation・データ品質は持たない)",
        ),
    ),
    (
        "exports",
        "更新頻度: 週1日曜 (export_weekly)。データサイエンティスト向けバルク配布物 (§7)",
        (
            "データセット名 (title): ユニークキー",
            "ファイル (files): Parquet (第一推奨) + CSV",
            "対象期間 (text) / 行数 (number) / スキーマ説明 (text) / 更新日 (date)",
            "ライセンスタグ別にファイルを分離 (commercial-ok と personal-only は別データセット §8.2)",
        ),
    ),
    (
        "job_log",
        "1行=1ジョブ実行。運用ログのため共通プロパティ対象外 (§6.3)",
        (
            "ジョブ名 (title) / 実行日時 (date)",
            "ステータス (select: 成功/一部失敗/失敗)",
            "処理件数/失敗件数 (number) / 失敗銘柄 (text)",
            "GitHub Run URL (url) / 所要時間(秒) (number)",
        ),
    ),
)


def catalog_blocks(db_ids: dict[str, str]) -> list[dict]:
    """データカタログページの本文ブロック (§7)。"""
    blocks: list[dict] = [
        _heading(1, "データカタログ"),
        _para(
            "本ページは「株式情報」配下の全データベースのスキーマ辞書・ライセンス・更新頻度・"
            "利用ガイドをまとめたものです。"
        ),
        _para(
            "免責: 本ページおよび配下の全データは情報提供のみを目的としており、投資助言ではありません。"
            "投資判断は自己責任で行ってください。"
        ),
        _heading(2, "ライセンスタグ (§2.2)"),
        _para("全データ行・全原本ファイルにライセンスタグが付与されています。"),
        _bullet("commercial-ok: 商用・再配布可 (出典記載条件)。EDINET由来および commercial-ok のみから算出した計算値"),
        _bullet("factual-cite: 事実データの抽出利用可。原文は内部保管とし、メタデータ+原文リンクのみ公開可"),
        _bullet("personal-only: 私的利用限定 (yfinance/stooq/JPXサイト統計/日証金)。公開・商用組込は禁止。日証金は規約で第三者提供を明文禁止"),
        _para(
            "注意: 公開ページ・エクスポートに全量を流せるのは commercial-ok のみです。"
            "factual-cite はメタデータ+リンクに限り、personal-only は非公開ビューに隔離してください。"
            "計算値は入力のうち最も厳しいタグを継承します (汚染防止ルール)。"
        ),
        _heading(2, "出典表記 (§2.1 規約条件)"),
        *[_bullet(attribution) for attribution in ATTRIBUTION.values()],
        _heading(2, "利用者別クイックスタート (§7)"),
        _heading(3, "投資家 (見る人)"),
        _bullet("①銘柄マスタの銘柄ページを開くと、株価・財務・開示・原本がリレーションで全部辿れます"),
        _bullet("同ページ配下の「株価テクニカル履歴」に、その銘柄の営業日ごとのテクニカルとバリュエーションが残ります"),
        _bullet("推奨ビュー (高ROEランキング/業種別/直近開示/決算カレンダー) でスクリーニングできます"),
        _heading(3, "データサイエンティスト"),
        _bullet("⑥時系列エクスポートDBから全銘柄×全期間の Parquet (第一推奨) / CSV を直接ダウンロード"),
        _bullet("各原本行 (⑤) にも機械可読な変換版 (CSV/Parquet) が併置されています"),
        _bullet("変換は値を一切変更していません (型変換・縦持ち化・文字コード正規化のみ §5.2)"),
        _heading(3, "開発者 / API利用者"),
        _bullet("Notion API でそのまま取得できます: POST https://api.notion.com/v1/databases/{database_id}/query"),
        _bullet("Python例: notion_client.Client(auth=TOKEN).databases.query(database_id=...)"),
        *[_bullet(f"DB ID ({DB_REGISTRY[key][1]}): {db_id}") for key, db_id in db_ids.items()],
        _heading(2, "推奨ビュー (手動作成手順)"),
        _para(
            "Notion の公開 REST API はビュー作成に未対応のため、以下のビューは Notion UI から"
            "手動で作成してください。"
        ),
        *[
            _bullet(f"{view['name']} [{DB_REGISTRY[view['db']][1]}]: {view['setup']}")
            for view in RECOMMENDED_VIEWS
        ],
        _heading(2, "共通プロパティ (§6.3 真実性・コンプラ担保)"),
        _bullet("ソース (select): EDINET/TDnet/yfinance/stooq/JPX/日証金/計算"),
        _bullet("ライセンスタグ (select): commercial-ok/factual-cite/personal-only"),
        _bullet("データ基準日 (date): その値が指す時点 / 取得日時 (date): パイプラインが取得した時刻"),
        _bullet("原本 (relation→⑤): 由来する原本ファイル行。どの値も原本まで遡れます (§3-3)"),
        _bullet("データ品質 (select): 正常/要確認(突合乖離)/欠損あり。取得失敗は空欄=欠損のまま (§3-1)"),
        _para("適用範囲: ①〜④⑥は全共通プロパティ。⑤はソース/ライセンスタグ/データ基準日/取得日時のみ。⑦は対象外。"),
        _heading(2, "スキーマ辞書"),
    ]
    for key, summary, items in _CATALOG_DICTIONARY:
        blocks.append(_heading(3, DB_REGISTRY[key][1]))
        blocks.append(_para(summary))
        blocks.extend(_bullet(item) for item in items)
    return blocks


_BLOCK_CHUNK = 80  # Notion API は1リクエスト最大100ブロック


def ensure_catalog_page(
    client: NotionClient, settings: Settings, setup: SchemaSetup, db_ids: dict[str, str]
) -> str:
    """データカタログページを冪等に作成 (子ページのタイトル一致で既存検出)。

    作成直後の重複収束 (#13) はしない: 手動のセットアップ CLI からだけ呼ばれ、
    定期ジョブと並行しない。万一重複しても静的な説明ページで値は割れない。
    """
    existing = setup.find_child_page(CATALOG_TITLE)
    if existing:
        logger.info("データカタログ既存: %s (内容は変更しない)", existing)
        return existing
    blocks = catalog_blocks(db_ids)
    page = client.create_page(
        parent={"type": "page_id", "page_id": settings.notion_parent_page_id},
        properties={"title": {"title": [_text(CATALOG_TITLE)]}},
        children=blocks[:_BLOCK_CHUNK],
    )
    for i in range(_BLOCK_CHUNK, len(blocks), _BLOCK_CHUNK):
        client.append_block_children(page["id"], blocks[i : i + _BLOCK_CHUNK])
    logger.info("データカタログ作成: %s", page["id"])
    return page["id"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jp_stock_pipeline.notion.schema",
        description="Notion 7DB+データカタログの冪等セットアップ (§6, P0)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Notionへ書き込まず操作を記録のみ")
    parser.add_argument(
        "--json-out",
        default="db_ids.json",
        help="DB IDの書き出し先 (既定: db_ids.json。dry-run時は合成IDのため書き出しスキップ)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings(dry_run=True if args.dry_run else None)
    client = NotionClient(
        settings.notion_token, rps=settings.notion_rps, dry_run=settings.dry_run
    )
    db_ids = ensure_all(client, settings)

    payload = json.dumps(db_ids, ensure_ascii=False, indent=2)
    print(payload)
    if settings.dry_run:
        logger.info("dry-run のため %s への書き出しはスキップ (合成IDの混入防止)", args.json_out)
    else:
        Path(args.json_out).write_text(payload + "\n", encoding="utf-8")
        logger.info("DB IDを書き出し: %s", args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
