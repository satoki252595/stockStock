"""TDnet テストフィクスチャ取得スクリプト (DESIGN.md §3-6, CONTRACTS 不変条件8)。

実APIレスポンスのみをフィクスチャとして保存する（捏造禁止）。
やのしんAPI・公式 TDnet ページとも公開エンドポイント（APIキー不要）。

使い方:
    uv run --no-sync python scripts/capture_tdnet.py [YYYYMMDD]

引数省略時は当日（JST）。土日祝は開示が無く404になるため直近営業日を指定する。
保存先: tests/fixtures/tdnet/
  - yanoshin_list_recent.json        … やのしん recent（Tdnetラップ形式）
  - yanoshin_list_{YYYYMMDD}.json    … やのしん日付指定（フラット形式）
  - official_I_list_{NNN}_{YYYYMMDD}.html … 公式一覧 全ページ
  - disclosure_{docid}.pdf           … 開示PDF 1件（最初の開示）

注意: 公式ページは直近約1ヶ月分しか公開されないため、フィクスチャの日付を
更新する際はテスト内の期待値（件数等）も実データに合わせて更新すること。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jp_stock_pipeline.http import FetchError, fetch  # noqa: E402
from jp_stock_pipeline.models import JST  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "tdnet"

YANOSHIN_BASE = "https://webapi.yanoshin.jp/webapi/tdnet/list"
OFFICIAL_BASE = "https://www.release.tdnet.info/inbs/"


def _save(name: str, content: bytes) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / name
    path.write_bytes(content)
    print(f"saved: {path} ({len(content)} bytes)")


def capture_yanoshin(datestr: str) -> dict:
    """やのしん recent + 日付指定の実レスポンスを保存する。"""
    recent = fetch(f"{YANOSHIN_BASE}/recent.json", params={"limit": 30})
    _save("yanoshin_list_recent.json", recent.content)

    daily = fetch(f"{YANOSHIN_BASE}/{datestr}.json", params={"limit": 300})
    _save(f"yanoshin_list_{datestr}.json", daily.content)
    return json.loads(daily.content)


def capture_official(datestr: str) -> None:
    """公式一覧ページを連番404停止で全ページ保存する。"""
    page_no = 1
    while page_no <= 40:
        url = f"{OFFICIAL_BASE}I_list_{page_no:03d}_{datestr}.html"
        try:
            resp = fetch(url)
        except FetchError as exc:
            if page_no == 1:
                print(f"公式一覧が取得できない（休日?）: {exc}")
            break
        _save(f"official_I_list_{page_no:03d}_{datestr}.html", resp.content)
        page_no += 1


def capture_sample_pdf(daily_payload: dict) -> None:
    """日付指定レスポンスの先頭開示の実PDFを1件保存する。"""
    for item in daily_payload.get("items", []):
        t = item.get("Tdnet", item)
        url = t.get("document_url") or ""
        if "rd.php?" in url:
            url = url.split("rd.php?", 1)[1]
        if not url.lower().endswith(".pdf"):
            continue
        resp = fetch(url)
        if resp.content.startswith(b"%PDF"):
            name = url.rsplit("/", 1)[-1]
            _save(f"disclosure_{name}", resp.content)
            return
    print("PDFを1件も取得できなかった")


def main() -> None:
    datestr = sys.argv[1] if len(sys.argv) > 1 else datetime.now(tz=JST).strftime("%Y%m%d")
    print(f"対象日: {datestr}")
    daily = capture_yanoshin(datestr)
    capture_official(datestr)
    capture_sample_pdf(daily)


if __name__ == "__main__":
    main()
