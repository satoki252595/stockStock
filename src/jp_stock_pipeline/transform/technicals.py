"""テクニカル指標の計算 (DESIGN.md §4 テクニカル/バリュエーション欄)。

全て「計算値」(§3-4 加工の明示)。算式とパラメータ:
- SMA(n): 終値の単純移動平均 (n=5/25/75/200)
- SMA25乖離率% = (終値 - SMA25) / SMA25 × 100
- RSI14: Wilder 法。初期値 = 直近14本の値上がり幅/値下がり幅の単純平均、
  以降 avg = (前回avg×13 + 当日値) / 14。RSI = 100 - 100/(1+RS)
- MACD(12,26,9): EMA12 - EMA26 (adjust=False)。シグナル = MACD の EMA9、
  ヒストグラム = MACD - シグナル
- BB(20,2σ): 20日移動平均 ± 2×母標準偏差 (ddof=0)
- ATR14: TR = max(高値-安値, |高値-前日終値|, |安値-前日終値|) の Wilder 平滑
- 出来高25日平均比 = 当日出来高 / 出来高25日単純移動平均（当日含む）
- 売買代金 = 終値 × 出来高（概算値。VWAP ベースの真の売買代金ではない。
  カタログにこの算式を明示する §3-4）
- 52週高値/安値: データが暦日で52週(364日)以上を覆う場合のみ、
  直近260営業日の高値/安値

不変条件 (§3-1): 期間不足で計算できない指標は None（補間・外挿・推定は一切
しない）。NaN は None に変換して返す。

ライセンス: 計算値のタグは入力価格データから licensing.inherit() で継承する
こと（§2.2。本モジュールは値の計算のみを行い、タグ付与は呼び出し側の責務）。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")

# 52週 = 364暦日 / 約260営業日
_WEEK52_CALENDAR_DAYS = 364
_WEEK52_TRADING_DAYS = 260

# 単日でこの比率を超える終値変動は、未調整株価における株式分割/併合の不連続を
# 疑う閾値 (§ コーポレートアクション Phase1)。実急騰急落でも発火しうるが、
# 用途は「要確認」フラグ（=人間に確認依頼）なので過検出は許容する。
SPLIT_JUMP_THRESHOLD = 0.35


def has_probable_split(
    close: pd.Series,
    threshold: float = SPLIT_JUMP_THRESHOLD,
    lookback: int = _WEEK52_TRADING_DAYS,
) -> bool:
    """終値系列の直近 lookback 本に、分割/併合由来とみられる不連続があるか。

    yfinance は未調整終値のため、分割日に終値が比率分だけ不連続に跳ぶ
    （例: 1→3分割で約-67%、5→1併合で約+400%）。直近の長期指標(SMA200/52週)が
    覆う範囲（既定260営業日）で閾値超の単日変動を検出する。検出時は呼び出し側が
    ② を「要確認」にし、分割をまたぐ可能性を人間判断に委ねる
    （自動調整はしない §3-5。厳密な比率調整は Phase4）。
    入力 close は日付昇順であることを前提とする（呼び出し側 _ohlcv が昇順化する）。
    """
    c = close.astype(float).dropna()
    if len(c) < 2:
        return False
    window = c.iloc[-(lookback + 1):]  # +1: 窓先頭の単日リターンも範囲内に含める
    returns = window.pct_change().abs()
    returns = returns[np.isfinite(returns)]  # 先頭NaN・0除算のinf(終値0)を除外
    return bool((returns > threshold).any())


def _none_if_nan(value) -> float | None:
    """NaN/None を None に正規化 (§3-1 欠損は欠損のまま)。"""
    if value is None:
        return None
    f = float(value)
    return None if math.isnan(f) or math.isinf(f) else f


def _sma_last(series: pd.Series, window: int) -> float | None:
    if len(series) < window:
        return None
    return _none_if_nan(series.iloc[-window:].mean())


def wilder_smooth(values: np.ndarray, period: int) -> float | None:
    """Wilder 平滑の最終値。初期値=先頭 period 本の単純平均、以降逐次平滑。

    values は平滑対象の系列（RSI なら値上がり幅、ATR なら TR）。
    len(values) < period なら None。
    """
    if len(values) < period:
        return None
    avg = float(np.mean(values[:period]))
    for v in values[period:]:
        avg = (avg * (period - 1) + float(v)) / period
    return avg


def rsi14_last(close: pd.Series, period: int = 14) -> float | None:
    """RSI (Wilder)。前日差分が period 本未満なら None。"""
    delta = close.diff().dropna()
    if len(delta) < period:
        return None
    gains = delta.clip(lower=0).to_numpy()
    losses = (-delta.clip(upper=0)).to_numpy()
    avg_gain = wilder_smooth(gains, period)
    avg_loss = wilder_smooth(losses, period)
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr14_last(df: pd.DataFrame, period: int = 14) -> float | None:
    """ATR (Wilder)。TR 計算には前日終値が要るため period+1 行未満なら None。"""
    if len(df) < period + 1:
        return None
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    prev_close = df["close"].shift(1).to_numpy(dtype=float)
    tr = np.maximum.reduce(
        [high[1:] - low[1:], np.abs(high[1:] - prev_close[1:]), np.abs(low[1:] - prev_close[1:])]
    )
    if np.isnan(tr).any():
        return None
    return wilder_smooth(tr, period)


def macd_last(close: pd.Series) -> tuple[float | None, float | None, float | None]:
    """MACD(12,26,9)。26本未満は全て None、26〜34本は MACD のみ。"""
    if len(close) < 26:
        return None, None, None
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_series = ema12 - ema26
    macd = _none_if_nan(macd_series.iloc[-1])
    if len(close) < 26 + 9:
        return macd, None, None
    signal_series = macd_series.ewm(span=9, adjust=False).mean()
    signal = _none_if_nan(signal_series.iloc[-1])
    hist = None if macd is None or signal is None else macd - signal
    return macd, signal, hist


def _validate(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"必須列が不足: {missing} (必要: {REQUIRED_COLUMNS})")
    if len(df) == 0:
        raise ValueError("空の DataFrame")
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    out = out.sort_values("date").reset_index(drop=True)
    return out


def compute_technicals(df: pd.DataFrame) -> dict:
    """日足 DataFrame (date昇順, columns=REQUIRED_COLUMNS) から最新スナップショットを計算。

    返り値キーは models.PriceTechnicalRecord の同名フィールドに対応する。
    計算不能な指標は None (§3-1)。
    """
    df = _validate(df)
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    last = df.iloc[-1]

    out: dict[str, float | None] = {
        "open": _none_if_nan(last["open"]),
        "high": _none_if_nan(last["high"]),
        "low": _none_if_nan(last["low"]),
        "close": _none_if_nan(last["close"]),
        "volume": _none_if_nan(last["volume"]),
    }

    # 前日比率% (2行以上)
    out["prev_close_pct"] = None
    if len(close) >= 2:
        prev = close.iloc[-2]
        if prev and not math.isnan(prev):
            out["prev_close_pct"] = _none_if_nan((close.iloc[-1] - prev) / prev * 100.0)

    # 売買代金（概算 = 終値×出来高。算式は docstring/カタログに明示 §3-4）
    c, v = out["close"], out["volume"]
    out["turnover"] = c * v if c is not None and v is not None else None

    # SMA 群
    out["sma5"] = _sma_last(close, 5)
    out["sma25"] = _sma_last(close, 25)
    out["sma75"] = _sma_last(close, 75)
    out["sma200"] = _sma_last(close, 200)
    out["sma25_dev_pct"] = (
        _none_if_nan((out["close"] - out["sma25"]) / out["sma25"] * 100.0)
        if out["close"] is not None and out["sma25"]
        else None
    )

    # 52週高安: データが暦日で52週以上を覆う場合のみ (§3-1 推定禁止)
    out["week52_high"] = None
    out["week52_low"] = None
    span_days = (df["date"].iloc[-1] - df["date"].iloc[0]).days
    if span_days >= _WEEK52_CALENDAR_DAYS:
        window = df.iloc[-_WEEK52_TRADING_DAYS:]
        out["week52_high"] = _none_if_nan(window["high"].astype(float).max())
        out["week52_low"] = _none_if_nan(window["low"].astype(float).min())

    out["rsi14"] = rsi14_last(close)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd_last(close)

    # ボリンジャーバンド (20, 2σ, ddof=0)
    out["bb_upper"] = None
    out["bb_lower"] = None
    if len(close) >= 20:
        win = close.iloc[-20:]
        mean = float(win.mean())
        sigma = float(win.std(ddof=0))
        out["bb_upper"] = _none_if_nan(mean + 2.0 * sigma)
        out["bb_lower"] = _none_if_nan(mean - 2.0 * sigma)

    out["atr14"] = atr14_last(df)

    # 出来高25日平均比（当日含む25日平均に対する当日比）
    out["volume_ratio25"] = None
    vol_sma25 = _sma_last(volume, 25)
    if vol_sma25 and out["volume"] is not None:
        out["volume_ratio25"] = out["volume"] / vol_sma25

    return out
