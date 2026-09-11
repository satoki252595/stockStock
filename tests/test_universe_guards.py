"""①母集団ガードの移植テスト（移行 P4a / G-core-4）。

移植元は kabulab-cf の src/cron/universe.ts:82-120。本テストは
「同じ入力で同じように止まる」ことを固定する。境界値ちょうどは通る側。

現行の実測値（2026-09-12）:
  core_stocks total 3,818 / active 3,715
  data_j.xls (2026-05-31) 全 4,451 行 / 4文字内国株 3,728 件
"""

from __future__ import annotations

import pytest

from jp_stock_pipeline.cloud_store.guards import GuardError
from jp_stock_pipeline.cloud_store.universe_guards import (
    MAX_DEACTIVATION_RATIO,
    MIN_EQUITY_ROWS,
    MIN_EXISTING_COVERAGE,
    MIN_JPX_ROWS,
    assert_population_sane,
    assert_universe_coverage,
    is_valid_stock_code,
    should_deactivate_universe_code,
)

ACTIVE = 3_715  # 本番実測
EQUITY = 3_728  # data_j 2026-05-31 実測
RAW = 4_451  # 同上


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

    def test_P4b_で母集団を広げると発火する(self) -> None:
        """+707 行して active 4,422 になると 3,728/4,422=0.843 で毎月止まる。

        P4b の前提条件として (c) の分母を equity に限定する改修が要ることを、
        仕様としてここに固定する。
        """
        with pytest.raises(GuardError, match="ガード\\(c\\)"):
            assert_universe_coverage(RAW, EQUITY, 4_422, 0)


class TestGuardD:
    def test_対象外化が2パーセント超なら止める(self) -> None:
        # 0.02 * 3715 = 74.3 → 75 件で発火
        with pytest.raises(GuardError, match="ガード\\(d\\)"):
            assert_universe_coverage(RAW, EQUITY, ACTIVE, 75)

    def test_境界の74件は通る(self) -> None:
        assert_universe_coverage(RAW, EQUITY, ACTIVE, 74)


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


class TestDeactivation:
    def test_raw_に無いコードは対象外化する(self) -> None:
        assert should_deactivate_universe_code("9999", frozenset({"7203"}))

    def test_raw_にあれば残す(self) -> None:
        assert not should_deactivate_universe_code("7203", frozenset({"7203"}))

    def test_契約違反のコードは_raw_にあっても対象外化する(self) -> None:
        assert should_deactivate_universe_code("25935", frozenset({"25935"}))

    def test_raw_codes_は内国株に絞る前の全行であること(self) -> None:
        """ETF を内国株フィルタ後の集合で判定すると毎回対象外化されてしまう。"""
        raw_all = frozenset({"7203", "1321"})  # 1321 は ETF
        assert not should_deactivate_universe_code("1321", raw_all)
        equities_only = frozenset({"7203"})
        assert should_deactivate_universe_code("1321", equities_only)
