"""yfinance コレクターのテスト (DESIGN.md §4 トラックB, §8.3, §2.1)。

- yf.download / Ticker の実呼び出しはテストでは行わない（レート・再現性のため）
- フィクスチャ prices/yfinance_daily_batch_7203.csv は scripts/capture_prices.py が
  2026-06-10 に実取得した 7203.T period=2y の原本（long 形式 CSV §5.2。捏造禁止 §3-6）
- チャンク分割・429再試行・missing 除外などの制御フローはネットワーク無しで検証する
"""

from __future__ import annotations

import pandas as pd
import pytest

from jp_stock_pipeline.collectors import yfinance_prices as yp
from jp_stock_pipeline.config import load_settings
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import ConvertStatus, Source
from jp_stock_pipeline.rawstore import sha256_bytes

from conftest import fixture_path


def _settings(tmp_path):
    return load_settings(dry_run=True, env={"RAW_DATA_DIR": str(tmp_path)})


def _fixture_frames() -> tuple[bytes, dict[str, pd.DataFrame]]:
    content = fixture_path("prices/yfinance_daily_batch_7203.csv").read_bytes()
    return content, yp.parse_long_csv(content)


# ---------------------------------------------------------------------------
# チャンク分割ロジック (§8.3: 100銘柄ずつ)
# ---------------------------------------------------------------------------


class TestChunked:
    def test_100ずつ分割_順序保持(self):
        codes = [f"{i:04d}" for i in range(250)]
        chunks = yp.chunked(codes, 100)
        assert [len(c) for c in chunks] == [100, 100, 50]
        assert [c for chunk in chunks for c in chunk] == codes  # 順序保持

    def test_端数なし(self):
        assert [len(c) for c in yp.chunked(list("ab") * 100, 100)] == [100, 100]

    def test_空リスト(self):
        assert yp.chunked([], 100) == []

    def test_不正サイズ(self):
        with pytest.raises(ValueError):
            yp.chunked(["7203"], 0)


def test_ticker変換():
    assert yp.to_ticker("7203") == "7203.T"  # §12: `7203.T` 形式
    assert yp.from_ticker("7203.T") == "7203"


# ---------------------------------------------------------------------------
# 原本 long CSV シリアライズの可逆性（実フィクスチャ §3-6）
# ---------------------------------------------------------------------------


class TestLongCsvRoundtrip:
    def test_パースとバイト一致の可逆性(self):
        content, frames = _fixture_frames()
        assert list(frames) == ["7203"]
        sub = frames["7203"]
        assert len(sub) > 0
        # 原本列 (yfinance 生列名のまま)
        assert set(sub.columns) <= set(yp._PRICE_COLUMNS)
        assert sub.index.is_monotonic_increasing
        # 可逆性: parse → serialize で原本とバイト一致（値不変 §5.2）
        assert yp.serialize_long_csv(frames) == content

    def test_値はCSV原文と一致(self):
        content, frames = _fixture_frames()
        lines = content.decode("utf-8").strip().splitlines()
        header = lines[0].split(",")
        first = lines[1].split(",")
        sub = frames[first[header.index("code")]]
        row = sub.iloc[0]
        assert sub.index[0].strftime("%Y-%m-%d") == first[header.index("Date")]
        assert row["Open"] == float(first[header.index("Open")])
        assert row["Adj Close"] == float(first[header.index("Adj Close")])

    def test_空のシリアライズ(self):
        # 全銘柄取得失敗時も「取得できなかった」事実のみ（ダミー生成しない §3-1）
        assert yp.serialize_long_csv({}) == b""


def test_to_parquet_型付き変換(tmp_path):
    """変換版 Parquet (§5.2: date, code, open, high, low, close, volume, adj_close)。"""
    _content, frames = _fixture_frames()
    df = pd.read_parquet(__import__("io").BytesIO(yp.to_parquet_bytes(frames)))
    assert list(df.columns) == [
        "date", "code", "open", "high", "low", "close", "volume", "adj_close",
    ]
    assert len(df) == len(frames["7203"])
    # 値不変 (§5.2): 原本フレームと同値
    sub = frames["7203"]
    assert df["close"].tolist() == sub["Close"].astype("float64").tolist()
    assert df["date"].tolist() == list(sub.index)
    assert (df["code"] == "7203").all()


# ---------------------------------------------------------------------------
# fetch_daily_batch（_download_chunk を実フィクスチャ由来データで差し替え）
# ---------------------------------------------------------------------------


