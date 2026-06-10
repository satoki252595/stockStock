"""J-Quants コレクターのテスト (DESIGN.md §4 トラックB, §12)。

- 実APIは登録必須（JQUANTS_MAIL_ADDRESS / JQUANTS_PASSWORD）のため、
  実フィクスチャは scripts/capture_prices.py jquants で取得する。
  未取得（資格情報なし）の間、実データ依存のテストは skip (§3-6)
- スロットル・pagination_key ループ・429バックオフ等の制御フローは
  偽セッション（行データは空）で検証する。市場データの捏造はしない (§3-6)
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from jp_stock_pipeline.collectors import jquants as jq
from jp_stock_pipeline.config import ConfigError, load_settings
from jp_stock_pipeline.http import FetchError
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import Source
from jp_stock_pipeline.rawstore import sha256_bytes

from conftest import FIXTURES_DIR


def _settings(tmp_path, *, creds: bool = True):
    env = {"RAW_DATA_DIR": str(tmp_path)}
    if creds:
        env |= {"JQUANTS_MAIL_ADDRESS": "user@example.com", "JQUANTS_PASSWORD": "secret"}
    return load_settings(dry_run=True, env=env)


# ---------------------------------------------------------------------------
# 偽セッション（認証フロー+ページ応答。データ行は空で捏造しない §3-6）
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.content = json.dumps(payload).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self) -> dict:
        return json.loads(self.content)


class _FakeSession:
    """auth_user/auth_refresh と GET ページ列に応答する requests.Session 代替。"""

    def __init__(self, pages: list[dict] | None = None, get_statuses: list[int] | None = None):
        self.pages = list(pages or [])
        self.get_statuses = list(get_statuses or [])
        self.calls: list[tuple[str, str, dict | None]] = []  # (method, url, params)
        self.auth_count = 0

    def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params))
        if url.endswith("/token/auth_user"):
            self.auth_count += 1
            return _FakeResponse(200, {"refreshToken": "rt-1"})
        if url.endswith("/token/auth_refresh"):
            return _FakeResponse(200, {"idToken": "it-1"})
        status = self.get_statuses.pop(0) if self.get_statuses else 200
        if status != 200:
            return _FakeResponse(status, {"message": "error"})
        return _FakeResponse(200, self.pages.pop(0))


class _FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds  # スリープで時間が進む


def _client(tmp_path, session: _FakeSession) -> tuple[jq.JQuantsClient, _FakeClock]:
    clock = _FakeClock()
    client = jq.JQuantsClient(
        _settings(tmp_path), session=session, sleeper=clock.sleep, clock=clock
    )
    return client, clock


# ---------------------------------------------------------------------------
# 認証情報無しエラー (§12)
# ---------------------------------------------------------------------------


class TestConfigError:
    def test_クライアント生成はConfigError(self, tmp_path):
        with pytest.raises(ConfigError, match="JQUANTS_MAIL_ADDRESS"):
            jq.JQuantsClient(_settings(tmp_path, creds=False))

    def test_daily_quotesもConfigError(self, tmp_path):
        with pytest.raises(ConfigError):
            jq.daily_quotes(_settings(tmp_path, creds=False), target_date=date(2026, 3, 10))


# ---------------------------------------------------------------------------
# スロットル間隔計算 (§4: 5コール/分 → 12秒間隔)
# ---------------------------------------------------------------------------


class TestThrottle:
    def test_wait_seconds計算(self):
        assert jq.throttle_wait_seconds(float("-inf"), 100.0) == 0.0  # 初回は待たない
        assert jq.throttle_wait_seconds(100.0, 105.0) == 7.0  # 12秒間隔の残り
        assert jq.throttle_wait_seconds(100.0, 112.0) == 0.0
        assert jq.throttle_wait_seconds(100.0, 120.0) == 0.0
        assert jq.MIN_CALL_INTERVAL == 12.0  # 5コール/分 (§4)

    def test_backoff_secondsは指数増加で上限120(self):
        values = [jq.backoff_seconds(i) for i in range(8)]
        assert values[0] == 2.0
        assert values == sorted(values)
        assert values[-1] == 120.0

    def test_全APIコールが12秒間隔(self, tmp_path):
        # 認証2コール+GET2コール = 4コール。初回以外は毎回12秒待つ
        session = _FakeSession(pages=[{"daily_quotes": []}, {"daily_quotes": []}])
        client, clock = _client(tmp_path, session)
        client.get_raw("prices/daily_quotes", {"date": "20260310"})
        client.get_raw("prices/daily_quotes", {"date": "20260311"})
        assert len(session.calls) == 4  # auth_user, auth_refresh, GET, GET
        assert clock.sleeps == [12.0, 12.0, 12.0]
        assert session.auth_count == 1  # idToken はメモリ保持で再利用（24h有効）


# ---------------------------------------------------------------------------
# 429 指数バックオフ
# ---------------------------------------------------------------------------


def test_429は指数バックオフ後に再試行(tmp_path):
    # get_statuses は GET（データ取得コール）のみが消費する（認証は常に200）
    session = _FakeSession(pages=[{"daily_quotes": []}], get_statuses=[429, 429, 200])
    client, clock = _client(tmp_path, session)
    raw, payload = client.get_raw("prices/daily_quotes", {"date": "20260310"})
    assert payload == {"daily_quotes": []}
    # スロットル12秒 ×(コール毎) に加えて 429 バックオフ 2,4 秒が入る
    assert 2.0 in clock.sleeps and 4.0 in clock.sleeps


def test_4xxはFetchError(tmp_path):
    session = _FakeSession(get_statuses=[403])
    client, _clock = _client(tmp_path, session)
    with pytest.raises(FetchError, match="403"):
        client.get_raw("prices/daily_quotes", {"date": "20260310"})


# ---------------------------------------------------------------------------
# pagination_key ループ (§12)
# ---------------------------------------------------------------------------


def test_pagination_keyループ(tmp_path):
    pages = [
        {"daily_quotes": [], "pagination_key": "key-1"},
        {"daily_quotes": [], "pagination_key": "key-2"},
        {"daily_quotes": []},  # pagination_key 無し → 終端
    ]
    session = _FakeSession(pages=pages)
    client, _clock = _client(tmp_path, session)

    raw, rows = jq.fetch_paginated(client, "prices/daily_quotes", {"date": "20260310"}, "daily_quotes")

    gets = [c for c in session.calls if c[0] == "GET"]
    assert len(gets) == 3
    # 2ページ目以降は元パラメータ + pagination_key
    assert gets[0][2] == {"date": "20260310"}
    assert gets[1][2] == {"date": "20260310", "pagination_key": "key-1"}
    assert gets[2][2] == {"date": "20260310", "pagination_key": "key-2"}
    # 原本 = 各ページ生レスポンスの改行連結 (JSON Lines。モジュール docstring の定義)
    expected = b"\n".join(json.dumps(p).encode("utf-8") for p in pages)
    assert raw == expected
    assert jq.parse_raw_pages(raw) == pages
    assert rows == []


def test_data_key欠落はFetchError(tmp_path):
    session = _FakeSession(pages=[{"message": "maintenance"}])
    client, _clock = _client(tmp_path, session)
    with pytest.raises(FetchError, match="daily_quotes"):
        jq.fetch_paginated(client, "prices/daily_quotes", {"date": "20260310"}, "daily_quotes")


# ---------------------------------------------------------------------------
# daily_quotes の原本保存（偽クライアント・行データ空）
# ---------------------------------------------------------------------------


def test_daily_quotes_原本保存とメタデータ(tmp_path):
    session = _FakeSession(pages=[{"daily_quotes": []}])
    client, _clock = _client(tmp_path, session)

    artifact, df = jq.daily_quotes(
        _settings(tmp_path), target_date=date(2026, 3, 10), client=client
    )

    raw = artifact.local_path.read_bytes()
    assert raw == json.dumps({"daily_quotes": []}).encode("utf-8")
    assert artifact.sha256 == sha256_bytes(raw)
    assert artifact.source is Source.JQUANTS
    assert artifact.datatype == "daily_quotes"
    assert artifact.scope == "ALL"
    assert artifact.data_date == date(2026, 3, 10)
    assert artifact.license_tag is LicenseTag.PERSONAL_ONLY  # §2.1 厳守
    assert "date=20260310" in artifact.url
    assert len(df) == 0  # 行が無ければ無いまま（補完しない §3-1）


def test_statements_引数はcodeかdateのどちらか一方(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(ValueError):
        jq.statements(settings)
    with pytest.raises(ValueError):
        jq.statements(settings, code="7203", target_date=date(2026, 3, 10))


# ---------------------------------------------------------------------------
# 実フィクスチャ（未取得なら skip。scripts/capture_prices.py jquants で取得）
# ---------------------------------------------------------------------------


def _glob_fixture(pattern: str):
    matches = sorted((FIXTURES_DIR / "prices").glob(pattern))
    if not matches:
        pytest.skip(
            f"実レスポンスフィクスチャ未取得: prices/{pattern} "
            "(scripts/capture_prices.py jquants — 要 JQUANTS_* 資格情報。捏造禁止 §3-6)"
        )
    return matches[0]


def test_実フィクスチャ_daily_quotesのDataFrame化():
    path = _glob_fixture("jquants_daily_quotes_*.jsonl")
    pages = jq.parse_raw_pages(path.read_bytes())
    rows = [r for p in pages for r in p["daily_quotes"]]
    assert len(rows) > 0
    df = jq.rows_to_dataframe(rows, date_columns=("Date",))
    # 列名はAPIのまま (§5.2)
    assert set(rows[0]) <= set(df.columns)
    assert "Code" in df.columns
    # Date のみ date 型へ（型変換のみ・値不変）
    assert df["Date"].iloc[0].isoformat() == rows[0]["Date"]
    # 値不変: 先頭行を原文と突合
    first = rows[0]
    for key, value in first.items():
        if key == "Date":
            continue
        got = df[key].iloc[0]
        assert (value is None and (got is None or got != got)) or got == value


def test_実フィクスチャ_statementsのDataFrame化():
    path = _glob_fixture("jquants_statements_*.jsonl")
    pages = jq.parse_raw_pages(path.read_bytes())
    rows = [r for p in pages for r in p["statements"]]
    assert len(rows) > 0
    df = jq.rows_to_dataframe(rows, date_columns=("DisclosedDate",))
    assert len(df) == len(rows)
    assert df["DisclosedDate"].iloc[0].isoformat() == rows[0]["DisclosedDate"]
