"""ライセンスタグ定義・継承ルール・公開フィルタ (DESIGN.md §2.2)。

不変条件:
- 全データ行・全原本ファイルにライセンスタグを必須付与する
- 計算値は入力のうち「最も厳しい」タグを継承する（personal-only の汚染防止）
- 公開導線に流せるのは commercial-ok（全量）と factual-cite（メタデータ+リンクのみ）
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from .models import Source


class LicenseTag(StrEnum):
    COMMERCIAL_OK = "commercial-ok"  # 商用・再配布可（出典記載条件）
    FACTUAL_CITE = "factual-cite"  # 事実データの抽出利用可・原文は内部保管
    PERSONAL_ONLY = "personal-only"  # 私的利用限定。公開・商用組込禁止


# 厳しさの順序 (大きいほど制約が強い)
_STRICTNESS: dict[LicenseTag, int] = {
    LicenseTag.COMMERCIAL_OK: 0,
    LicenseTag.FACTUAL_CITE: 1,
    LicenseTag.PERSONAL_ONLY: 2,
}

# ソース別デフォルトタグ (§2.1 の評価結果)。
# Source.CALC は入力依存のため定義しない → 必ず inherit() を使う。
SOURCE_LICENSE: dict[Source, LicenseTag] = {
    Source.EDINET: LicenseTag.COMMERCIAL_OK,
    Source.TDNET: LicenseTag.FACTUAL_CITE,
    Source.YFINANCE: LicenseTag.PERSONAL_ONLY,
    Source.STOOQ: LicenseTag.PERSONAL_ONLY,
    Source.JPX: LicenseTag.PERSONAL_ONLY,
    # 日証金の利用規約は「私的利用の範囲を超えて利用することはできず…第三者の
    # 利用に供することを固く禁じます」と明文で定める。JPX より厳しい。
    Source.JSF: LicenseTag.PERSONAL_ONLY,
}


def source_license(source: Source) -> LicenseTag:
    """ソースのデフォルトライセンスタグを返す。計算値には使えない。"""
    if source is Source.CALC:
        raise ValueError(
            "計算値のライセンスタグは入力データから inherit() で継承すること (§2.2)"
        )
    return SOURCE_LICENSE[source]


def inherit(tags: Iterable[LicenseTag]) -> LicenseTag:
    """入力タグ群から最も厳しいタグを継承する (§2.2 汚染防止ルール)。"""
    tags = list(tags)
    if not tags:
        raise ValueError("継承元タグが空。計算値には必ず入力データのタグを渡すこと")
    return max(tags, key=lambda t: _STRICTNESS[t])


def is_publishable(tag: LicenseTag) -> bool:
    """公開ビュー・エクスポートに全量を流してよいか。"""
    return tag is LicenseTag.COMMERCIAL_OK


def is_metadata_publishable(tag: LicenseTag) -> bool:
    """メタデータ+原文リンクのみなら公開してよいか (factual-cite を含む)。"""
    return tag in (LicenseTag.COMMERCIAL_OK, LicenseTag.FACTUAL_CITE)


# 出典表記 (EDINET は規約条件 §2.1)
ATTRIBUTION: dict[Source, str] = {
    Source.JSF: "出典: 日本証券金融（JSF）貸借取引情報。私的利用に限定し第三者へ提供しない。",
    Source.EDINET: "出典: EDINET（金融庁）。本データは EDINET 公表情報を編集・加工して作成。",
    Source.TDNET: "出典: TDnet（東京証券取引所 適時開示情報閲覧サービス）。原文は各社開示資料。",
}

# 商用化判断時のチェックリスト (§2.2。公開・商用ローンチ前に必ず実行)
COMMERCIALIZATION_CHECKLIST: tuple[str, ...] = (
    "JPX TDnet API（有料）の契約可否を確認する",
    "商用利用可能な株価データベンダーを選定する（yfinance/stooq を置換。例: J-Quants Pro 等の商用契約。廃止した無料APIとは別物）",
    "公開対象が commercial-ok / factual-cite(メタデータのみ) でフィルタされていることを検証する",
    "各ソースの規約原文を再確認する（必要に応じて専門家確認）",
    "出典表記（EDINET 等）と編集・加工の明記が全公開面に存在することを確認する",
    "免責（情報提供のみ・投資助言ではない）が公開面に明記されていることを確認する",
)
