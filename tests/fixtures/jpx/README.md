# JPX 信用残 PDF のフィクスチャ

取得日: 2026-09-12 / 取得元: `https://www.jpx.co.jp/markets/statistics-equities/margin/05.html`
（`syumatsu2026090400.pdf`、2026-09-04 申込み現在）

## `syumatsu_weekly_20260904.txt`

**実PDFから `pypdfium2` で抽出したテキストそのまま**（値は一切改変していない）。
PDF 本体は 867KB × 5週あるためリポジトリに入れず、様式判定に必要なヘッダ40行と
明細50行だけを切り出している。行を抜き出しただけで、各行の文字列は原本と同一。

## `expected_weekly_20260904.json`

上記テキストの期待解析結果。**kabulab-cf の TypeScript 実装
（`services/vwap-analysis/lib/margin.ts`）を同じ PDF に対して実行した出力から
生成**しており、Python 実装の出力を写したものではない（自己参照を避けている）。

## 移行ゲート G-margin-1 の検証記録（2026-09-12 実施）

実PDF 5週分（2026-08-07 / 08-14 / 08-21 / 08-28 / 09-04、計 21,143 行）に対し、
Python 実装と kabulab-cf の TypeScript 実装の出力を突き合わせた結果:

| 週 | 行数 | 完全一致 | find() 不一致 |
|---|---:|---|---:|
| 2026-08-07 | 4,231 | ✓ | 0 |
| 2026-08-14 | 4,231 | ✓ | 0 |
| 2026-08-21 | 4,231 | ✓ | 0 |
| 2026-08-28 | 4,229 | ✓ | 0 |
| 2026-09-04 | 4,226 | ✓ | 0 |

`week` 値・行数・全行の値・順序がすべて一致。PDF テキスト抽出が
`pypdfium2`（Python）と `unpdf`/pdf.js（TS）で別ライブラリでも、
解析結果は同一だった。

ライセンス: JPX サイト統計は **personal-only**。公開 API・エクスポートへ流さない。

## commit しない（重要）

このリポジトリは **PUBLIC** である。personal-only / factual-cite のフィクスチャを
commit すると、上の表で自ら禁じている「公開・再配布」をリポジトリ自身が行うことに
なる。2026-09-12 に該当 17 ファイル（約1.5MB）を**履歴ごと除去**した。

- 取得は `uv run --no-sync python scripts/capture_*.py`。手元にだけ置く
- `.gitignore` が該当拡張子を除外している。`git add -f` で強制追加しないこと
- 未取得の環境では `tests/conftest.py` の `fixture_path()` が **skip** する
  （fail ではない）。実測で 707 passed / 100 skipped / 失敗 0
- commercial-ok の EDINET 原本だけは追跡してよい
