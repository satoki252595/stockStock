# 実装契約 (CONTRACTS)

実装エージェント向けの内部契約。`docs/DESIGN.md` が正本。矛盾したら DESIGN.md が勝つ。

## 不変条件（全モジュール共通・絶対遵守）

1. **原本必須** (§8.1-4): 構造化データの upsert より前に、原本+変換版が ⑤原本ファイルDB へアップロードされていること。アップロード失敗時は構造化データを書かずに異常終了
2. **真実性** (§3): ダミー・サンプル・推定値・補間値を生成するコードを書かない。フォールバックは実在ソースの実データのみ。取得失敗は None / 欠損として記録
3. **来歴** (§3-3): 全レコードに `Provenance`（ソース・ライセンスタグ・データ基準日・取得日時・原本リレーション）を設定
4. **ライセンス継承** (§2.2): 計算値は `licensing.inherit()` で最も厳しいタグを継承
5. **冪等 upsert** (§8.1-6): キー = 銘柄コード(①②) / 銘柄×決算期末×開示種別(③) / docID(④) / SHA256(⑤)
6. **変換は値不変** (§5.2): 型変換・縦持ち化・文字コード正規化のみ。値の修正・丸め・補完禁止
7. **dry-run** (§3-6): 全ジョブは `--dry-run` で Notion に書き込まずに動作する
8. **テストフィクスチャは実レスポンスのみ** (§3-6): 捏造禁止。取得不能（要APIキー等）なら `tests/conftest.py fixture_path()` の skip 機構を使い、`scripts/capture_*.py` に取得スクリプトを置く
9. **銘柄コード正規化は `contracts/stock_code.py` だけ**（下の「既知例外」2 件を除く。新しい例外を作らない）: 正規表現も 5 文字→4 文字の切り出しも他モジュールに書かない。3 操作（`normalize_stock_code` / `parse_stock_code` / `source_code_to_ticker`）を使い分ける。期待値は共有テストベクタ `tests/fixtures/contracts/stock-code-vectors.json` が正で、kabulab-cf `src/shared/jpx/stock-code.ts` と同一バイト列のファイルを共有する（CI の `cross-repo-contract` ジョブが diff する）

## 銘柄コード契約

| 操作 | 入力 | 出力 | 使う場面 |
|---|---|---|---|
| `normalize_stock_code` | 任意 | 正規化済み文字列（妥当性は見ない） | 表示・比較の前処理 |
| `parse_stock_code` | 任意 | 4 文字の正準形 or `None` | ①②由来の値の検証、URL パラメータ |
| `source_code_to_ticker` | TDnet `company_code` / EDINET `secCode` | 4 文字の正準形 or `None` | ③④の取込 |

正準形は `^[0-9]{3}[0-9A-Z]$`（4 文字）。JPX は 2024 年 1 月から英字入りコードを
付番しており、英字は仕様上 2 桁目と 4 桁目を取りうるが、本番 `core_stocks` の
英字コード 174 件は全件 4 桁目のみ（実測）。`1A00` 台の付番が始まったら
パターンと共有テストベクタを同時に更新する。

**5 文字形式の 4 文字化は末尾検査文字が `"0"` のときだけ行う。** 「5 文字なら
先頭 4 文字」で切ると別の証券を取り違える（`"25935"` 伊藤園第1種優先株式 →
`"2593"` 同社普通株。本番に両方が実在する）。TDnet は実測で全件末尾 `"0"`
だが、**EDINET については末尾 `"0"` 限定の実測根拠が無い**（生 `secCode` を
保存する表が無く分布が取れない）。

### 既知例外（レビュー時に見つかった未解決分）

この 2 系統は正準実装へ寄せていない。**寄せ忘れではなく、寄せられない/寄せると
壊れる理由がある。** 不変条件9 を読んで「もう 1 箇所も無い」と思わないこと。