def _as_download_result(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """yf.download(group_by="ticker") 形式（列=MultiIndex[ticker, field]）へ再構成する。"""
    return pd.concat({yp.to_ticker(code): sub for code, sub in frames.items()}, axis=1)


def test_fetch_daily_batch_チャンク_スリープ_missing(tmp_path, monkeypatch):
    _content, frames = _fixture_frames()
    download_result = _as_download_result(frames)
    requested: list[list[str]] = []
    sleeps: list[float] = []

    def fake_download_chunk(tickers, period, *, sleeper):
        requested.append(list(tickers))
        if "7203.T" in tickers:
            return download_result
        return pd.DataFrame()  # データ無しチャンク（→ missing。ダミー禁止 §3-1）

    monkeypatch.setattr(yp, "_download_chunk", fake_download_chunk)
    monkeypatch.setattr(yp, "CHUNK_SIZE", 1)  # 2銘柄を2チャンクに分けるため
    settings = _settings(tmp_path)

    artifact, result, missing = yp.fetch_daily_batch(
        settings, ["7203", "9999"], period="2y", sleeper=sleeps.append
    )

    # チャンク分割と順序 (§8.3)
    assert requested == [["7203.T"], ["9999.T"]]
    # チャンク間スリープは 2〜5 秒 (§8.3)。最終チャンク後は不要
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 5.0
    # 取得失敗銘柄は結果から除外し missing へ（None埋め禁止 §3-1）
    assert list(result) == ["7203"]
    assert missing == ["9999"]
    # 値不変 (§5.2)
    pd.testing.assert_frame_equal(result["7203"], frames["7203"])

    # 原本 = 取得単位の生データの long CSV シリアライズ (§5.1, §5.2)
    raw = artifact.local_path.read_bytes()
    assert raw == yp.serialize_long_csv(result)
    assert artifact.sha256 == sha256_bytes(raw)
    assert artifact.source is Source.YFINANCE
    assert artifact.datatype == "daily_prices_batch"
    assert artifact.scope == "ALL"
    assert artifact.license_tag is LicenseTag.PERSONAL_ONLY  # §2.1
    assert artifact.data_date == frames["7203"].index.max().date()
    # 変換版 Parquet が併置される (§5.2)
    assert artifact.convert_status is ConvertStatus.DONE
    assert len(artifact.converted_paths) == 1
    assert artifact.converted_paths[0].suffix == ".parquet"
    assert artifact.converted_paths[0].exists()


def test_fetch_daily_batch_チャンク例外は欠損として続行(tmp_path, monkeypatch):
    def fake_download_chunk(tickers, period, *, sleeper):
        raise RuntimeError("boom")

    monkeypatch.setattr(yp, "_download_chunk", fake_download_chunk)
    artifact, result, missing = yp.fetch_daily_batch(
        _settings(tmp_path), ["7203", "9999"], sleeper=lambda s: None
    )
    # 全滅でも欠損は欠損のまま（前日値コピー等をしない §3-2）
    assert result == {}
    assert missing == ["7203", "9999"]
    assert artifact.local_path.read_bytes() == b""
    assert artifact.data_date is None  # 推定しない (§3-1)


# ---------------------------------------------------------------------------
# 429 相当の再試行（60秒待って1回だけ §8.3）
# ---------------------------------------------------------------------------


class TestRateLimitRetry:
    def test_429で60秒待機して1回だけ再試行(self, monkeypatch):
        calls: list[int] = []
        sleeps: list[float] = []

        def fake_download(tickers, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("HTTP Error 429: Too Many Requests")
            return pd.DataFrame()

        monkeypatch.setattr(yp.yf, "download", fake_download)
        df = yp._download_chunk(["7203.T"], "2y", sleeper=sleeps.append)
        assert df.empty
        assert len(calls) == 2  # 再試行は1回だけ
        assert sleeps == [yp.RATE_LIMIT_WAIT] == [60.0]

    def test_再試行も失敗なら送出(self, monkeypatch):
        def fake_download(tickers, **kwargs):
            raise RuntimeError("429 Too Many Requests")

        monkeypatch.setattr(yp.yf, "download", fake_download)
        with pytest.raises(RuntimeError, match="429"):
            yp._download_chunk(["7203.T"], "2y", sleeper=lambda s: None)

    def test_429以外は再試行せず送出(self, monkeypatch):
        calls: list[int] = []

        def fake_download(tickers, **kwargs):
            calls.append(1)
            raise ValueError("not rate limit")

        monkeypatch.setattr(yp.yf, "download", fake_download)
        with pytest.raises(ValueError):
            yp._download_chunk(["7203.T"], "2y", sleeper=lambda s: None)
        assert len(calls) == 1

    def test_is_rate_limited判定(self):
        assert yp._is_rate_limited(RuntimeError("HTTP 429"))
        assert yp._is_rate_limited(RuntimeError("Too Many Requests"))
        assert not yp._is_rate_limited(RuntimeError("connection reset"))


# ---------------------------------------------------------------------------
# fetch_valuation（失敗銘柄は載せない §3-1。実呼び出しはしない）
# ---------------------------------------------------------------------------


def test_fetch_valuation_失敗銘柄は結果に載せない(tmp_path, monkeypatch):
    sleeps: list[float] = []

    class FailingTicker:
        def __init__(self, ticker):
            raise RuntimeError("network down")

    monkeypatch.setattr(yp.yf, "Ticker", FailingTicker)
    result = yp.fetch_valuation(_settings(tmp_path), ["7203", "6758"], sleeper=sleeps.append)
    # 取れない銘柄は載せない（None で埋めない §3-1）。失敗はログのみ
    assert result == {}
    # 銘柄毎 0.5 秒スリープ（最終銘柄の後は不要）
    assert sleeps == [yp.VALUATION_SLEEP] == [0.5]
