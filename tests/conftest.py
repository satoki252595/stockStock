"""テスト共通設定。

フィクスチャ方針 (DESIGN.md §3-6): 実APIレスポンスの保存物のみ使用する。
フィクスチャが未取得の場合、テストは fail ではなく skip する
（scripts/capture_*.py で実レスポンスを取得して配置する）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def fixture_path(relative: str) -> Path:
    """実レスポンスフィクスチャのパスを返す。未取得なら skip。"""
    path = FIXTURES_DIR / relative
    if not path.exists():
        pytest.skip(
            f"実レスポンスフィクスチャ未取得: {relative} "
            f"(scripts/capture_*.py で取得して配置すること。捏造フィクスチャは禁止 §3-6)"
        )
    return path


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def dry_client():
    """dry-run の NotionClient (書き込みは .ops に記録されるのみ)。"""
    from jp_stock_pipeline.notion.client import NotionClient

    return NotionClient(token=None, dry_run=True, rps=1000.0)


def notion_env(**overrides: str) -> dict[str, str]:
    """dry-run 用の Notion 環境変数（L-34。各テストの ENV を集約）。

    db_ids.json を読まず、4 DB の ID を固定値で与える。呼ばれたつど新しい
    dict を返す（テスト間の混線を防ぐ）。
    """
    env = {
        "NOTION_DB_IDS_FILE": "/nonexistent/db_ids.json",
        "NOTION_DB_STOCK_MASTER": "db-master",
        "NOTION_DB_FINANCIALS": "db-fin",
        "NOTION_DB_DISCLOSURES": "db-disc",
        "NOTION_DB_RAW_FILES": "db-raw",
    }
    env.update(overrides)
    return env


def dry_settings(tmp_path, **extra: str):
    """dry-run の Settings（L-34。raw 置き場は tmp へ向ける）。

    `EDINET_API_KEY` 等は呼び出し側が `extra` で足す。
    """
    from jp_stock_pipeline.config import load_settings

    return load_settings(
        dry_run=True,
        env={"RAW_DATA_DIR": str(tmp_path), **extra},
    )


def provenance(**overrides):
    """テスト用の Provenance（L-34。EDINET/commercial-ok 始まり）。

    TDnet/財務省由来など既定と違う来歴は呼び出し側が上書きする。
    """
    from datetime import datetime

    from jp_stock_pipeline.licensing import LicenseTag
    from jp_stock_pipeline.models import JST, Provenance, Source

    kwargs = dict(
        source=Source.EDINET,
        license_tag=LicenseTag.COMMERCIAL_OK,
        data_date=datetime(2026, 6, 10, tzinfo=JST).date(),
        fetched_at=datetime(2026, 6, 10, 19, 30, tzinfo=JST),
    )
    kwargs.update(overrides)
    return Provenance(**kwargs)
