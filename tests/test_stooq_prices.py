"""stooq 日足コレクターのテスト (DESIGN.md §4 トラックB, §2.1)。

フィクスチャ (§3-6: 実レスポンスのみ):
- prices/stooq_daily_7203.csv          … 実日足CSV（取得できた場合のみ存在。無ければ skip）
- prices/stooq_challenge_response.html … 2026-06-10 実取得のブラウザ検証ページ

注記: 2026-06 時点で stooq の CSV ダウンロードはブラウザ検証通過後も空レスポンスを
返すことがある（IP単位のダウンロード制限とみられる）。その場合 CSV フィクスチャは
未取得のまま skip となる（捏造はしない §3-6）。scripts/capture_prices.py stooq で再試行。
"""

from __future__ import annotations

import hashlib
import re

import pytest

from jp_stock_pipeline.collectors import stooq_prices as sp
from jp_stock_pipeline.config import load_settings
from jp_stock_pipeline.http import FetchError
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import Source
from jp_stock_pipeline.rawstore import sha256_bytes

from conftest import fixture_path


def _settings(tmp_path):
    return load_settings(dry_run=True, env={"RAW_DATA_DIR": str(tmp_path)})


# ---------------------------------------------------------------------------
# 実CSVフィクスチャのパース（列・型・日付昇順）
# ---------------------------------------------------------------------------


class TestParseDailyCsv:
    def test_実フィクスチャのパース(self):
        content = fixture_path("prices/stooq_daily_7203.csv").read_bytes()
        df = sp.parse_daily_csv(content)
        # 期待列 (§4: Date,Open,High,Low,Close,Volume)
        assert list(df.columns)[: len(sp.EXPECTED_COLUMNS)] == sp.EXPECTED_COLUMNS
        assert len(df) > 0
        # 型変換のみ (§5.2): Date は date 型、数値列は float
        first_date = df["Date"].iloc[0]
        assert type(first_date).__name__ == "date"
        for col in ("Open", "High", "Low", "Close", "Volume"):
            assert df[col].dtype == "float64"
        # stooq の日足CSVは日付昇順
        assert df["Date"].is_monotonic_increasing

    def test_値はCSV原文と一致(self):
        """型変換のみで値が不変であること (§5.2)。先頭データ行を原文と突合する。"""
        content = fixture_path("prices/stooq_daily_7203.csv").read_bytes()
        df = sp.parse_daily_csv(content)
        lines = content.decode("utf-8-sig").strip().splitlines()
        first = lines[1].split(",")  # lines[0] はヘッダ
        assert df["Date"].iloc[0].isoformat() == first[0]
        assert df["Open"].iloc[0] == float(first[1])
        assert df["Close"].iloc[0] == float(first[4])


# ---------------------------------------------------------------------------
# 無効レスポンス経路（欠損として扱う §3-2。ダミー生成禁止）
# ---------------------------------------------------------------------------


class TestInvalidResponses:
    def test_No_dataはFetchError(self):
        # stooq は未知銘柄等で "No data" を返す（文書化された応答文字列）
        with pytest.raises(FetchError, match="No data"):
            sp.parse_daily_csv(b"No data")

    def test_空レスポンスはFetchError(self):
        with pytest.raises(FetchError, match="空レスポンス"):
            sp.parse_daily_csv(b"")
        with pytest.raises(FetchError):
            sp.parse_daily_csv(b"   \n  ")

    def test_ブラウザ検証ページはFetchError(self):
        # 実取得したチャレンジページ（CSVでないレスポンス）を原本にしない (§3)
        html = fixture_path("prices/stooq_challenge_response.html").read_bytes()
        with pytest.raises(FetchError, match="CSVでない"):
            sp.parse_daily_csv(html)


# ---------------------------------------------------------------------------
# ブラウザ検証チャレンジ（proof-of-work）の解決
# ---------------------------------------------------------------------------


def test_solve_challenge_実チャレンジページ():
    html = fixture_path("prices/stooq_challenge_response.html").read_text(encoding="utf-8")
    solved = sp._solve_challenge(html)
    assert solved is not None
    c, n = solved
    # ページ内 JavaScript と同一の条件: sha256(c+n) 先頭が "0"*d
    m = re.search(r'const c="([^"]+)",d=(\d+)', html)
    assert m and c == m.group(1)
    digest = hashlib.sha256(f"{c}{n}".encode()).hexdigest()
    assert digest.startswith("0" * int(m.group(2)))


def test_solve_challenge_チャレンジでなければNone():
    assert sp._solve_challenge("Date,Open,High,Low,Close,Volume") is None


# ---------------------------------------------------------------------------
# fetch_daily（実フィクスチャbytesで _fetch_csv_bytes を差し替え）
# ---------------------------------------------------------------------------


def test_fetch_daily_原本保存とDataFrame(tmp_path, monkeypatch):
    raw = fixture_path("prices/stooq_daily_7203.csv").read_bytes()
    url = sp.STOOQ_DAILY_URL.format(code="7203")
    monkeypatch.setattr(sp, "_fetch_csv_bytes", lambda code, session=None: (raw, url))

    artifact, df = sp.fetch_daily(_settings(tmp_path), "7203")

    # 原本 = 生CSVバイト列そのまま (§5.1, §8.1 step 2)
    assert artifact.local_path.read_bytes() == raw
    assert artifact.sha256 == sha256_bytes(raw)
    assert artifact.source is Source.STOOQ
    assert artifact.datatype == "daily_prices"
    assert artifact.scope == "7203"
    assert artifact.license_tag is LicenseTag.PERSONAL_ONLY  # §2.1
    # データ基準日 = CSV中の最終取引日
    assert artifact.data_date == df["Date"].max()
    assert len(df) > 0


def test_fetch_daily_No_dataは原本を保存しない(tmp_path, monkeypatch):
    """無効レスポンスは原本として保存せず FetchError（欠損 §3-2）。"""
    url = sp.STOOQ_DAILY_URL.format(code="0000")
    monkeypatch.setattr(sp, "_fetch_csv_bytes", lambda code, session=None: (b"No data", url))
    settings = _settings(tmp_path)
    with pytest.raises(FetchError):
        sp.fetch_daily(settings, "0000")
    assert list(tmp_path.iterdir()) == []  # 何も保存されない
