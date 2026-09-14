"""①母集団ガードの移植テスト（移行 P4a / G-core-4 / D4）。

移植元は kabulab-cf の src/cron/universe.ts:82-120。本テストは
「同じ入力で同じように止まる」ことを固定する。境界値ちょうどは通る側。
(c) の分母と (d2) だけは移植元から意図的に変えてあり（D4）、その意図も
ここで固定する。

現行の実測値（2026-09-12）:
  core_stocks total 3,818 / active 3,715 / instrument_type は 3,818 行すべて NULL
  data_j.xls (2026-05-31) 全 4,451 行 / 4文字内国株 3,728 件

**基準日を書かない数字を置かないこと。** data_j は毎月差し替わるので、基準日
抜きの「+707」「+725」はどちらも正しく見えて食い違う。実際に
`docs/CF-CANONICAL-DESIGN.md` P4b 節（2026-08-31 版で +725 / active 4,440）と
本テストの docstring（2026-05-31 版で +707 / active 4,422）が食い違っていたので、
下の定数へ基準日ごとに分けて持たせ、P4b の判定には設計書と同じ 2026-08-31 版を使う。
"""

from __future__ import annotations

import pytest

from jp_stock_pipeline.cloud_store.guards import GuardError
from jp_stock_pipeline.cloud_store.universe_guards import (
    MAX_DEACTIVATION_RATIO,
    MIN_BACKFILLED_EQUITY_ROWS,
    MIN_EQUITY_ROWS,
    MIN_EXISTING_COVERAGE,
    MIN_JPX_ROWS,
    assert_instrument_type_backfilled,
    assert_population_sane,
    assert_universe_coverage,
    coverage_denominator,
    instrument_type_backfilled,
    is_valid_stock_code,
    normalize_stock_code,
    should_deactivate_universe_code,
)

ACTIVE = 3_715  # 本番 core_stocks の is_active=1（2026-09-12 実測）
TOTAL = 3_818  # 同・全行
EQUITY = 3_728  # data_j.xls 2026-05-31 の isListedEquity
RAW = 4_451  # 同上・全行

# P4b の正本は現行配布 data_j.xlsx（基準日 2026-08-31）。
# raw 4,441 / isListedEquity 3,700 / 集合差 +725 → active 3,715 + 725 = 4,440。
# 旧版（2026-05-31）では +707 → 4,422 で、本テストの docstring だけが
# そちらを引いていたため設計書と食い違っていた。両方を基準日つきで持つ。
EQUITY_20260831 = 3_700
P4B_INSERT = 725
P4B_ACTIVE = ACTIVE + P4B_INSERT  # 4,440


class TestConstantsMatchSource:
    """移植元の定数と1つでもずれたら気づけるようにする。"""

    def test_定数は_universe_ts_と同じ(self) -> None:
        assert MIN_JPX_ROWS == 4_000
        assert MIN_EQUITY_ROWS == 3_000
        assert MIN_EXISTING_COVERAGE == 0.98
        assert MAX_DEACTIVATION_RATIO == 0.02


class TestGuardA:
    def test_raw_が下限未満なら止める(self) -> None:
        with pytest.raises(GuardError, match="ガード\\(a\\)"):
            assert_universe_coverage(3_999, EQUITY, ACTIVE, 0)

    def test_境界ちょうどは通る(self) -> None:
        assert_universe_coverage(4_000, EQUITY, ACTIVE, 0)


class TestGuardB:
    def test_内国株が下限未満なら止める(self) -> None:
        with pytest.raises(GuardError, match="ガード\\(b\\)"):
            assert_universe_coverage(RAW, 2_999, ACTIVE, 0)

    def test_境界ちょうどは通る(self) -> None:
        assert_universe_coverage(RAW, 3_000, 3_000, 0)


