"""classify_title（④ 書類種別判定）のテスト。

タイトル文字列はすべて実在の開示タイトル (§3-6):
- 主に 2026-06-10 実取得の tdnet/yanoshin_list_20260610.json からの引用
- フィクスチャ日に存在しなかった種別（大量保有・有報・四半期報告）は
  法定書類名に基づく定型タイトルを使用（コメントで明示）
"""

from __future__ import annotations

import json

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
