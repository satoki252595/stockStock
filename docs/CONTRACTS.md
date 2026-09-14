# 実装契約 (CONTRACTS)

実装エージェント向けの内部契約。`docs/DESIGN.md` が正本。矛盾したら DESIGN.md が勝つ。

## 不変条件（全モジュール共通・絶対遵守）

1. **原本必須** (§8.1-4): 構造化データの upsert より前に、原本+変換版が ⑤原本ファイルDB へアップロードされていること。アップロード失敗時は構造化データを書かずに異常終了
2. **真実性** (§3): ダミー・サンプル・推定値・補間値を生成するコードを書かない。フォールバックは実在ソースの実データのみ。取得失敗は None / 欠損として記録
3. **来歴** (§3-3): 全レコードに `Provenance`（ソース・ライセンスタグ・データ基準日・取得日時・原本リレーション）を設定
4. **ライセンス継承** (§2.2): 計算値は `licensing.inherit()` で最も厳しいタグを継承
5. **冪等 upsert** (§8.1-6): キー = 銘柄コード(①) / 銘柄×決算期末×開示種別(③) / docID(④) / SHA256(⑤)
6. **変換は値不変** (§5.2): 型変換・縦持ち化・文字コード正規化のみ。値の修正・丸め・補完禁止
7. **dry-run** (§3-6): 全ジョブは `--dry-run` で Notion に書き込まずに動作する
8. **テストフィクスチャは実レスポンスのみ** (§3-6): 捏造禁止。取得不能（要APIキー等）なら `tests/conftest.py fixture_path()` の skip 機構を使い、`scripts/capture_*.py` に取得スクリプトを置く
9. **銘柄コード正規化は `contracts/stock_code.py` だけ**（下の「既知例外」2 件を除く。新しい例外を作らない）: 正規表現も 5 文字→4 文字の切り出しも他モジュールに書かない。3 操作（`normalize_stock_code` / `parse_stock_code` / `source_code_to_ticker`）を使い分ける。期待値は共有テストベクタ `tests/fixtures/contracts/stock-code-vectors.json` が正で、kabulab-cf `src/shared/jpx/stock-code.ts` と同一バイト列のファイルを共有する（CI の `cross-repo-contract` ジョブが diff する）

10. **列単位ライセンス地図は `cloud_store/schema.MIXED_LICENSE_COLUMNS` と
   `cloud_store/governance.TABLE_LICENSE` の 2 つだけ**。地図に無い (表, 列) は
   「公開してよいと決まっていない」= 公開投影に入れない（allowlist であって
   denylist ではない）。タグは「値が第三者の著作物・データセットを含むか」を
   答える列で、「公開 API が出すべきか」とは別（後者は API 設計の判断）。
   D1 の `jss_column_license` へ投入するのは `jobs/license_map.py` **だけ**で、
   期待値は共有契約 `tests/fixtures/contracts/d1-license-map.json`（kabulab-cf と
   同一バイト列。CI の `cross-repo-contract` が diff する）

## 列単位ライセンス地図

| 区分 (`TableKind`) | 判定根拠 | 例 |
|---|---|---|
| `column-map` | `MIXED_LICENSE_COLUMNS`（1 行に混在） | `core_stocks` |
| `row-tag` | 行の `license_tag` 列 | `jss_raw_files` / `jss_supply_latest` |
| `uniform` | 表全体で 1 タグ | `swing_*` / `yuho_*` / `ir_disclosures` |
| `operational` | 第三者由来の値を含まない | `jss_job_runs` / `jss_dataset_freshness` |
| `unclassified` | タグ未決（公開しない扱い） | — |

**`core_stocks.sector` と `core_stocks.sector33` を取り違えないこと。** 名前も値も
似ているが writer と一次ソースが違うので**タグが逆**になる:
`sector` = kabulab-cf が JPX `data_j.xlsx` の33業種を書く既存列 → personal-only /
`sector33` = stockStock が EDINET「提出者業種」を書く新設列 → commercial-ok。
2 列を 1 列へ統合してはいけない（タグの違う値が同居すると列単位で区別できない）。
`sector33` は stockStock `master_sync` が東証33業種の名称へ正規化して埋める（2026-09-13〜。`updated_at` は進めない）。

## 銘柄コード契約

