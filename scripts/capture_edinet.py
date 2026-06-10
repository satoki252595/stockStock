"""EDINET 実レスポンスフィクスチャ取得スクリプト (DESIGN.md §3-6, CONTRACTS.md 不変条件8)。

テストフィクスチャは実レスポンスの保存物のみ使用する（捏造禁止）。
EDINET API v2 は Subscription-Key 必須のため、本スクリプトを APIキー所有者が
実行して tests/fixtures/edinet/ に実データを配置する。未配置のテストは skip される。

使い方:
    EDINET_API_KEY=xxxx uv run --no-sync python scripts/capture_edinet.py [--date YYYY-MM-DD]

取得物（tests/fixtures/edinet/）:
    Edinetcode.zip               EDINETコードリスト（キー不要・常時再取得）
    documents_list_sample.json   書類一覧 (documents.json?type=2) の生レスポンス
    xbrl_sample.zip / .meta.json 対象書類の XBRL zip (type=1) と書類メタ（実一覧の1件）
    csv_sample.zip  / .meta.json 同 CSV zip (type=5)
    pdf_sample.pdf  / .meta.json 同 PDF (type=2)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jp_stock_pipeline.collectors.edinet import (  # noqa: E402
    EDINET_API_BASE,
    has_sec_code,
    is_target_document,
)
from jp_stock_pipeline.collectors.edinet_codelist import CODELIST_URL  # noqa: E402
from jp_stock_pipeline.http import FetchError, fetch  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "edinet"


def _last_weekday(today: date) -> date:
    """直近の平日（書類一覧が空になりにくい日付の既定値）。"""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # 土日
        d -= timedelta(days=1)
    return d


def _save(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    print(f"保存: {path} ({len(content)} bytes)")


def capture_codelist() -> None:
    """EDINETコードリスト（公開・キー不要）を再取得する。"""
    resp = fetch(CODELIST_URL)
    _save(FIXTURE_DIR / "Edinetcode.zip", resp.content)


def capture_documents_list(api_key: str, target_date: date) -> list[dict]:
    """書類一覧の生レスポンスを保存し results を返す。"""
    resp = fetch(
        f"{EDINET_API_BASE}/documents.json",
        params={"date": target_date.isoformat(), "type": 2, "Subscription-Key": api_key},
    )
    body = json.loads(resp.content)
    status = str(body.get("metadata", {}).get("status", body.get("StatusCode", "")))
    if status != "200":
        raise SystemExit(f"書類一覧がエラーレスポンス: {body.get('message', body)}")
    _save(FIXTURE_DIR / "documents_list_sample.json", resp.content)
    return body.get("results", [])


def capture_document_files(api_key: str, results: list[dict]) -> None:
    """証券コード付き・XBRL ありの対象書類1件について type=1/2/5 を保存する。"""
    candidates = [
        d
        for d in results
        if is_target_document(d) and has_sec_code(d) and str(d.get("xbrlFlag")) == "1"
    ]
    if not candidates:
        print("対象書類（secCode あり・xbrlFlag=1）が無い日付。--date を変えて再実行すること")
        return
    doc = candidates[0]
    doc_id = doc["docID"]
    print(f"サンプル書類: docID={doc_id} secCode={doc.get('secCode')} {doc.get('docDescription')}")

    for fetch_type, filename in ((1, "xbrl_sample.zip"), (5, "csv_sample.zip"), (2, "pdf_sample.pdf")):
        try:
            resp = fetch(
                f"{EDINET_API_BASE}/documents/{doc_id}",
                params={"type": fetch_type, "Subscription-Key": api_key},
            )
        except FetchError as exc:
            print(f"type={fetch_type} 取得失敗（スキップ・捏造はしない）: {exc}")
            continue
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            print(f"type={fetch_type} はエラーレスポンスのため保存しない: {resp.content[:200]!r}")
            continue
        _save(FIXTURE_DIR / filename, resp.content)
        # テストが docID/secCode を参照するためのメタ（実一覧の1件をそのまま保存）
        meta_path = FIXTURE_DIR / f"{filename.rsplit('.', 1)[0]}.meta.json"
        meta_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"保存: {meta_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=_last_weekday(date.today()),
        help="書類一覧の対象日 (YYYY-MM-DD)。既定は直近の平日",
    )
    args = parser.parse_args()

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    capture_codelist()

    api_key = os.environ.get("EDINET_API_KEY")
    if not api_key:
        raise SystemExit(
            "EDINET_API_KEY が未設定。書類一覧・書類本体のフィクスチャ取得には必須。"
            "（コードリストのみ取得済み）"
        )
    results = capture_documents_list(api_key, args.date)
    print(f"書類一覧: {len(results)} 件 ({args.date})")
    capture_document_files(api_key, results)


if __name__ == "__main__":
    main()