class TestGuardC:
    def test_被覆率が98パーセント未満なら止める(self) -> None:
        with pytest.raises(GuardError, match="ガード\\(c\\)"):
            assert_universe_coverage(RAW, 3_400, ACTIVE, 0)

    def test_分母は_total_ではなく_active(self) -> None:
        # active 3,715 なら 3,641 で 0.98006 → 通る。
        # total 3,818 を分母にすると 0.9537 で落ちる = 取り違えを検出できる
        assert_universe_coverage(RAW, 3_641, ACTIVE, 0)

    def test_現行の実測値は素通りする(self) -> None:
        assert_universe_coverage(RAW, EQUITY, ACTIVE, 0)

    def test_比率がちょうど98パーセントなら通る(self) -> None:
        """移植元は `< 0.98` で止める。`<=` に変えたらここが落ちる。

        (b) が先に発火しないよう equity は 3,000 以上に保つ。
        """
        assert 4_900 / 5_000 == MIN_EXISTING_COVERAGE
        assert_universe_coverage(RAW, 4_900, 5_000, 0)

    def test_比率が98パーセントをわずかに下回れば止める(self) -> None:
        with pytest.raises(GuardError, match="ガード\\(c\\)"):
            assert_universe_coverage(RAW, 4_899, 5_000, 0)

    def test_P4b_で母集団を広げても_equity_の分母なら素通りする(self) -> None:
        """D4 の本体。+725 行して active 4,440 になっても、equity の分母なら通る。

        3,700/4,440 = 0.833 で毎月 throw していた（設計書 P4b 節の実測）のは、
        分子が内国普通株なのに分母が全銘柄種別だったから。分母を
        `is_active=1 AND instrument_type='equity'` に絞れば 3,700/3,715 = 0.996。
        """
        assert_universe_coverage(
            RAW,
            EQUITY_20260831,
            P4B_ACTIVE,
            0,
            existing_equity_active_count=ACTIVE,
        )

    def test_分母が_active_全体のままなら_P4b_で発火する(self) -> None:
        """移植元のままの分母だと何が起きるかを、直した後も残して固定する。

        equity 件数を渡さない（= instrument_type 未充填・未観測）経路は
        従来の分母へ縮退するので、ここは**発火するのが正しい**。
        「充填が P4b の前提条件」という設計の表明であって誤検知ではない。
        """
        with pytest.raises(GuardError, match="ガード\\(c\\)"):
            assert_universe_coverage(RAW, EQUITY_20260831, P4B_ACTIVE, 0)

    def test_発火時のメッセージにどちらの分母かを書く(self) -> None:
        """0.833 だけ見せられても、充填漏れか本当の被覆不足かが読めない。"""
        with pytest.raises(GuardError, match="instrument_type 未観測"):
            assert_universe_coverage(RAW, EQUITY_20260831, P4B_ACTIVE, 0)
        with pytest.raises(GuardError, match="instrument_type 未充填"):
            assert_universe_coverage(
                RAW, EQUITY_20260831, P4B_ACTIVE, 0, existing_equity_active_count=0
            )
        with pytest.raises(GuardError, match="active かつ equity"):
            assert_universe_coverage(
                RAW, 3_100, P4B_ACTIVE, 0, existing_equity_active_count=3_715
            )


class TestGuardD1:
    """分子が全銘柄種別なので分母も active 全体。移植元のまま（理由は実装 docstring）。"""

    def test_対象外化が2パーセント超なら止める(self) -> None:
        # 0.02 * 3715 = 74.3 → 75 件で発火
        with pytest.raises(GuardError, match="ガード\\(d1\\)"):
            assert_universe_coverage(RAW, EQUITY, ACTIVE, 75)

    def test_境界の74件は通る(self) -> None:
        assert_universe_coverage(RAW, EQUITY, ACTIVE, 74)

    def test_比率がちょうど2パーセントなら通る(self) -> None:
        """移植元は `> 0.02` で止める。`>=` に変えたらここが落ちる。"""
        assert 60 / 3_000 == MAX_DEACTIVATION_RATIO
        assert_universe_coverage(RAW, EQUITY, 3_000, 60)

    def test_比率が2パーセントをわずかに超えれば止める(self) -> None:
        with pytest.raises(GuardError, match="ガード\\(d1\\)"):
            assert_universe_coverage(RAW, EQUITY, 3_000, 61)

    def test_equity_の対象外化がゼロでも銘柄種別横断の大量廃止は拾う(self) -> None:
        """(d1) を active 全体の分母で残した理由。

        ETF が一斉に 100 本消える事故は equity の比率では 0/3,715 なので
        (d2) には映らない。(d1) だけがこれを拾う。
        """
        with pytest.raises(GuardError, match="ガード\\(d1\\)"):
            assert_universe_coverage(
                RAW,
                EQUITY_20260831,
                P4B_ACTIVE,
                100,  # 100/4,440 = 2.25%
                existing_equity_active_count=ACTIVE,
                pending_deactivation_equity_count=0,
            )


