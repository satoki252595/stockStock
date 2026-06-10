"""yfinance 株価コレクター (DESIGN.md §4 トラックB, §8.3 / §2.1)。

- ライセンス: Yahoo Finance ToS は自動アクセス・商用利用を禁止 → personal-only 固定
  （§2.1。公開・商用導線には一切流さない）
- レート対策 (§8.3): `yf.download` を 100 銘柄ずつのチャンクで実行し、
  チャンク間に 2〜5 秒スリープ。HTTP 429 相当の例外時は 60 秒待機して 1 回だけ再試行
- 真実性 (§3): 取得できなかった銘柄は結果から除外し missing リストで返す。
  None 埋め・前日値コピー等のダミー生成は絶対にしない

原本の定義 (§5.1, §5.2):
    yfinance は HTTP 生レスポンスをライブラリ外に公開しないため、
    「取得単位（fetch_daily_batch の 1 回呼び出し）で得た生 DataFrame 群を
    無加工で long 形式 CSV 化したバイト列」を原本とする。
    値は yfinance が返したものをそのまま書き出す（型変換・縦持ち化のみ。
    丸め・補完・修正は行わない §5.2）。
"""

from __future__ import annotations

import io
import json
import logging
import random
import time
from typing import TYPE_CHECKING, Callable

import pandas as pd
import yfinance as yf

from ..licensing import source_license
from ..models import ConvertStatus, RawArtifact, Source
from ..rawstore import converted_filename, save_raw

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

CHUNK_SIZE = 100  # §8.3: 100銘柄ごとにスリープ
CHUNK_SLEEP_RANGE = (2.0, 5.0)  # チャンク間スリープ秒 (§8.3: 2〜5秒)
RATE_LIMIT_WAIT = 60.0  # §8.3: 429時は60秒待機
VALUATION_SLEEP = 0.5  # fetch_valuation の銘柄毎スリープ秒

# long 形式 CSV の列順（先頭2列がキー、以降は yfinance の生列名のまま）
_LONG_KEY_COLUMNS = ["code", "Date"]
_PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def to_ticker(code: str) -> str:
    """証券コード4桁 → yfinance ティッカー (§12: `7203.T` 形式)。"""
    return f"{code}.T"


def from_ticker(ticker: str) -> str:
    """yfinance ティッカー → 証券コード。"""
    return ticker.removesuffix(".T")


def chunked(codes: list[str], size: int = CHUNK_SIZE) -> list[list[str]]:
    """銘柄リストを size 件ずつのチャンクに分割する（順序保持 §8.3）。"""
    if size <= 0:
        raise ValueError("チャンクサイズは正の整数")
    return [codes[i : i + size] for i in range(0, len(codes), size)]


def _is_rate_limited(exc: BaseException) -> bool:
    """HTTP 429 相当の例外か（yfinance は版により例外型が変わるため文字列も見る）。"""
    if type(exc).__name__ in ("YFRateLimitError",):
        return True
    text = str(exc)
    return "429" in text or "Too Many Requests" in text or "Rate limit" in text


def _download_chunk(
    tickers: list[str], period: str, *, sleeper: Callable[[float], None] = time.sleep
) -> pd.DataFrame:
    """1チャンク分を yf.download で取得する (§8.3)。

    429 相当の例外時は 60 秒待機して 1 回だけ再試行する。
    それ以外の例外・再試行失敗は呼び出し側へ送出（→チャンク全銘柄を missing 扱い）。
    """

    def _do() -> pd.DataFrame:
        return yf.download(
            tickers,
            period=period,
            group_by="ticker",
            auto_adjust=False,
            threads=False,
            progress=False,
        )

    try:
        return _do()
    except Exception as exc:
        if not _is_rate_limited(exc):
            raise
        logger.warning("yfinance 429 相当: %s — %d 秒待機して再試行 (§8.3)", exc, int(RATE_LIMIT_WAIT))
        sleeper(RATE_LIMIT_WAIT)
        return _do()


def _extract_ticker_frame(df: pd.DataFrame, ticker: str, n_tickers: int) -> pd.DataFrame | None:
    """yf.download 結果から 1 銘柄分の生 DataFrame を取り出す。

    全行 NaN（取得失敗銘柄）は None を返す（→ missing。ダミー禁止 §3-1）。
    値は一切変更しない。
    """
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        if ticker not in df.columns.get_level_values(0):
            return None
        sub = df[ticker]
    elif n_tickers == 1:
        sub = df
    else:
        return None
    sub = sub.dropna(how="all")
    if sub.empty:
        return None
    return sub


