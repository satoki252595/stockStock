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