class TestGuardD2:
    """新設。equity の分子を equity の分母で見る（D4）。"""

    def test_P4b_後も対象外化の上限が緩まない(self) -> None:
        """(d1) 単独だと、母集団拡張で内国普通株の実効上限が 74 -> 88 件へ緩む。

        現行 active 3,715 なら 88 件は 2.37% で (d1) が止めるが、P4b 後の
        4,440 では 1.98% になって (d1) を素通りする。equity の分母で見る (d2) を
        足すと 88/3,715 = 2.37% で止まり、上限が自動的に緩まなくなる。

        ※ (c) は (d) より先に評価されるので、被覆率を満たす equity を与えて
        (d) だけを見る。
        """
        # 拡張前: (d1) だけで止まる
        with pytest.raises(GuardError, match="ガード\\(d1\\)"):
            assert_universe_coverage(RAW, EQUITY_20260831, ACTIVE, 88)
        # 拡張後・(d2) なし: 88/4,440 = 1.98% で素通りしてしまう（旧挙動）
        assert_universe_coverage(RAW, 4_400, P4B_ACTIVE, 88)
        # 拡張後・(d2) あり: 88/3,715 = 2.37% で止まる
        with pytest.raises(GuardError, match="ガード\\(d2\\)"):
            assert_universe_coverage(
                RAW,
                EQUITY_20260831,
                P4B_ACTIVE,
                88,
                existing_equity_active_count=ACTIVE,
                pending_deactivation_equity_count=88,
            )

    def test_境界の74件は通る(self) -> None:
        assert_universe_coverage(
            RAW,
            EQUITY_20260831,
            P4B_ACTIVE,
            74,
            existing_equity_active_count=ACTIVE,
            pending_deactivation_equity_count=74,
        )

    def test_比率がちょうど2パーセントなら通る(self) -> None:
        """(d1) と同じ向き（`>` で止める）であること。"""
        assert_universe_coverage(
            RAW,
            3_100,
            10_000,
            60,
            existing_equity_active_count=3_000,
            pending_deactivation_equity_count=60,
        )
        with pytest.raises(GuardError, match="ガード\\(d2\\)"):
            assert_universe_coverage(
                RAW,
                3_100,
                10_000,
                61,
                existing_equity_active_count=3_000,
                pending_deactivation_equity_count=61,
            )

    def test_分子と分母を非可換にしない(self) -> None:
        """(d2) は equity の分子しか受け取らない。

        「(d) の分母だけを equity に絞る」案（却下した案 1）だと、ETF が 100 本
        廃止された月に 100/3,715 ではなく別母集団の比率になり誤発火する。
        (d2) に全銘柄種別の分子を渡す経路が無いことを型と引数名で固定する。
        """
        # 全体の分子 100・equity の分子 0 → (d2) は発火しない（分母 3,715 でも）
        assert_universe_coverage(
            RAW,
            EQUITY_20260831,
            P4B_ACTIVE,
            74,  # (d1) は 74/4,440 = 1.67% で通る
            existing_equity_active_count=ACTIVE,
            pending_deactivation_equity_count=0,
        )

    def test_equity_の件数を渡さなければ評価しない(self) -> None:
        """遷移期（instrument_type 未充填）は (d2) を評価しない。

        分母が 0 なので評価できない。ここで 0 除算するか、0 を「equity が
        1 件も無い」と読んで全部を異常にすると、充填前に毎月止まる。
        """
        assert_universe_coverage(
            RAW, EQUITY, ACTIVE, 74, pending_deactivation_equity_count=74
        )
        assert_universe_coverage(
            RAW,
            EQUITY,
            ACTIVE,
            74,
            existing_equity_active_count=0,
            pending_deactivation_equity_count=74,
        )