1. **`worker/src/shared/routes.ts` の `isValidCode`**（公開 API / MCP の入力検証）。
   worker は別言語・別ビルドで Python を import できないため、正規表現リテラルを
   持つしかない。代わりに `worker/test/stock-code-contract.test.ts` が共有テスト
   ベクタの `canonical_regex` と**リテラルを直接突合**して固定する。
   ここは**正規化しない**（URL パスがキャッシュキー・D1 述語になる面なので、
   表記揺れを吸収すると同じ銘柄に複数の URL ができる）。
2. **`collectors/jpx_margin.py` の `parse_margin_text`**（と kabulab-cf
   `services/vwap-analysis/lib/margin.ts` の `parseMarginText`）。JPX 信用残 PDF の
   5 文字コードを `[:4]` / `.slice(0, 4)` で**無条件に切っている**＝本 PR が
   「取り違え」と呼んだ規則そのものが残っている。`source_code_to_ticker` に
   寄せなかった理由: 信用銘柄には ETF/REIT が含まれ、これらの検査文字は `"0"` で
   ない可能性が高い（TDnet では ETF `1671` が `"16714"`）。末尾 `"0"` 限定に
   すると正当な行を大量に落としうるため、**実 PDF で検査文字の分布を測るまで
   触らない**。フィクスチャは規約上コミットできず該当テストは常時 skip なので、
   回帰検知はゼロのまま。種類株が信用残に現れれば同じ取り違えが起きる。

## コアモジュール（実装済み・変更時は要注意）

| モジュール | 提供物 |
|---|---|
| `config.py` | `load_settings()` → `Settings`（トークン・APIキー・`db_id(key)`・`notion_rps`・`dry_run`・`raw_data_dir`） |
| `licensing.py` | `LicenseTag`, `source_license()`, `inherit()`, `is_publishable()`, `is_metadata_publishable()`, `ATTRIBUTION`, `COMMERCIALIZATION_CHECKLIST` |
| `models.py` | `Source`, `DataQuality`, `ConvertStatus`, `JST`, `now_jst()`, `Provenance`, `RawArtifact`, `StockMasterRecord`, `PriceTechnicalRecord`, `FinancialSummaryRecord`, `DisclosureRecord` |
| `rawstore.py` | `save_raw()`, `raw_filename()`, `converted_filename()`, `sha256_bytes()` |
| `http.py` | `fetch()`（リトライ3回・指数バックオフ）, `FetchError` |
| `notion/client.py` | `NotionClient`（スロットル・429バックオフ・dry-run記録 `.ops`・`raw_api()`） |

DB論理キー（`Settings.db_id()` / schema.py / upsert.py で共通）:
`stock_master` / `prices` / `financials` / `disclosures` / `raw_files` / `exports` / `job_log`

## バンドル別ファイル所有権（並列実装時の衝突防止）

| バンドル | 所有ファイル |
|---|---|
| A: Notion層 | `notion/schema.py`, `notion/upsert.py`, `notion/file_upload.py`, `tests/test_schema.py`, `tests/test_upsert.py`, `tests/test_file_upload.py` |
| B: EDINET | `collectors/edinet.py`, `collectors/edinet_codelist.py`, `convert/xbrl_to_csv.py`, `scripts/capture_edinet.py`, `tests/test_edinet*.py`, `tests/test_xbrl_to_csv.py`, `tests/fixtures/edinet/` |
| C: TDnet | `collectors/tdnet_yanoshin.py`, `collectors/tdnet_official_fallback.py`, `scripts/capture_tdnet.py`, `tests/test_tdnet*.py`, `tests/fixtures/tdnet/` |
| D: 株価系 | `collectors/yfinance_prices.py`, `collectors/stooq_prices.py`, `scripts/capture_prices.py`, `tests/test_yfinance*.py`, `tests/test_stooq*.py`, `tests/fixtures/prices/` |
| E: 変換(非XBRL) | `convert/json_to_parquet.py`, `convert/pdf_to_text.py`, `convert/xls_to_csv.py`, `scripts/capture_convert_fixtures.py`, `tests/test_convert*.py`, `tests/fixtures/convert/` |
| F: transform | `transform/technicals.py`, `transform/normalize.py`, `transform/reconcile.py`, `scripts/capture_transform_fixtures.py`, `tests/test_technicals.py`, `tests/test_normalize.py`, `tests/test_reconcile.py`, `tests/fixtures/transform/` |
| G: ジョブ+CI | `jobs/*.py`, `.github/workflows/*.yml`, `tests/test_jobs*.py`（A〜F完了後に実装） |

