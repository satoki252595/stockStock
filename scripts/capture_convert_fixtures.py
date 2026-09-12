"""バンドルE（変換レイヤー・非XBRL）の実レスポンスフィクスチャ取得スクリプト (§3-6)。

テストフィクスチャは実レスポンスの保存物のみ使用する（捏造禁止 §3-6）。
本スクリプトは公開エンドポイントから以下を tests/fixtures/convert/ に保存する:

1. yanoshin_tdnet_recent.json
   やのしんTDnet WEB-API の直近開示一覧（公開API・キー不要）
2. tdnet_disclosure_sample.pdf
   上記一覧の document_url から実開示PDFを1件
   （factual-cite §2.1: 原文は内部保管・テスト検証のみに留める）
   併せて出所を tdnet_disclosure_sample.source.txt に記録する
3. data_j.xls
   JPX 上場銘柄一覧（personal-only §2.1: 私的検証用。詳細は同階層 README.md）

使い方:
    uv run --no-sync python scripts/capture_convert_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jp_stock_pipeline.http import FetchError, fetch
from jp_stock_pipeline.models import now_jst

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "convert"

YANOSHIN_RECENT_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/recent.json?limit=5"
# JPX は 2026-09 に配布形式を .xls から .xlsx へ差し替えた。旧 URL は HTTP 404。
# 一覧ページ https://www.jpx.co.jp/markets/statistics-equities/misc/01.html が正。
JPX_DATA_J_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
)
JPX_DATA_J_NAME = "data_j.xlsx"


def _direct_pdf_url(document_url: str) -> str:
    """やのしんの rd.php リダイレクトURLから直接URLを取り出す。"""
    marker = "rd.php?"
    if marker in document_url:
        return document_url.split(marker, 1)[1]
    return document_url


def capture_yanoshin_json() -> dict:
    """直近開示一覧JSONを取得・保存し、パース結果を返す。"""
    resp = fetch(YANOSHIN_RECENT_URL)
    out = FIXTURES_DIR / "yanoshin_tdnet_recent.json"
    out.write_bytes(resp.content)  # 原本バイト列を無加工で保存
    print(f"saved: {out} ({len(resp.content)} bytes)")
    return json.loads(resp.content)


def capture_tdnet_pdf(listing: dict) -> None:
    """一覧から最初のPDF開示を1件ダウンロードして保存する。"""
    for item in listing.get("items", []):
        tdnet = item.get("Tdnet", {})
        document_url = tdnet.get("document_url") or ""
        if not document_url.lower().endswith(".pdf"):
            continue
        url = _direct_pdf_url(document_url)
        resp = fetch(url)
        out = FIXTURES_DIR / "tdnet_disclosure_sample.pdf"
        out.write_bytes(resp.content)
        meta = FIXTURES_DIR / "tdnet_disclosure_sample.source.txt"
        meta.write_text(
            f"source_url: {url}\n"
            f"document_url(yanoshin): {document_url}\n"
            f"fetched_at: {now_jst().isoformat()}\n"
            f"company: {tdnet.get('company_code', '')} {tdnet.get('company_name', '')}\n"
            f"title: {tdnet.get('title', '')}\n",
            encoding="utf-8",
        )
        print(f"saved: {out} ({len(resp.content)} bytes) <- {url}")
        return
    print("WARN: 一覧にPDFの document_url が見つからなかった（PDF未保存）", file=sys.stderr)


def capture_jpx_data_j() -> None:
    """JPX data_j.xlsx（上場銘柄一覧）を取得・保存する。personal-only (§2.1)。

    マジックバイトを検証する。JPX が配布形式を差し替えたとき（実際 2026-09 に
    .xls → .xlsx が起きた）、404 でない HTML を掴んで「取得できた」ことにしない。
    """
    resp = fetch(JPX_DATA_J_URL)
    # .xlsx は zip (PK\x03\x04)。旧 .xls の OLE2 ヘッダとも HTML とも違う。
    if not resp.content.startswith(b"PK\x03\x04"):
        raise FetchError(
            f"JPX 上場銘柄一覧が xlsx (zip) でない: head={resp.content[:16]!r}"
            " — 配布形式/URL が変わっていないか一覧ページで確認すること"
        )
    out = FIXTURES_DIR / JPX_DATA_J_NAME
    out.write_bytes(resp.content)
    print(f"saved: {out} ({len(resp.content)} bytes)")


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    listing: dict | None = None
    try:
        listing = capture_yanoshin_json()
    except FetchError as exc:
        failures += 1
        print(f"WARN: やのしんJSON取得失敗（テストは skip される）: {exc}", file=sys.stderr)
    if listing is not None:
        try:
            capture_tdnet_pdf(listing)
        except FetchError as exc:
            failures += 1
            print(f"WARN: TDnet PDF取得失敗（テストは skip される）: {exc}", file=sys.stderr)
    try:
        capture_jpx_data_j()
    except FetchError as exc:
        failures += 1
        print(
            f"WARN: JPX {JPX_DATA_J_NAME} 取得失敗（テストは skip される）: {exc}",
            file=sys.stderr,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
