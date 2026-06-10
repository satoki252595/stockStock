"""株価系テストフィクスチャ取得スクリプト (DESIGN.md §3-6, CONTRACTS 不変条件8)。

実レスポンスのみをフィクスチャとして保存する（捏造禁止）。
取得できなかったソースはスキップし、対応するテストは skip のままになる。

使い方:
    uv run --no-sync python scripts/capture_prices.py [stooq|yfinance|jquants ...]

引数省略時は全ソースを試行する。保存先: tests/fixtures/prices/
  - stooq_daily_7203.csv            … stooq 日足CSV（公開。ブラウザ検証PoW対応）
  - stooq_challenge_response.html   … stooq ブラウザ検証ページ（実レスポンス）
  - yfinance_daily_batch_7203.csv   … yfinance 7203.T period=2y の原本（long CSV）
  - jquants_daily_quotes_{YYYYMMDD}.jsonl … J-Quants 日次株価（要 JQUANTS_* 環境変数）
  - jquants_statements_7203.jsonl   … J-Quants 財務サマリ（同上）
  - jquants_announcement.jsonl      … J-Quants 決算発表予定（同上）

J-Quants は無料版でも登録（メール+パスワード）が必須。未設定なら自動スキップ。
データは personal-only (§2.1)。フィクスチャもリポジトリ外公開しないこと。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jp_stock_pipeline.collectors import jquants, stooq_prices, yfinance_prices  # noqa: E402
from jp_stock_pipeline.config import ConfigError, load_settings  # noqa: E402
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


def capture_jquants() -> None:
    """J-Quants 実レスポンスを保存する（要 JQUANTS_MAIL_ADDRESS / JQUANTS_PASSWORD）。"""
    settings = load_settings(env=dict(os.environ))
    settings.raw_data_dir = FIXTURES / "_tmp_raw"
    try:
        client = jquants.JQuantsClient(settings)
    except ConfigError as exc:
        print(f"J-Quants スキップ: {exc}")
        return
    # 無料版は12週遅延 (§4) → 13週前の平日を指定
    target = date.today() - timedelta(weeks=13)
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    try:
        artifact, df = jquants.daily_quotes(settings, target_date=target, client=client)
        _save(
            f"jquants_daily_quotes_{target.strftime('%Y%m%d')}.jsonl",
            artifact.local_path.read_bytes(),
        )
        print(f"  daily_quotes: {len(df)} 行")
        artifact, df = jquants.statements(settings, code=CODE, client=client)
        _save(f"jquants_statements_{CODE}.jsonl", artifact.local_path.read_bytes())
        print(f"  statements: {len(df)} 行")
        artifact, df = jquants.announcement(settings, client=client)
        _save("jquants_announcement.jsonl", artifact.local_path.read_bytes())
        print(f"  announcement: {len(df)} 行")
    except FetchError as exc:
        print(f"J-Quants 取得失敗: {exc}")


def main() -> None:
    targets = set(sys.argv[1:]) or {"stooq", "yfinance", "jquants"}
    if "stooq" in targets:
        capture_stooq()
    if "yfinance" in targets:
        capture_yfinance()
    if "jquants" in targets:
        capture_jquants()


if __name__ == "__main__":
    main()
