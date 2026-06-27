"""共通モデル: ソース・品質・原本アーティファクト・来歴・正規化レコード契約。

DESIGN.md §3-3: 全行に「ソース」「データ基準日」「取得日時」「原本リレーション」を
持たせ、どの値も原本まで遡れる状態を保証する。
§3-1: 取得できなかった値は None のまま保持する（ダミー・推定・補間の生成禁止）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .licensing import LicenseTag

JST = timezone(timedelta(hours=9), name="JST")


def now_jst() -> datetime:
    return datetime.now(tz=JST)


class Source(StrEnum):
    """データソース (Notion「ソース」select の選択肢と一致させる)。"""

    EDINET = "EDINET"
    TDNET = "TDnet"
    YFINANCE = "yfinance"
    STOOQ = "stooq"
    JPX = "JPX"
    CALC = "計算"


class DataQuality(StrEnum):
    """§6.3 データ品質 select。"""

    OK = "正常"
    NEEDS_REVIEW = "要確認"
    MISSING = "欠損あり"


class ConvertStatus(StrEnum):
    """⑤ 原本ファイルDB「変換状態」select (§5.2)。"""

    DONE = "完了"
    FAILED = "失敗"
    NOT_APPLICABLE = "対象外"


@dataclass
class Provenance:
    """全構造化行に必須の来歴 (§6.3 共通プロパティに対応)。"""

    source: Source
    license_tag: "LicenseTag"
    data_date: date | None  # データ基準日（その値が指す時点）
    fetched_at: datetime  # 取得日時
    raw_page_id: str | None = None  # ⑤ 原本ファイルDB の行ID（リレーション先）
    quality: DataQuality = DataQuality.OK


@dataclass
class RawArtifact:
    """1取得単位の原本 (§5.1: APIコール1回/ダウンロード1回 = 1原本 = ⑤の1行)。"""

    source: Source
    datatype: str  # 例: "documents_list", "xbrl", "codelist", "daily_prices"
    scope: str  # 銘柄コード or "ALL"
    data_date: date | None
    fetched_at: datetime
    url: str
    local_path: Path
    sha256: str
    size_bytes: int
    license_tag: "LicenseTag"
    converted_paths: list[Path] = field(default_factory=list)
    convert_status: ConvertStatus = ConvertStatus.NOT_APPLICABLE
    notion_page_id: str | None = None  # ⑤ へのアップロード完了後に設定される

    @property
    def filename(self) -> str:
        return self.local_path.name


# ---------------------------------------------------------------------------
# 正規化レコード契約（transform → notion.upsert 間のインターフェース）
# 数値系はすべて Optional: 取得できなかった値は None のまま渡し、
# Notion 側では空欄になる (§3-1)。
# ---------------------------------------------------------------------------


@dataclass
class StockMasterRecord:
    """① 銘柄マスタ (キー=銘柄コード)。"""

    code: str  # 証券コード 例 "7203"
    name: str
    provenance: Provenance
    market: str | None = None  # 市場区分
    sector33: str | None = None  # 33業種
    sector17: str | None = None  # 17業種
    edinet_code: str | None = None
    listed: bool = True  # 上場状態 (checkbox: 現在上場しているか)
    # ライフサイクル (§: コーポレートアクション対応)。日付は一次開示で判明した場合のみ
    # 設定し、不明なら None のまま (§3-1 推定禁止)。
    status: str | None = None  # 状態: 上場/監理/整理/上場廃止
    listing_date: date | None = None  # 上場日 (新規上場開示で判明した場合)
    delisting_date: date | None = None  # 上場廃止日 (上場廃止開示で判明した場合)


@dataclass
class PriceTechnicalRecord:
    """② 株価テクニカル (キー=銘柄コード。毎営業日upsertの最新スナップショット)。"""

    code: str
    provenance: Provenance
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    prev_close_pct: float | None = None  # 前日比率%
    volume: float | None = None
    turnover: float | None = None  # 売買代金
    market_cap: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    sma5: float | None = None
    sma25: float | None = None
    sma75: float | None = None
    sma200: float | None = None
    sma25_dev_pct: float | None = None  # SMA25乖離率%
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    bb_upper: float | None = None  # BB+2σ (20日)
    bb_lower: float | None = None  # BB-2σ (20日)
    atr14: float | None = None
    volume_ratio25: float | None = None  # 出来高25日平均比
    per: float | None = None
    pbr: float | None = None
    dividend_yield_pct: float | None = None


@dataclass
class FinancialSummaryRecord:
    """③ 財務サマリ (キー=銘柄コード×決算期末×開示種別)。"""

    code: str
    fiscal_period_end: date  # 決算期末
    disclosure_type: str  # 本決算/1Q/2Q/3Q/修正/予想
    provenance: Provenance
    consolidated: str | None = None  # 連結/単体
    accounting_standard: str | None = None  # 日本基準/IFRS/US-GAAP 等
    net_sales: float | None = None
    operating_income: float | None = None
    ordinary_income: float | None = None
    net_income: float | None = None
    eps: float | None = None
    bps: float | None = None
    roe_pct: float | None = None
    roa_pct: float | None = None
    equity_ratio_pct: float | None = None
    cf_operating: float | None = None
    cf_investing: float | None = None
    cf_financing: float | None = None
    dps_actual: float | None = None  # 1株配当(実績)
    dps_forecast: float | None = None  # 1株配当(予想)
    forecast_net_sales: float | None = None  # 来期予想
    forecast_operating_income: float | None = None
    forecast_ordinary_income: float | None = None
    forecast_net_income: float | None = None
    forecast_eps: float | None = None
    disclosed_at: datetime | None = None  # 開示日


@dataclass
class DisclosureRecord:
    """④ 開示書類 (キー=書類管理番号 docID等)。"""

    doc_id: str  # TDnet/EDINET の書類管理番号
    title: str
    disclosed_at: datetime
    provenance: Provenance
    code: str | None = None  # 銘柄コード (4桁基準。全市場一括等は None)
    doc_type: str = "その他"  # 短信/有報/四半期報告/業績修正/配当修正/大量保有/自社株買い/
    # 株式分割/株式併合/上場廃止/新規上場/その他
    source_url: str | None = None
    has_xbrl: bool = False
    # コーポレートアクションの構造化属性 (§: 分割/併合のみ設定。タイトルから抽出
    # できた場合のみ。不明なら None で原文リンクに委ねる §3-1)。Phase4 の価格調整は
    # split_factor を一次情報として用いる。
    split_ratio: str | None = None  # 例 "1:3" (1株→3株)。表示用の人間可読比率
    split_factor: float | None = None  # 例 3.0 (分割) / 0.2 (5株→1株併合)。新株数/旧株数
    effective_date: date | None = None  # 効力発生日 (権利タイミング。タイトル記載時のみ)