class TestInstrumentTypeTransition:
    """`instrument_type` が本番で全 NULL である遷移期の扱い（D4）。"""

    def test_未観測なら従来の分母を使う(self) -> None:
        assert coverage_denominator(ACTIVE, None) == (
            ACTIVE,
            "active 全体／instrument_type 未観測",
        )

    def test_未充填_ゼロ_でも従来の分母を使う(self) -> None:
        """本番の現状。0 を「equity が 1 件も無い」と読むと充填前に必ず止まる。"""
        assert coverage_denominator(ACTIVE, 0) == (
            ACTIVE,
            "active 全体／instrument_type 未充填",
        )
        assert_universe_coverage(RAW, EQUITY, ACTIVE, 0, existing_equity_active_count=0)

    def test_充填後は_equity_の分母を使う(self) -> None:
        assert coverage_denominator(P4B_ACTIVE, ACTIVE) == (ACTIVE, "active かつ equity")

    def test_充填漏れは症状ではなく原因の言葉で止める(self) -> None:
        with pytest.raises(GuardError, match="instrument_type の充填が済んでいない"):
            assert_instrument_type_backfilled(ACTIVE, 0)

    def test_充填済みなら通る(self) -> None:
        assert_instrument_type_backfilled(P4B_ACTIVE, ACTIVE)

    def test_本当に空なら充填を要求しない(self) -> None:
        """初回 seed 前。active 0 に対して充填を求めると seed が通らない。"""
        assert_instrument_type_backfilled(0, 0)


class TestPartialBackfill:
    """`instrument_type` の充填が**途中で止まった**状態（2026-09-12 レビューで追加）。

    `None`／`0` だけを未充填として扱うと、equity 件数が 500 のまま P4b を通したとき

    - (c) が **fail-open**: 3,100/500 = 6.2 で 0.98 を割らない。本当の被覆率は
      3,100/4,440 = 0.698 なので、部分取得された data_j を素通しする
    - (d2) が **誤発火**: 11/500 = 2.2%。実母集団では 11/3,700 = 0.3% で、
      D4 が消したはずの「毎月 throw する」の再来になる

    充填は 3,818 行への UPDATE をチャンクで回す別フェーズなので途中終了しうる。
    下限は新しい数字ではなく (b) の `MIN_EQUITY_ROWS` を再利用している。
    """

    PARTIAL = 500  # 充填が途中で止まった equity 件数

    def test_下限は_b_の_MIN_EQUITY_ROWS_を再利用する(self) -> None:
        """勘で置いた別の数字に差し替えられないよう、由来を固定する。"""
        assert MIN_BACKFILLED_EQUITY_ROWS == MIN_EQUITY_ROWS

    def test_部分充填は充填済みと認めない(self) -> None:
        assert not instrument_type_backfilled(None)
        assert not instrument_type_backfilled(0)
        assert not instrument_type_backfilled(self.PARTIAL)
        assert not instrument_type_backfilled(MIN_BACKFILLED_EQUITY_ROWS - 1)
        assert instrument_type_backfilled(MIN_BACKFILLED_EQUITY_ROWS)
        assert instrument_type_backfilled(ACTIVE)

    def test_部分充填なら従来の分母へ縮退する(self) -> None:
        denominator, label = coverage_denominator(P4B_ACTIVE, self.PARTIAL)
        assert denominator == P4B_ACTIVE
        assert "部分充填" in label

    def test_部分充填で_c_が空虚にならない(self) -> None:
        """この diff の前は素通りしていた（fail-open）。縮退後は 0.698 で発火する。"""
        with pytest.raises(GuardError, match="ガード\\(c\\)"):
            assert_universe_coverage(
                RAW, 3_100, P4B_ACTIVE, 0, existing_equity_active_count=self.PARTIAL
            )

    def test_部分充填の発火メッセージに原因を書く(self) -> None:
        with pytest.raises(GuardError, match="部分充填"):
            assert_universe_coverage(
                RAW, 3_100, P4B_ACTIVE, 0, existing_equity_active_count=self.PARTIAL
            )

    def test_部分充填では_d2_を誤発火させない(self) -> None:
        """11/500 = 2.2% で止めてはいけない。実母集団では 11/3,700 = 0.3%。

        ※ (c) は (d) より先に評価され、部分充填では分母が active 全体へ縮退する。
        (d2) だけを見るために、縮退後の被覆率を満たす equity を与える
        （`TestGuardD2.test_P4b_後も対象外化の上限が緩まない` と同じ手）。
        """
        assert_universe_coverage(
            RAW,
            4_400,  # 4,400/4,440 = 0.991 で (c) は通る
            P4B_ACTIVE,
            11,
            existing_equity_active_count=self.PARTIAL,
            pending_deactivation_equity_count=11,
        )

    def test_部分充填は原因の言葉で先に止める(self) -> None:
        with pytest.raises(GuardError, match="充填が途中で止まっている"):
            assert_instrument_type_backfilled(P4B_ACTIVE, self.PARTIAL)

    def test_充填済みなら_d2_は従来どおり働く(self) -> None:
        """縮退を足しても (d2) 本来の発火を殺していないこと。"""
        with pytest.raises(GuardError, match="ガード\\(d2\\)"):
            assert_universe_coverage(
                RAW,
                EQUITY_20260831,
                P4B_ACTIVE,
                88,
                existing_equity_active_count=ACTIVE,
                pending_deactivation_equity_count=88,
            )