| 操作 | 入力 | 出力 | 使う場面 |
|---|---|---|---|
| `normalize_stock_code` | 任意 | 正規化済み文字列（妥当性は見ない） | 表示・比較の前処理 |
| `parse_stock_code` | 任意 | 4 文字の正準形 or `None` | ①由来の値の検証、URL パラメータ |
| `source_code_to_ticker` | TDnet `company_code` / EDINET `secCode` | 4 文字の正準形 or `None` | ③④の取込 |
| `margin_code_to_key` | JPX 信用残 PDF の 5 文字コード | 4 文字の正準形 / 種類株は 5 文字のまま / `None` | ⑧ 信用残 R2 `margin/{date}.json` の `rows[].code` |

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

この系統は正準実装へ寄せていない。**寄せ忘れではなく、寄せられない/寄せると
壊れる理由がある。** 不変条件9 を読んで「もう 1 箇所も無い」と思わないこと。

1. **`worker/src/shared/routes.ts` の `isValidCode`**（公開 API / MCP の入力検証）。
   worker は別言語・別ビルドで Python を import できないため、正規表現リテラルを
   持つしかない。代わりに `worker/test/stock-code-contract.test.ts` が共有テスト
   ベクタの `canonical_regex` と**リテラルを直接突合**して固定する。
   ここは**正規化しない**（URL パスがキャッシュキー・D1 述語になる面なので、
   表記揺れを吸収すると同じ銘柄に複数の URL ができる）。

### 解消済み: JPX 信用残の 5 文字コード（2026-09-13）

kabulab-cf `services/vwap-analysis/lib/margin.ts` の `parseMarginText` は、
5 文字コードを `.slice(0, 4)` で**無条件に切っていた**（取り違えの規則そのもの）。
（stockStock 側の `collectors/jpx_margin.py` は移行中止に伴い削除。判定記録は残す）
「信用銘柄の ETF/REIT は検査文字が `"0"` でない見込みなので、`source_code_to_ticker`
に寄せると正当な行を落とす」として保留していたが、実 PDF で測って決着した。

| 実測（申込み現在） | 2026-08-28 | 2026-09-04 |
|---|---:|---:|
| 明細行（ISIN を持つ行） | 4,229 | 4,227 |
| 検査文字 `"0"` | 4,222 | 4,220 |
| 検査文字 `"5"` / `"6"` | 6 / 1 | 6 / 1 |
| うち ETF・ETN・REIT・インフラ・JDR（`受益証券`/`投資証券`/`ＪＤＲ` 等） | 全件 `"0"` | 全件 `"0"` |
| 旧規則で同じ 4 文字に潰れた組 / 行 | 6 組 / 13 行 | 6 組 / 13 行 |
| `source_code_to_ticker` を当てた場合に落ちる行 | 7 | 7 |

- 懸念は外れていた。**検査文字が非 `"0"` の 7 行はすべて種類株**（伊藤園第１種優先株式
  `25935`、ゼンショー `75505`・日本航空 `92015`・ANA `92025`・インフロニア `50765`・
  ソフトバンク `94345`/`94346` の社債型種類株式）で、**全件が同社普通株と先頭 4 文字を共有**。
- 規則は `margin_code_to_key`（TS `marginCodeToKey`、共有ベクタの `margin_to_key`）:
  末尾 `"0"` は 4 文字、末尾 `"1"`〜`"9"` は **5 文字のまま別の証券**として保持。
  取り違えは 0、落ちる行も 0。`source_code_to_ticker` を使わないのは、
  信用残が「PDF の行を 1 件も捨てずに写す」writer だから（TDnet/EDINET の取込は
  4 文字の母集団へ突合するので、取りこぼし側の `source_code_to_ticker` のまま）。
- 出力互換: 変わるのは毎週この 7 行の `code` の値だけ（キー・行数・行順は不変）。
  `/vwap-analysis/api/margin` は 4 文字コードしか受け付けず、行を先頭一致で返す。
  実 PDF では衝突 6 組すべてで普通株が先に並ぶため、**4 文字コードで引ける値は
  1 つも変わらない**。種類株の行は従来も普通株に隠れて引けず、今後も引けない
  （5 文字の `code` を公開 API で受けるかは別の判断）。
