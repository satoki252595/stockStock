"""FastAPI 配信の認証・ルーティング・シリアライズ (DB はフェイク _query)。

_query をパッチして実 PostgreSQL なしで、API キー認証・404・上限バリデーション・
日付の JSON シリアライズ・全保護エンドポイントの認証要求を検証する。
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from jp_stock_pipeline.config import LocalStoreSettings
from jp_stock_pipeline.local_store import api as api_mod


def _settings(api_key: str | None = "secret") -> LocalStoreSettings:
    return LocalStoreSettings(
        host="testhost", port=5432, dbname="db", user="u", password="p", api_key=api_key
    )


@pytest.fixture
def make_client(monkeypatch):
    def _make(rows, *, api_key: str | None = "secret") -> TestClient:
        monkeypatch.setattr(api_mod, "_query", lambda settings, sql, params=None: list(rows))
        return TestClient(api_mod.create_app(_settings(api_key)))

    return _make


def test_health_needs_no_auth(make_client):
    c = make_client([])
    assert c.get("/health").json() == {"status": "ok"}


def test_requires_valid_api_key(make_client):
    c = make_client([{"code": "7203"}])
    assert c.get("/stocks").status_code == 401
    assert c.get("/stocks", headers={"X-API-Key": "bad"}).status_code == 401
    assert c.get("/stocks", headers={"X-API-Key": "secret"}).status_code == 200


def test_no_auth_when_key_unset(make_client):
    # api_key 未設定なら認証なしでアクセス可（信頼できる LAN 内前提）
    c = make_client([{"code": "7203"}], api_key=None)
    assert c.get("/stocks").status_code == 200


def test_stock_list_serializes_dates(make_client):
    c = make_client(
        [{"code": "7203", "name": "トヨタ", "listed": True, "listing_date": date(2020, 1, 1)}]
    )
    r = c.get("/stocks", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    body = r.json()
    assert body[0]["code"] == "7203"
    assert body[0]["listing_date"] == "2020-01-01"  # date は ISO 文字列へ


def test_get_stock_404_when_empty(make_client):
    c = make_client([])
    assert c.get("/stocks/9999", headers={"X-API-Key": "secret"}).status_code == 404


def test_get_stock_found(make_client):
    c = make_client([{"code": "7203", "name": "トヨタ"}])
    r = c.get("/stocks/7203", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    assert r.json()["code"] == "7203"


def test_prices_timeseries(make_client):
    c = make_client(
        [
            {"code": "7203", "data_date": date(2026, 6, 10), "close": 2500.0},
            {"code": "7203", "data_date": date(2026, 6, 9), "close": 2480.0},
        ]
    )
    r = c.get("/prices/7203?from=2026-06-01&to=2026-06-10", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    assert len(r.json()) == 2
    assert r.json()[0]["close"] == 2500.0


def test_limit_over_max_is_422(make_client):
    c = make_client([])
    r = c.get("/prices/7203?limit=999999", headers={"X-API-Key": "secret"})
    assert r.status_code == 422  # le=5000 超過


def test_all_secured_endpoints_reject_without_key(make_client):
    c = make_client([])
    for path in [
        "/stocks", "/stocks/7203", "/prices/7203", "/financials/7203",
        "/disclosures", "/raw", "/jobs",
    ]:
        assert c.get(path).status_code == 401, path