class TestEquitySubsetInvariant:
    """equity active ⊆ active。超過は引数の取り違えか SELECT の失敗。

    分母が呼び出し側から来る引数になったので、その値が壊れていたら (c)(d2) の
    意味が丸ごと変わる。`assert_population_sane` と同じ趣旨で先に止める。
    """

    def test_equity_が_active_を超えたら止める(self) -> None:
        with pytest.raises(GuardError, match="部分集合なのでありえない"):
            assert_universe_coverage(
                RAW, EQUITY, ACTIVE, 0, existing_equity_active_count=ACTIVE + 1
            )

    def test_同数は通す(self) -> None:
        """充填直後・P4b 前は active 全件が equity になる（実測 3,715 / 3,715）。"""
        assert_universe_coverage(
            RAW, EQUITY, ACTIVE, 0, existing_equity_active_count=ACTIVE
        )

    def test_充填チェック側でも同じ前提を見る(self) -> None:
        with pytest.raises(GuardError, match="部分集合なのでありえない"):
            assert_instrument_type_backfilled(ACTIVE, ACTIVE + 1)


class TestInitialSeedHole:
    def test_existing_がゼロなら_c_と_d_はスキップされる(self) -> None:
        """移植元の意図的な穴。挙動を変えない（変えると初回 seed が通らない）。"""
        assert_universe_coverage(RAW, EQUITY, 0, 0)
        assert_universe_coverage(RAW, EQUITY, 0, 999_999)

    def test_行があるのに_active_ゼロは別ガードで止める(self) -> None:
        with pytest.raises(GuardError, match="SELECT の失敗"):
            assert_population_sane(3_818, 0)

    def test_本当に空なら通す(self) -> None:
        assert_population_sane(0, 0)


class TestStockCode:
    @pytest.mark.parametrize("code", ["7203", "130A", "409A", "0001"])
    def test_4文字契約に合格する(self, code: str) -> None:
        assert is_valid_stock_code(code)

    @pytest.mark.parametrize("code", ["25935", "720", "72031", "", None, "abcd"])
    def test_契約に合格しない(self, code: str | None) -> None:
        assert not is_valid_stock_code(code)

    def test_小文字は大文字化して判定する(self) -> None:
        assert is_valid_stock_code("130a")

    def test_前後の空白を落として判定する(self) -> None:
        assert is_valid_stock_code(" 7203 ")
        assert is_valid_stock_code("\t130A\n")

    def test_全角は半角化して判定する(self) -> None:
        """移植元 normalizeStockCode が全角英数字を半角へ倒すのに合わせる。"""
        assert normalize_stock_code("７２０３") == "7203"
        assert normalize_stock_code("１３０ａ") == "130A"
        assert is_valid_stock_code("７２０３")
        assert is_valid_stock_code("１３０ａ")

    def test_全角でも5桁は落ちる(self) -> None:
        assert not is_valid_stock_code("２５９３５")


class TestDeactivation:
    def test_raw_に無いコードは対象外化する(self) -> None:
        assert should_deactivate_universe_code("9999", frozenset({"7203"}))

    def test_raw_にあれば残す(self) -> None:
        assert not should_deactivate_universe_code("7203", frozenset({"7203"}))

    def test_契約違反のコードは_raw_にあっても対象外化する(self) -> None:
        assert should_deactivate_universe_code("25935", frozenset({"25935"}))

    def test_raw_codes_は内国株に絞る前の全行であること(self) -> None:
        """ETF 等の非普通株を内国株フィルタ後の集合で判定すると毎回対象外化されてしまう。"""
        raw_all = frozenset({"7203", "1202"})  # 1202 は合成 ETF コード
        assert not should_deactivate_universe_code("1202", raw_all)
        equities_only = frozenset({"7203"})
        assert should_deactivate_universe_code("1202", equities_only)