def serialize_long_csv(frames: dict[str, pd.DataFrame]) -> bytes:
    """生 DataFrame 群を long 形式 CSV バイト列にする（原本の定義。モジュール docstring 参照）。

    列: code, Date, Open, High, Low, Close, Adj Close, Volume
    （後段6列は yfinance の生列名のまま）。値は無加工 (§5.2)。
    銘柄コード昇順 × 日付昇順で安定化する（並べ替えは値を変えない）。
    """
    parts: list[pd.DataFrame] = []
    for code in sorted(frames):
        sub = frames[code].sort_index()  # 日付昇順（並べ替えのみ・値不変）
        sub = sub.reset_index()
        # index 名は版により "Date"/"index" — 原本列名 Date に揃える（名称のみ・値不変）
        sub = sub.rename(columns={sub.columns[0]: "Date"})
        sub.insert(0, "code", code)
        cols = _LONG_KEY_COLUMNS + [c for c in _PRICE_COLUMNS if c in sub.columns]
        parts.append(sub[cols])
    if not parts:
        return b""
    long_df = pd.concat(parts, ignore_index=True)
    # Date は ISO 日付文字列に（型変換のみ・値不変 §5.2）
    long_df["Date"] = pd.to_datetime(long_df["Date"]).dt.strftime("%Y-%m-%d")
    buf = io.StringIO()
    long_df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")


def parse_long_csv(content: bytes) -> dict[str, pd.DataFrame]:
    """long 形式 CSV 原本を銘柄別 DataFrame 群へ戻す（serialize_long_csv の逆変換）。

    返り値の各 DataFrame は Date を DatetimeIndex に持ち、列は CSV に存在した
    価格列（型は pandas 推論のまま。強制キャストせず値不変を保つ §5.2）。
    serialize_long_csv(parse_long_csv(x)) == x（バイト一致の可逆性）が成立する。
    """
    df = pd.read_csv(io.BytesIO(content))
    frames: dict[str, pd.DataFrame] = {}
    for code, sub in df.groupby("code", sort=True):
        sub = sub.drop(columns=["code"])
        sub["Date"] = pd.to_datetime(sub["Date"])
        sub = sub.set_index("Date")
        frames[str(code)] = sub
    return frames


