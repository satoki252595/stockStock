"""classify_title（④ 書類種別判定）のテスト。

タイトル文字列はすべて実在の開示タイトル (§3-6):
- 主に 2026-06-10 実取得の tdnet/yanoshin_list_20260610.json からの引用
- フィクスチャ日に存在しなかった種別（大量保有・有報・四半期報告）は
  法定書類名に基づく定型タイトルを使用（コメントで明示）
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from jp_stock_pipeline.collectors import tdnet_yanoshin as ty
from jp_stock_pipeline.collectors.tdnet_yanoshin import classify_title

from conftest import fixture_path

ALL_DOC_TYPES = {
    ty.DOC_TYPE_TANSHIN,
    ty.DOC_TYPE_FORECAST_REVISION,
    ty.DOC_TYPE_DIVIDEND_REVISION,
    ty.DOC_TYPE_BUYBACK,
    ty.DOC_TYPE_LARGE_HOLDING,
    ty.DOC_TYPE_ANNUAL_REPORT,
    ty.DOC_TYPE_QUARTERLY_REPORT,
    ty.DOC_TYPE_YUTAI,
    ty.DOC_TYPE_SPLIT,
    ty.DOC_TYPE_CONSOLIDATION,
    ty.DOC_TYPE_DELISTING,
    ty.DOC_TYPE_NEW_LISTING,
    ty.DOC_TYPE_OTHER,
}


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # --- 短信（フィクスチャ実例） ---
        ("2026年４月期決算短信〔日本基準〕(連結)", ty.DOC_TYPE_TANSHIN),
        ("2027年１月期　第１四半期決算短信［日本基準］（非連結）", ty.DOC_TYPE_TANSHIN),
        ("2026年10月期第２四半期（中間期）決算短信〔日本基準〕(非連結)", ty.DOC_TYPE_TANSHIN),
        # --- 業績修正（フィクスチャ実例） ---
        ("通期業績予想の修正に関するお知らせ", ty.DOC_TYPE_FORECAST_REVISION),
        ("2027年１月期業績予想の修正に関するお知らせ", ty.DOC_TYPE_FORECAST_REVISION),
        # 業績+配当の同時修正は優先度ルールで業績修正（フィクスチャ実例）
        ("通期連結業績予想及び配当予想の修正に関するお知らせ", ty.DOC_TYPE_FORECAST_REVISION),
        # --- 配当修正（フィクスチャ実例） ---
        ("期末配当予想の修正（増配）に関するお知らせ", ty.DOC_TYPE_DIVIDEND_REVISION),
        ("2026年9月期配当予想の修正（増配）に関するお知らせ", ty.DOC_TYPE_DIVIDEND_REVISION),
        # --- 自社株買い = 自己株式の「取得」（フィクスチャ実例） ---
        ("自己株式の取得状況に関するお知らせ", ty.DOC_TYPE_BUYBACK),
        (
            "自己株式の取得及び自己株式立会外買付取引(ToSTNeT-3)による自己株式の買付けに関するお知らせ",
            ty.DOC_TYPE_BUYBACK,
        ),
        # --- 大量保有 / 有報 / 四半期報告（法定書類名に基づく定型タイトル） ---
        ("大量保有報告書の提出に関するお知らせ", ty.DOC_TYPE_LARGE_HOLDING),
        ("有価証券報告書の提出に関するお知らせ", ty.DOC_TYPE_ANNUAL_REPORT),
        ("四半期報告書の提出に関するお知らせ", ty.DOC_TYPE_QUARTERLY_REPORT),
        # --- コーポレートアクション (§ Phase2) ---
        ("株式分割に関するお知らせ", ty.DOC_TYPE_SPLIT),
        ("株式分割及び定款の一部変更に関するお知らせ", ty.DOC_TYPE_SPLIT),
        ("株式併合及び単元株式数の変更に関するお知らせ", ty.DOC_TYPE_CONSOLIDATION),
        # 比率を伴う本物の分割は、配当修正を併記しても分割扱い
        ("株式分割（1株を3株に分割）及び配当予想の修正に関するお知らせ", ty.DOC_TYPE_SPLIT),
        # 分割を“言及”するだけで本体は修正（比率なし）→ 修正へ回す (§ Phase2)
        ("株式分割に伴う配当予想の修正に関するお知らせ", ty.DOC_TYPE_DIVIDEND_REVISION),
        ("株式併合に伴う業績予想の修正に関するお知らせ", ty.DOC_TYPE_FORECAST_REVISION),
        # フィクスチャ実例の上場廃止タイトル（本人の確定的な廃止後開示）
        ("上場廃止後の当社株式の取り扱いに関するお知らせ", ty.DOC_TYPE_DELISTING),
        ("東京証券取引所グロース市場への新規上場に関するお知らせ", ty.DOC_TYPE_NEW_LISTING),
        # --- 上場廃止/新規上場の偽陽性抑制（①状態を倒さない。否定/回避/段階/解除/派生/第三者）---
        # まだ上場継続(監理/整理段階) → 状態を倒さない
        ("上場廃止に係る猶予期間入りに関するお知らせ", ty.DOC_TYPE_OTHER),
        # 猶予期間の解消＝上場維持の良いニュース
        ("上場廃止に係る猶予期間入りの解消に関するお知らせ", ty.DOC_TYPE_OTHER),
        # 廃止基準への抵触回避＝健全
        ("上場廃止基準への抵触回避に関するお知らせ", ty.DOC_TYPE_OTHER),
        # 「該当しない」否定
        ("当社株式の上場廃止には該当しない旨のお知らせ", ty.DOC_TYPE_OTHER),
        # 「おそれ」＝まだ廃止確定でない
        ("上場廃止のおそれに関するお知らせ", ty.DOC_TYPE_OTHER),
        # 第三者(子会社)の上場廃止＝開示主体(親)は上場継続
        ("連結子会社の上場廃止に関するお知らせ", ty.DOC_TYPE_OTHER),
        ("子会社の新規上場に関するお知らせ", ty.DOC_TYPE_OTHER),
        # ただし「完全子会社化に伴う上場廃止」は開示主体自身の本物の廃止(任意廃止の主流)
        ("株式交換による完全子会社化に伴う上場廃止に関するお知らせ", ty.DOC_TYPE_DELISTING),
        # 普通株は継続し別証券クラス(優先株式等)のみ廃止＝普通株の状態を倒さない
        ("当社優先株式の上場廃止に関するお知らせ", ty.DOC_TYPE_OTHER),
        ("新株予約権付社債の上場廃止に関するお知らせ", ty.DOC_TYPE_OTHER),
        # 「上場廃止に伴う配当予想の修正」は本体が配当修正
        ("上場廃止に伴う配当予想の修正に関するお知らせ", ty.DOC_TYPE_DIVIDEND_REVISION),
        # --- その他（フィクスチャ実例。誤検知しやすいタイトルを含む） ---
        ("事業譲受に関するお知らせ", ty.DOC_TYPE_OTHER),
        ("ＥＴＦの収益分配のお知らせ", ty.DOC_TYPE_OTHER),
        # 「業績予想」を含むが「修正」ではない
        ("通期連結業績予想と実績の差異に関するお知らせ", ty.DOC_TYPE_OTHER),
        # 「自己株式」を含むが「取得」ではない
        ("自己株式の消却に関するお知らせ", ty.DOC_TYPE_OTHER),
        # 「配当」を含むが「修正」ではない
        ("連結子会社からの配当金受領に関するお知らせ", ty.DOC_TYPE_OTHER),
    ],
)
def test_classify_title(title, expected):
    assert classify_title(title) == expected


def test_classify_は定義済み種別のみ返す():
    """実フィクスチャの全180タイトルが既知の書類種別に分類される。"""
    payload = json.loads(
        fixture_path("tdnet/yanoshin_list_20260610.json").read_text(encoding="utf-8")
    )
    titles = [item["title"] for item in payload["items"]]
    assert len(titles) == 180
    for title in titles:
        assert classify_title(title) in ALL_DOC_TYPES


def test_classify_フィクスチャ内の決算短信は全て短信():
    payload = json.loads(
        fixture_path("tdnet/yanoshin_list_20260610.json").read_text(encoding="utf-8")
    )
    tanshin = [t for t in (i["title"] for i in payload["items"]) if ty.KW_TANSHIN in t]
    assert len(tanshin) > 0
    for title in tanshin:
        assert classify_title(title) == ty.DOC_TYPE_TANSHIN


# ---------------------------------------------------------------------------
# コーポレートアクション属性の抽出 (§ Phase2/4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "ratio", "factor"),
    [
        ("株式分割（1株を3株に分割）に関するお知らせ", "1:3", 3.0),
        ("普通株式1株につき2株の割合をもって分割いたします", "1:2", 2.0),
        ("株式分割（1：5）に関するお知らせ", "1:5", 5.0),  # 全角コロン
        ("株式併合（5株を1株に併合）に関するお知らせ", "5:1", 0.2),
        # 比率がタイトルに無ければ推定しない (§3-1)
        ("株式分割に関するお知らせ", None, None),
    ],
)
def test_parse_split_terms(title, ratio, factor):
    r, f = ty.parse_split_terms(title)
    assert r == ratio
    if factor is None:
        assert f is None
    else:
        assert f == pytest.approx(factor)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("株式分割（効力発生日 2026年4月1日）に関するお知らせ", date(2026, 4, 1)),
        ("株式分割（効力発生日を2026年10月1日とする）", date(2026, 10, 1)),
        ("株式分割に関するお知らせ", None),  # 効力発生日がタイトルに無い
        # 後続の別日付(基準日)を誤って掴まない＝誤った権威的日付より欠損が正しい (§3-1)
        ("効力発生日に先立つ基準日を2026年3月31日とする株式分割", None),
    ],
)
def test_parse_effective_date(title, expected):
    assert ty.parse_effective_date(title) == expected


def test_corporate_action_attrs_only_for_relevant_types():
    """比率抽出は分割/併合のみ。上場廃止/新規上場では比率は付かない。"""
    ratio, factor, _eff = ty.corporate_action_attrs(
        "株式分割（1株を2株に分割）", ty.DOC_TYPE_SPLIT
    )
    assert (ratio, factor) == ("1:2", 2.0)
    # 上場廃止に「1株を2株」と書いてあっても分割比率としては拾わない
    ratio2, factor2, _ = ty.corporate_action_attrs(
        "上場廃止後の取り扱い", ty.DOC_TYPE_DELISTING
    )
    assert ratio2 is None and factor2 is None


class TestParticleInsertedSplitConsolidation:
    """「株式の分割」のように助詞「の」を挟む実開示表記を拾う。

    「株式分割」の単純部分一致では拾えなかった実例が
    2026-09-11時点のTDnetフィクスチャ全3,137タイトル中に1件存在した
    （「株式の分割、定款の一部変更、期末配当予想の修正 及び株主優待制度の
    変更に関するお知らせ」）。この1件は比率を伴わず配当修正語も併記されて
    いるため、修正後も分類結果自体は「配当修正」のままで変わらない（既存の
    優先順位設計どおり）。この修正で意味を持つのは、将来「株式の分割（1株を
    3株に分割）」のように**比率つき・修正語なし**で「の」を挟む本物の分割
    告知が来たときに、その他へ落ちず正しく分割として拾われること。
    """

    def test_mentions_split_detects_particle_form(self):
        assert ty._mentions_split("株式の分割に関するお知らせ") is True
        assert ty._mentions_split("株式分割に関するお知らせ") is True
        assert ty._mentions_split("株式の分割") is True

    def test_mentions_consolidation_detects_particle_form(self):
        assert ty._mentions_consolidation("株式の併合に関するお知らせ") is True
        assert ty._mentions_consolidation("株式併合に関するお知らせ") is True

    def test_unrelated_title_does_not_mention_split(self):
        assert ty._mentions_split("2026年3月期 決算短信〔日本基準〕(連結)") is False

    def test_particle_form_with_ratio_and_no_revision_is_classified_as_split(self):
        """比率つき・修正語なしなら本物の分割告知として拾う（本テストの主眼）。"""
        title = "株式の分割（1株を3株に分割）に関するお知らせ"
        assert classify_title(title) == ty.DOC_TYPE_SPLIT
        assert ty.parse_split_terms(title) == ("1:3", 3.0)

    def test_particle_form_with_revision_language_still_prefers_revision(self):
        """実開示: 修正語併記・比率なしなら既存の優先順位どおり配当修正のまま。"""
        title = (
            "株式の分割、定款の一部変更、期末配当予想の修正 "
            "及び株主優待制度の変更に関するお知らせ"
        )
        assert classify_title(title) == ty.DOC_TYPE_DIVIDEND_REVISION
        # ただし言及自体は正しく検出できている（分割データの欠落ではなく
        # 優先順位判断の結果であることを区別する）
        assert ty._mentions_split(title) is True

    def test_particle_form_consolidation_with_ratio(self):
        title = "株式の併合（5株を1株に併合）に関するお知らせ"
        assert classify_title(title) == ty.DOC_TYPE_CONSOLIDATION
        assert ty.parse_split_terms(title) == ("5:1", 0.2)

    def test_real_tdnet_titles_are_unaffected(self):
        """実フィクスチャ全180タイトルで、修正前後の分類が一致すること。"""
        payload = json.loads(
            fixture_path("tdnet/yanoshin_list_20260610.json").read_text(encoding="utf-8")
        )
        titles = [item["title"] for item in payload["items"]]
        for title in titles:
            assert classify_title(title) in ALL_DOC_TYPES