- 既に R2 にある週（旧規則で書かれたもの）は上書きしない。旧規則の週と新規則の週が
  混在するが、上記のとおり `/api/margin` の答えは同じ。
- **答えが変わる読み手が 1 つある**（レビューで判明）: 株ラボ-Youtube
  `kabulab_yt/data/margin.py` → `engines/_m05_support.load_deduped_week`（m05 days-to-cover）。
  同一 `code` の行を「値が一致しない重複」として**組ごと除外**しているため、旧規則の週では
  普通株 `2593`/`5076`/`7550`/`9201`/`9202`/`9434` まで捨てていた（13 行）。新規則の週では
  重複が 0 になり、この 6 銘柄の普通株がユニバースに入り、種類株 7 行は 5 文字なので
  `eligible_codes`（4 文字）に一致せず「ユニバース外」として除外件数に計上される。
  取り違えで捨てていた正当な行が戻る方向の変化で、公開面ではない（手元の分析エンジン）ため
  本変更では株ラボ-Youtube を触らない。同モジュール docstring の「原因は未確認」の
  重複 6 銘柄はこの種類株だった。
- マージ順の罠: `cross-repo-contract` は相手リポの **main** と突合するので、両 PR とも
  マージ前は赤、片方をマージした直後の main も赤になる。両方マージした後に両リポの
  `cross-repo-contract` を再実行して緑を確認する。
- 検証: 共有ベクタの `margin_to_key` は `test_stock_code_contract.py` で常時走る
  （kabulab-cf `margin.test.ts` と両側で固定）。stockStock 側のパーサと
  実 PDF テスト（`TestRealPdfCodes`）は移行中止に伴い削除した。

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
`stock_master` / `financials` / `disclosures` / `raw_files`
（`prices` / `exports` / `job_log` は廃止）

## バンドル別ファイル所有権（並列実装時の衝突防止）

| バンドル | 所有ファイル |
|---|---|
| A: Notion層 | `notion/schema.py`, `notion/upsert.py`, `notion/file_upload.py`, `tests/test_schema.py`, `tests/test_upsert.py`, `tests/test_file_upload.py` |
| B: EDINET | `collectors/edinet.py`, `collectors/edinet_codelist.py`, `convert/xbrl_to_csv.py`, `scripts/capture_edinet.py`, `tests/test_edinet*.py`, `tests/test_xbrl_to_csv.py`, `tests/fixtures/edinet/` |
| C: TDnet | `collectors/tdnet_yanoshin.py`, `collectors/tdnet_official_fallback.py`, `scripts/capture_tdnet.py`, `tests/test_tdnet*.py`, `tests/fixtures/tdnet/` |
| D: 株価系 | 廃止（`collectors/yfinance_prices.py`, `collectors/stooq_prices.py`, `scripts/capture_prices.py`, `tests/test_yfinance*.py`, `tests/test_stooq*.py` ごと） |
| E: 変換(非XBRL) | `convert/json_to_parquet.py`, `convert/pdf_to_text.py`, `convert/xls_to_csv.py`, `scripts/capture_convert_fixtures.py`, `tests/test_convert*.py`, `tests/fixtures/convert/` |
| F: transform | `transform/normalize.py`, `scripts/capture_transform_fixtures.py`, `tests/test_normalize.py`, `tests/fixtures/transform/`（`technicals.py` / `reconcile.py` と対応テストは廃止） |
| G: ジョブ+CI | `jobs/*.py`, `.github/workflows/*.yml`, `tests/test_jobs*.py`（A〜F完了後に実装） |

## 注意: HTTP 200 のエラーレスポンス

`http.fetch()` は HTTP 200 で返る本文の真偽判定をしない（docstring 通り）。
実在の例: EDINET は 200 + `{"StatusCode":401}` JSON（stooq の 200 + ブラウザ検証
HTML は廃止済み経路の記録）。**全コレクターは本文の内容検証必須**（マジックバイト・
JSON 構造・CSV ヘッダ等）。エラー応答を原本として保存してはならない (§3)。

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
フロー = Fetch → save_raw → convert → upload_raw_artifact → transform → upsert → D1 `jss_job_runs` へ実行記録 (§8.1)。
部分失敗は続行して最後に「一部失敗」でログ、原本アップロード失敗はその取得単位の構造化書き込みを中止。
