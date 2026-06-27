"""テクニカル指標計算のテスト。

実データ: tests/fixtures/transform/yfinance_7203T_daily.csv
(7203.T の日足2年分。scripts/capture_transform_fixtures 相当で実取得済み)。
検証はテスト内で同じ実データから独立に再計算した値と比較する。
"""

from __future__ import annotations

import pandas as pd
import pytest

from jp_stock_pipeline.transform.technicals import compute_technicals, has_probable_split

from conftest import fixture_path


@pytest.fixture(scope="module")
def daily_df() -> pd.DataFrame:
    path = fixture_path("transform/yfinance_7203T_daily.csv")
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    return df


@pytest.fixture(scope="module")
def result(daily_df) -> dict:
    return compute_technicals(daily_df)


class TestWithRealData:
    def test_close_matches_last_row(self, daily_df, result):
        assert result["close"] == pytest.approx(float(daily_df["close"].iloc[-1]))

    def test_sma5_equals_mean_of_last5(self, daily_df, result):
        expected = float(daily_df["close"].iloc[-5:].mean())
        assert result["sma5"] == pytest.approx(expected)

    def test_sma200_equals_mean_of_last200(self, daily_df, result):
        expected = float(daily_df["close"].iloc[-200:].mean())
        assert result["sma200"] == pytest.approx(expected)

    def test_prev_close_pct(self, daily_df, result):
        prev, cur = daily_df["close"].iloc[-2], daily_df["close"].iloc[-1]
        assert result["prev_close_pct"] == pytest.approx((cur - prev) / prev * 100.0)

    def test_turnover_is_close_times_volume(self, daily_df, result):
        expected = float(daily_df["close"].iloc[-1]) * float(daily_df["volume"].iloc[-1])
        assert result["turnover"] == pytest.approx(expected)

    def test_rsi14_in_range(self, result):
        assert 0.0 <= result["rsi14"] <= 100.0

    def test_bb_band_ordering(self, daily_df, result):
        sma20 = float(daily_df["close"].iloc[-20:].mean())
        assert result["bb_lower"] < sma20 < result["bb_upper"]

    def test_bb_matches_direct_computation(self, daily_df, result):
        win = daily_df["close"].iloc[-20:].astype(float)
        sigma = float(win.std(ddof=0))
        assert result["bb_upper"] == pytest.approx(float(win.mean()) + 2 * sigma)

    def test_atr14_positive(self, result):
        assert result["atr14"] > 0

    def test_volume_ratio25(self, daily_df, result):
        expected = float(daily_df["volume"].iloc[-1]) / float(
            daily_df["volume"].iloc[-25:].mean()
        )
        assert result["volume_ratio25"] == pytest.approx(expected)

    def test_week52_high_low_from_window(self, daily_df, result):
        window = daily_df.iloc[-260:]
        assert result["week52_high"] == pytest.approx(float(window["high"].max()))
        assert result["week52_low"] == pytest.approx(float(window["low"].min()))
        assert result["week52_low"] <= result["close"] <= result["week52_high"] * 1.0001

    def test_macd_consistency(self, result):
        assert result["macd_hist"] == pytest.approx(
            result["macd"] - result["macd_signal"]
        )

    def test_sma25_dev_pct(self, result):
        expected = (result["close"] - result["sma25"]) / result["sma25"] * 100.0
        assert result["sma25_dev_pct"] == pytest.approx(expected)


class TestInsufficientDataReturnsNone:
    """期間不足の指標は None (§3-1。補間・外挿をしないことの検証)。"""

    def test_short_history_sma200_none(self, daily_df):
        out = compute_technicals(daily_df.iloc[-150:])
        assert out["sma200"] is None
        assert out["sma75"] is not None

    def test_under_52_weeks_no_week52(self, daily_df):
        out = compute_technicals(daily_df.iloc[-100:])
        assert out["week52_high"] is None
        assert out["week52_low"] is None

    def test_tiny_history_most_none(self, daily_df):
        out = compute_technicals(daily_df.iloc[-3:])
        assert out["close"] is not None
        assert out["prev_close_pct"] is not None
        for key in ("sma5", "sma25", "rsi14", "macd", "bb_upper", "atr14", "volume_ratio25"):
            assert out[key] is None, key

    def test_single_row(self, daily_df):
        out = compute_technicals(daily_df.iloc[-1:])
        assert out["prev_close_pct"] is None
        assert out["close"] is not None

    def test_macd_needs_26(self, daily_df):
        out = compute_technicals(daily_df.iloc[-30:])
        assert out["macd"] is not None
        assert out["macd_signal"] is None  # 26+9 未満

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            compute_technicals(pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"]))

    def test_missing_column_raises(self, daily_df):
        with pytest.raises(ValueError):
            compute_technicals(daily_df.drop(columns=["volume"]))


class TestOrderIndependence:
    def test_unsorted_input_same_result(self, daily_df, result):
        shuffled = daily_df.sample(frac=1.0, random_state=7)
        out = compute_technicals(shuffled)
        assert out["sma25"] == pytest.approx(result["sma25"])
        assert out["close"] == pytest.approx(result["close"])


class TestHasProbableSplit:
    """分割/併合の不連続検出 (§ コーポレートアクション Phase1)。"""

    def test_no_jump_is_false(self):
        close = pd.Series([100.0 + i * 0.5 for i in range(300)])  # なだらか
        assert has_probable_split(close) is False

    def test_split_jump_detected(self):
        # 直近で 3000 → 1000 (1→3分割相当, -67%)
        close = pd.Series([3000.0] * 50 + [1000.0] * 50)
        assert has_probable_split(close) is True

    def test_reverse_split_jump_detected(self):
        # 1000 → 3000 (3→1併合相当, +200%)
        close = pd.Series([1000.0] * 50 + [3000.0] * 50)
        assert has_probable_split(close) is True

    def test_old_jump_outside_lookback_ignored(self):
        # 先頭で分割、その後 300本平穏 → lookback(260)外なので現スナップショットに無影響
        close = pd.Series([3000.0] * 10 + [1000.0] * 300)
        assert has_probable_split(close, lookback=260) is False

    def test_too_short_is_false(self):
        assert has_probable_split(pd.Series([100.0])) is False