## 注意: HTTP 200 のエラーレスポンス

`http.fetch()` は HTTP 200 で返る本文の真偽判定をしない（docstring 通り）。
実在の例: EDINET は 200 + `{"StatusCode":401}` JSON、stooq は 200 + ブラウザ検証
HTML を返す。**全コレクターは本文の内容検証必須**（マジックバイト・JSON 構造・
CSV ヘッダ等）。エラー応答を原本として保存してはならない (§3)。

## XBRL tidy 形式（B が生成、F が消費）

§5.2「財務項目をフラットな tidy 形式」の列定義（CSV/Parquet 共通・この順）:

```
code           : str  銘柄コード4桁（不明は空文字）
doc_id         : str  書類管理番号
element        : str  XBRL要素名（プレフィックス込み 例 jppfs_cor:NetSales）
context_ref    : str  コンテキストID（原文のまま）
period_start   : str  ISO日付 or 空
period_end     : str  ISO日付 or 空
instant_date   : str  ISO日付 or 空（instant型のとき）
consolidated   : str  "連結"/"単体"/""（コンテキストから判別できた場合のみ）
unit           : str  単位（原文のまま 例 JPY, shares。無ければ空）
value          : str  値（**原文の文字列をそのまま**。数値化は transform 側で行う）
```

## 移行の切替判定 (G-fin-1) は「値一致」ではない

`cloud_store/fin_parity.py` が正本。③断面の writer 交代 (P5) の判定は
**項目ごとに閾値が違う**。`per` / `pbr` だけ相対誤差 ≤ 1e-6（実測 worst
1.4e-07）で、`eps` / `bps` / `roe` / `price` には**値一致の閾値を置かない**
（Yahoo が TTM を改訂するので原理的に一致しない。実測 eps 1,611 行中
1e-6 での一致は 76 行）。代わりに導出整合・符号・欠損パターンで見る。

**「G-fin-1 が通った」を「値が一致した」と読み替えないこと。** 閾値なしの項目は
レポートに「値一致は判定していない（観測のみ）」と出る。詳細と実測値は
`fin_parity.py` の docstring と `docs/CF-CANONICAL-DESIGN.md` の P5 節。

## transform → upsert の受け渡し

`models.py` の `*Record` dataclass のみを使う。数値が取れない項目は None のまま。

## ⑤ 原本アップロードの契約（A が実装、全ジョブが使用）

```python
notion.file_upload.upload_raw_artifact(client, settings, artifact: RawArtifact) -> str
```
- SHA256 で ⑤ を検索 → 既存ならその page_id を返す（重複スキップ §8.1-2）
- 新規なら File Upload API（20MB以下single_part / 超はmulti_part）で原本+変換版を添付し行作成
- 失敗時は `RawUploadError` を送出（呼び出し側=ジョブは構造化書き込みを中止）
- 成功時 `artifact.notion_page_id` を設定して返す

## ジョブの骨格（G が実装）

各ジョブ: `python -m jp_stock_pipeline.jobs.<name> [--dry-run] [--date YYYY-MM-DD] [--limit N]`
フロー = Fetch → save_raw → convert → upload_raw_artifact → transform → upsert → ⑦ジョブログ記録 (§8.1)。
`prices_daily` は ② upsert のあと、①銘柄ページ配下の株価テクニカル履歴子DBへ同じスナップショットを日付キーで追記する（8,000行で次シャード。`--skip-history` で省略可）。
部分失敗は続行して最後に「一部失敗」でログ、原本アップロード失敗はその取得単位の構造化書き込みを中止。
