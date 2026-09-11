"""TDnet 適時開示からの株主優待分類。

みんかぶの利用規約が取得・蓄積・公開を明文で禁じているため、⑨株主優待は
**一次開示から自前で構築する**。優待掲載文の実体は発行会社の IR 文なので、
TDnet から取れば規約の適用外であり factual-cite として公開面にも出せる。

ここで使うタイトルはすべて data/raw に実在する TDnet 開示の実タイトル。
"""

from __future__ import annotations

import pytest

from jp_stock_pipeline.collectors import tdnet_yanoshin as T
from jp_stock_pipeline.notion import schema as S


class TestClassification:
    @pytest.mark.parametrize(
        ("title", "action"),
        [
            ("株主優待制度の新設に関するお知らせ", T.YUTAI_ACTION_NEW),
            ("株主優待制度導入に関するお知らせ", T.YUTAI_ACTION_NEW),
            ("株主優待制度の変更に関するお知らせ", T.YUTAI_ACTION_CHANGE),
            ("株主優待制度の変更（拡充）に関するお知らせ", T.YUTAI_ACTION_CHANGE),
            ("株主優待品一部変更のお知らせ", T.YUTAI_ACTION_CHANGE),
            ("株主優待制度の廃止に関するお知らせ", T.YUTAI_ACTION_ABOLISH),
            ("株主優待制度の再開に関するお知らせ", T.YUTAI_ACTION_RESUME),
            ("創業30周年記念株主優待の実施に関するお知らせ", T.YUTAI_ACTION_COMMEMORATIVE),
        ],
    )
    def test_real_titles_are_classified_with_their_action(self, title, action):
        assert T.classify_title(title) == T.DOC_TYPE_YUTAI
        assert T.parse_yutai_action(title) == action

    def test_commemorative_wins_over_new(self):
        """「記念…優待の実施」を『新設』にしない（実施 は新設のキーワードでもある）。"""
        title = "創業30周年記念株主優待の実施に関するお知らせ"
        assert T.parse_yutai_action(title) == T.YUTAI_ACTION_COMMEMORATIVE


class TestPriority:
    """複合開示では、投資判断上より重い種別を優先する。"""

    def test_dividend_revision_wins_over_yutai(self):
        """実開示に3件あった複合パターン。配当修正の方が重い。"""
        title = "2026年５月期の期末配当予想の修正（無配）及び株主優待制度の廃止に関するお知らせ"
        assert T.classify_title(title) == T.DOC_TYPE_DIVIDEND_REVISION

    def test_yutai_action_is_still_available_on_a_combined_disclosure(self):
        """種別が配当修正でも、優待の区分は失われない。"""
        title = "2026年５月期の期末配当予想の修正（無配）及び株主優待制度の廃止に関するお知らせ"
        assert T.parse_yutai_action(title) == T.YUTAI_ACTION_ABOLISH

    def test_split_wins_over_yutai(self):
        """分割は比率を伴う一次コーポレートアクションなので優先する。"""
        title = "株式分割、株式分割に伴う定款の一部変更および 株主優待制度の変更に関するお知らせ"
        assert T.classify_title(title) == T.DOC_TYPE_SPLIT
        assert T.parse_yutai_action(title) == T.YUTAI_ACTION_CHANGE

    def test_tanshin_is_unaffected(self):
        assert T.classify_title("2026年3月期 決算短信〔日本基準〕(連結)") == T.DOC_TYPE_TANSHIN


class TestNoGuessing:
    def test_ambiguous_title_yields_no_action(self):
        """区分が読み取れないものは空文字。推測で埋めない (§3-1)。"""
        assert T.classify_title("株主優待制度に関するお知らせ") == T.DOC_TYPE_YUTAI
        assert T.parse_yutai_action("株主優待制度に関するお知らせ") == T.YUTAI_ACTION_UNKNOWN

    def test_non_yutai_title_yields_no_action(self):
        assert T.parse_yutai_action("2026年3月期 決算短信") == T.YUTAI_ACTION_UNKNOWN


class TestSchemaOption:
    def test_yutai_is_a_valid_document_type(self):
        """④の select に無い値を書くと Notion 側で弾かれる。"""
        assert T.DOC_TYPE_YUTAI in S.DOC_TYPES