def to_parquet_bytes(frames: dict[str, pd.DataFrame]) -> bytes:
    """変換版 Parquet (§5.2 株価履歴: 型付き date, code, open, high, low, close, volume, adj_close)。

    値は原本と同一（型付け・縦持ち化・列名正規化のみ。丸め・補完禁止 §5.2）。
    """
    rows: list[pd.DataFrame] = []
    rename = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
        "Adj Close": "adj_close",
    }
    for code in sorted(frames):
        sub = frames[code].copy().reset_index()
        sub = sub.rename(columns={sub.columns[0]: "date", **rename})
        sub.insert(1, "code", code)
        cols = ["date", "code"] + [
            c for c in ("open", "high", "low", "close", "volume", "adj_close") if c in sub.columns
        ]
        rows.append(sub[cols])
    if not rows:
        empty = pd.DataFrame(
            columns=["date", "code", "open", "high", "low", "close", "volume", "adj_close"]
        )
        buf0 = io.BytesIO()
        empty.to_parquet(buf0, index=False)
        return buf0.getvalue()
    out = pd.concat(rows, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    out["code"] = out["code"].astype("string")
    for col in ("open", "high", "low", "close", "volume", "adj_close"):
        if col in out.columns:
            out[col] = out[col].astype("float64")
    buf = io.BytesIO()
    out.to_parquet(buf, index=False)
    return buf.getvalue()


def fetch_daily_batch(
    settings: "Settings",
    codes: list[str],
    period: str = "2y",
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[RawArtifact, dict[str, pd.DataFrame], list[str]]:
    """全銘柄の日足を 100 銘柄チャンクで一括取得する (§8.3, §8.1 step 1-3)。

    返り値: (原本 RawArtifact, {code: 生DataFrame}, missing 銘柄リスト)
    - 原本 = 本呼び出しで得た生 DataFrame 群の long 形式 CSV（モジュール docstring の定義）。
      datatype="daily_prices_batch", scope="ALL", license=personal-only (§2.1)
    - 例外・空データの銘柄は結果から除外し missing に列挙（ダミー埋め禁止 §3-1）
    - 変換版 Parquet (§5.2) を原本と併置（変換失敗でも原本保存は成立 §5.2）
    - 全銘柄 missing（取得ゼロ）の場合も「取得できなかった」事実をそのまま返す
    """
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    chunks = chunked(codes, CHUNK_SIZE)
    for i, chunk in enumerate(chunks):
        tickers = [to_ticker(c) for c in chunk]
        try:
            df = _download_chunk(tickers, period, sleeper=sleeper)
        except Exception as exc:
            logger.warning("yfinance チャンク取得失敗 (%d銘柄): %s — 欠損として記録 (§3-2)", len(chunk), exc)
            missing.extend(chunk)
            df = None
        if df is not None:
            for code, ticker in zip(chunk, tickers, strict=True):
                sub = _extract_ticker_frame(df, ticker, len(tickers))
                if sub is None:
                    missing.append(code)
                else:
                    frames[code] = sub
        if i < len(chunks) - 1:
            sleeper(random.uniform(*CHUNK_SLEEP_RANGE))  # §8.3: チャンク間 2〜5秒

    content = serialize_long_csv(frames)
    data_date = max((f.index.max().date() for f in frames.values()), default=None)
    artifact = save_raw(
        content,
        source=Source.YFINANCE,
        datatype="daily_prices_batch",
        scope="ALL",
        data_date=data_date,
        url=f"yfinance:download(period={period},n={len(codes)})",
        ext="csv",
        license_tag=source_license(Source.YFINANCE),
        base_dir=settings.raw_data_dir,
    )
    # 変換版 Parquet (§5.2)。失敗しても原本保存は成立させ状態のみ記録
    try:
        pq = to_parquet_bytes(frames)
        pq_path = artifact.local_path.parent / converted_filename(artifact.filename, "parquet")
        pq_path.write_bytes(pq)
        artifact.converted_paths.append(pq_path)
        artifact.convert_status = ConvertStatus.DONE
    except Exception as exc:
        logger.warning("Parquet 変換失敗（原本保存は成立 §5.2）: %s", exc)
        artifact.convert_status = ConvertStatus.FAILED
    return artifact, frames, missing


# ---------------------------------------------------------------------------
# バリュエーション (§4: 時価総額・PER・PBR・配当利回り)
# ---------------------------------------------------------------------------

# 取得項目 → (fast_info キー, get_info キー)
_VALUATION_KEYS: dict[str, tuple[str | None, str | None]] = {
    "market_cap": ("marketCap", "marketCap"),
    "per": (None, "trailingPE"),
    "pbr": (None, "priceToBook"),
    "dividend_yield": (None, "dividendYield"),
}


def fetch_valuation(
    settings: "Settings",
    codes: list[str],
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, dict]:
    """銘柄毎に時価総額・PER・PBR・配当利回りを取得する (§4 トラックB)。

    - Ticker.fast_info / get_info から market_cap / per(trailingPE) /
      pbr(priceToBook) / dividend_yield を取得
    - 取れなかった項目・銘柄は結果に載せない（None 埋め禁止 §3-1）。失敗はログのみ
    - 銘柄毎 0.5 秒スリープ (§8.3 レート対策)
    """
    result: dict[str, dict] = {}
    for i, code in enumerate(codes):
        values: dict[str, float] = {}
        try:
            ticker = yf.Ticker(to_ticker(code))
            fast: dict = {}
            info: dict = {}
            try:
                fast = dict(ticker.fast_info or {})
            except Exception as exc:
                logger.info("yfinance fast_info 失敗 %s: %s", code, exc)
            try:
                info = ticker.get_info() or {}
            except Exception as exc:
                logger.info("yfinance get_info 失敗 %s: %s", code, exc)
            for name, (fast_key, info_key) in _VALUATION_KEYS.items():
                value = None
                if fast_key is not None:
                    value = fast.get(fast_key)
                if value is None and info_key is not None:
                    value = info.get(info_key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values[name] = float(value)
        except Exception as exc:
            logger.warning("yfinance バリュエーション取得失敗 %s: %s（欠損のまま §3-2）", code, exc)
        if values:
            result[code] = values
        if i < len(codes) - 1:
            sleeper(VALUATION_SLEEP)
    return result


def save_valuation_raw(settings: "Settings", valuations: dict[str, dict]) -> RawArtifact:
    """fetch_valuation の取得結果を原本として保存する (§5.1)。

    yfinance は生レスポンスを公開しないため、取得単位（一括呼び出し）の
    取得値を無加工で JSON 化したバイト列を原本とする（fetch_daily_batch と同じ定義 §5.2）。
    """
    content = json.dumps(valuations, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
    return save_raw(
        content,
        source=Source.YFINANCE,
        datatype="valuation",
        scope="ALL",
        data_date=None,  # 取得時点のスナップショット（基準日は特定できないため None §3-1）
        url=f"yfinance:fast_info/get_info(n={len(valuations)})",
        ext="json",
        license_tag=source_license(Source.YFINANCE),
        base_dir=settings.raw_data_dir,
    )
