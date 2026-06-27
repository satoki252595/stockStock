"""株価系テストフィクスチャ取得スクリプト (DESIGN.md §3-6, CONTRACTS 不変条件8)。

実レスポンスのみをフィクスチャとして保存する（捏造禁止）。
取得できなかったソースはスキップし、対応するテストは skip のままになる。

使い方:
    uv run --no-sync python scripts/capture_prices.py [stooq|yfinance ...]

引数省略時は全ソースを試行する。保存先: tests/fixtures/prices/
  - stooq_daily_7203.csv            … stooq 日足CSV（公開。ブラウザ検証PoW対応）
  - stooq_challenge_response.html   … stooq ブラウザ検証ページ（実レスポンス）
  - yfinance_daily_batch_7203.csv   … yfinance 7203.T period=2y の原本（long CSV）

株価系は personal-only (§2.1)。フィクスチャもリポジトリ外公開しないこと。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jp_stock_pipeline.collectors import stooq_prices, yfinance_prices  # noqa: E402
from jp_stock_pipeline.config import load_settings  # noqa: E402
from jp_stock_pipeline.http import FetchError  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "prices"
CODE = "7203"  # トヨタ自動車（フィクスチャ用の実在銘柄）


def _save(name: str, content: bytes) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / name
    path.write_bytes(content)
    print(f"saved: {path} ({len(content)} bytes)")


def capture_stooq() -> None:
    """stooq 7203 日足CSVを実取得する（ブラウザ検証チャレンジは PoW を解いて通過）。"""
    try:
        content, url = stooq_prices._fetch_csv_bytes(CODE)
    except FetchError as exc:
        print(f"stooq 取得失敗: {exc}")
        return
    head = content[:4096].decode("utf-8", errors="replace")
    if "__verify" in head:
        # チャレンジページ自体も実レスポンスとして保存（エラー経路テスト用）
        _save("stooq_challenge_response.html", content)
        print("stooq: ブラウザ検証を通過できなかった（チャレンジページを保存）")
        return
    try:
        stooq_prices.parse_daily_csv(content)  # 実CSVであることを検証してから保存
    except FetchError as exc:
        print(f"stooq: 実CSVが得られなかったため保存しない（捏造禁止 §3-6）: {exc}")
        return
    _save(f"stooq_daily_{CODE}.csv", content)


def capture_yfinance() -> None:
    """yfinance 7203.T period=2y を実取得し、原本（long CSV）として保存する。"""
    settings = load_settings(dry_run=True, env={"RAW_DATA_DIR": str(FIXTURES / "_tmp_raw")})
    artifact, frames, missing = yfinance_prices.fetch_daily_batch(settings, [CODE], period="2y")
    if CODE not in frames:
        print(f"yfinance: {CODE} を取得できなかった (missing={missing})")
        return
    _save(f"yfinance_daily_batch_{CODE}.csv", artifact.local_path.read_bytes())


def main() -> None:
    targets = set(sys.argv[1:]) or {"stooq", "yfinance"}
    if "stooq" in targets:
        capture_stooq()
    if "yfinance" in targets:
        capture_yfinance()


if __name__ == "__main__":
    main()
