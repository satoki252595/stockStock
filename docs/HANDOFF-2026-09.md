# 引き継ぎ文書（2026-09）: リファクタリングの背景・決定・修正内容・本番手順

対象: `satoki252595/stockStock`（本リポジトリ）と `satoki252595/kabulab_tool_cloudflare`（以下 kabulab-cf）。
kabulab-cf 側の対応版は同リポジトリの `docs/HANDOFF-2026-09.md`（本文書の抜粋。決定・修正内容・本番手順・不変条件 C/D/G/H のみ）。

---

## 1. この文書の目的と読み方

- 目的: 2026-09-12〜14 に決めたこと・済んだこと・これから直すことを 1 か所にまとめ、**この文書だけを読んだ人が設計・実装・テスト・本番手順を引き継げる**ようにする。
- 現在形で書く。「事実」と「決定」を分け、推測は「未確認」と明記する。数字には出典（実測日・PR 番号・台帳 ID）を付ける。
- **台帳 ID（L-01〜L-68）**: 2026-09-13〜14 に 9 本の調査を統合した「リファクタ台帳」の候補番号。本文書の §5 の表は台帳の全 68 件を同じ ID で並べる。各件の対象ファイル・根拠・依存・行数は同じディレクトリの **`HANDOFF-2026-09-LEDGER.md`**（台帳の公開版。実在コードを伏せたもの）にある。§5 の 1 行要約だけで着手できない件（L-13 / L-32 / L-35 / L-38 / L-41 / L-68 など）はそちらの action 節を読む。
- **不変条件 ID（A1〜J4）**: 台帳の INVARIANTS 節の番号。§4 に ID を保ったまま転記した。PR 本文・レビューで「B2 に触れる」のように参照する。
- **運用者が持つ非公開の手順書**: 秘密・実在銘柄コードと区分の対応・GitHub Support 依頼の対象・ローカルの退避先など、PUBLIC リポジトリに置けない情報は運用者の非公開メモにある。本文書は「その手順が存在する」ことだけを書き、中身は写さない。中身の目録: (1) GitHub Support への依頼文の下書きと対象コミット・検証コマンド、(2) 非普通株 9 銘柄の退避（JSONL・戻し方・Time Travel の bookmark）、(3) 台帳の原本（伏せていないもの）と残タスク精査の項目別結果、(4) 2026-09-13〜14 の決定メモ。Secret の値は非公開手順書には無い（Notion の DB ID は本リポジトリの `db_ids.json`、D1 の database_id は kabulab-cf の `wrangler.toml`、トークンは GitHub Secrets と手元の `.env` だけ）。
- 本文書は PUBLIC リポジトリに置く。**書いてはいけないもの**: 実在の銘柄コードと非普通株区分（ETF/REIT/出資証券/PRO/外国株）の対応、みんかぶ由来の本文、GitHub Support 依頼の対象コミット、トークン・DB ID などの秘密、個人のローカルパス。件数（例: reit_fund 8 / investment_certificate 1）は書いてよい。

---

## 2. 背景

### 2.1 構成（事実）

| 要素 | 内容 |
|---|---|
| stockStock（Python 3.12 / uv / nix） | 収集ジョブ群（GitHub Actions cron）。EDINET・TDnet・日証金・JPX・Yahoo から取得し、Notion と Cloudflare（D1 / R2）へ書く。`worker/` に公開 Worker `jss-api-public` と MCP（`jp_supply_latest` 等 5 ツール）。 |
| kabulab-cf（TypeScript / Node 22 / pnpm 9 / nix） | 単一 Cloudflare Worker に 7 サービス（rsi-screening / otakara-yutai / swing-trading / financial-math / yuho-quant / ir-catalog / vwap-analysis）を Hono サブアプリで同居。取込は GitHub Actions（Node）から D1 REST で書く。Workers Cron は使わない。 |
| Notion | stockStock の `DB_REGISTRY` に 7 DB（①銘柄マスタ ②株価テクニカル ③財務サマリ ④開示書類 ⑤原本ファイル ⑥時系列エクスポート ⑦収集ジョブログ）。kabulab-cf は一次データを Notion「バックアップ」配下へアーカイブ。 |
| D1 | 1 DB `kabulab-cf` に両リポジトリの表が同居（kabulab-cf の `core_*`/`rsi_*`/`yutai_*`/`otakara_*`/`swing_*`/`yuho_*`/`ir_disclosures`/`p_momentum` と stockStock の `jss_*`）。2026-09-13 時点 29 表（台帳 B2、`OBSERVED_TABLE_COUNT=29`）。 |
| R2 | `vwap-data`（日足 `daily/{code}.json`・5 分足 `intra/{code}.json`・信用残 `margin/{date}.json`、外部読者あり）、`jp-stock-raw`（原本 `raw/…`）、`jp-stock-supply`（需給 `supply/{code}.json`、`backup/yutai/`）。 |
| GitHub Actions | stockStock 12 workflow（有効 cron 8 = `TestWorkflowCrons.EXPECTED` の 7 本 + `ops_check`。`margin_weekly` の cron はコメントアウト。台帳 E6）。kabulab-cf 4 workflow（stock-sync / vwap-ingest / catchup / ci）。 |
| ライセンス境界 | 宣言は stockStock（`governance.TABLE_LICENSE` 29 表・`MIXED_LICENSE_COLUMNS` 16 列・`WRITER_CLAIMS` 18 件 → `tests/fixtures/contracts/d1-license-map.json`）、実効防御は kabulab-cf（`public-columns.ts` / `active-equity.ts`）。 |

### 2.2 恒久の制約（決定・変更不可）

1. **ランニングコストを増やさない**（2026-09-13 ユーザー指示）。D1 の rows_read / rows_written、Actions 分数、R2 の増減を**実測**で PR の「コスト影響」節に示す。「たぶん増えない」は通らない。金額が変わらなくても走査行が増える箇所は隠さず報告する。
2. **新 cron / 新 workflow を足さない**（J1）。既存ジョブに相乗りさせる。Workers Paid 機能・Workers Cron は使わない。
3. **両リポジトリは PUBLIC のまま**（J1）。private 化は Actions が有料枠に入るため推奨から外す（2026-09-14 実測: 両リポ合計 6,276 分/月、private なら約 $24〜26/月）。
4. **ライセンス境界**（J2）: JPX・Yahoo・日証金 = personal-only、みんかぶ = no-store（リポジトリ・Notion・Issue/PR・ログに保存せず、公開面に本文を再掲しない）、TDnet = factual-cite、EDINET = commercial-ok。公開面の業種は EDINET 由来の 33 業種。公開面に JPX 由来の値（市場区分・商品区分・sector）は出さない。
5. **PUBLIC リポジトリの掲載規則**: 実在銘柄コードと非普通株区分の対応・出典サイトの掲載文・秘密・ローカルパスを commit / PR 本文 / Issue / コミットメッセージに書かない（GitHub は本文の編集履歴も残す）。テストの合成コードは 1000〜1299（JPX 未割当。2026-09-14 実測: 公開ファイル・D1 とも 1300 未満のコードは 0 件）。
6. **ダミー禁止・フォールバック禁止**（J3、kabulab-cf CLAUDE.md ルール1/2）。

### 2.3 2026-09-12〜14 に済んだこと（事実）

| 日付 | 内容 | 出典 |
|---|---|---|
| 09-12 | 規約上公開できないデータ（kabulab-cf: みんかぶ掲載文 5,694 行 / stockStock: personal-only と factual-cite のフィクスチャ 17 本）を `git filter-repo` で全履歴から除去し force push。再発防止（`.gitignore` と `tests/test_fixture_policy.py`）を入れた。後始末（GitHub は到達不能オブジェクトを自動 GC しない）は §8 T1 | kabulab-cf #10、stockStock #30、運用者メモ |
| 09-12〜13 | 壊れたときに気づける運用層: `ops_check`（鮮度・SLO・列ドリフト・ライセンス地図）を日次化し、失敗は GitHub Issue 1 本、緑に戻れば自動 close | stockStock #33 #35 #43 #48 |
| 09-12 | 銘柄コード正準形を両リポで 1 実装に（種類株の取り違え防止）、共有ベクタ `stock-code-vectors.json` を CI で突合 | stockStock #38 #49、kabulab-cf #20 #28 |
| 09-12〜13 | 公開面から JPX 由来 personal-only を外し EDINET 33 業種へ（`core_stocks.sector33`、業種ランキングのキー移行） | kabulab-cf #22 #24 #25、stockStock #44 |
| 09-13 | Notion ④ の既存重複（1,312 ページ）を 1 ページに集約（全件バックアップ → 計画 → 適用）。検索→作成の競合で重複ができても 1 つに収束する修正 | stockStock #46 #50 |
| 09-13 | ③財務サマリの PK に連結区分を追加し、半期報告書の表記を「2Q」→「中間」（③）/「半期報告」（④）へ。**D1・Notion ③（8,315 件）・④（8,547 件）とも適用済み**（2026-09-13〜14 実行。dry-run で残り 0 件、D1 は `disclosure_type='2Q'` 0 行・「中間」31 行を確認） | stockStock #51 |
| 09-13 | financial-math の旧 2 表（`finmath_*`）を D1 から DROP し地図から外した（バックアップ → マージ → 手動 migration の手順の前例） | kabulab-cf #26、stockStock #47 |
| 09-13 | 非普通株 9 銘柄（reit_fund 8 / investment_certificate 1）を kabulab-cf の D1 から削除（16 表・1,058 行、TDnet 開示 108 件を含む）。jss_*・Notion・R2 の日足は消していない。退避と戻し方は運用者の非公開手順書（Time Travel の保持は 2026-10-13 頃まで） | 2026-09-13 18:33 UTC 実行 |
| 09-13 | 日次取込と公開面の母集団を active かつ普通株（`instrument_type='equity'`）に限定。述語は `src/shared/db/active-equity.ts` の 1 か所 | kabulab-cf #27 #30、stockStock #52 #53 |
| 09-13 | 取込（TDnet / EDINET）の母集団は **案 B** = `equity OR (is_active=0 AND instrument_type IS NULL)`。理由: inactive には地域取引所単独上場の会社が入り（直近 30 日 22 件 / 15 社）、その開示を止めるのは決定の範囲外 | kabulab-cf #31 |
| 09-13 | 優待要約をローカル LLM ではなくクラウド LLM で行う前提に作り替え、ローカル LLM と楽天 API 推定（enrich-from-web）を撤去 | kabulab-cf #29 |
| 09-13 | `prices_daily` の `timeout-minutes` を 300 に戻した（150 では月〜木の実績 207〜253 分が cancel される） | stockStock #54 |
| 09-13 | 両リポの PR・Issue を 0、リモートブランチを `main` だけにした | 21:16 UTC 時点 |
| 09-14 | 先行 4 件（6 PR）を反証レビューの後に squash マージした（01:19〜01:20 UTC）: (D1) `jss_raw_files.doc_id` を旧キー救済で埋める（9/11 分の原本を再実行し、文書単位の行の NULL が 0・R2 は増えていないことを確認）、(D2) 共有テストの実在コードを合成コードへ置換（両リポ同時、静的ガード付き）、(D3) wrangler ログを既定で書かない、(D4) 要約取込 dry-run の掲載文断片漏れ | stockStock #55 #56 #57、kabulab-cf #32 #33 #34 |

### 2.4 コスト基準値（実測。PR の「コスト影響」節はこれと比べる）

| 軸 | 基準値 | 実測日・出典 |
|---|---|---|
| GitHub Actions（過去 30 日、ジョブごと分単位切り上げ） | stockStock 4,787 分（prices_daily 3,346 / ci 543 / tdnet_hourly 515 / export_weekly 114 / edinet_daily 103 / supply_daily 85 / master_sync 47 / その他 34）、kabulab-cf 1,489 分（vwap-ingest 673 / stock-sync 649 / ci 102 / catchup 65）。PUBLIC なので $0。private なら Free 2,000 分/月・$0.006/分 | 2026-09-14、精査 T1 |
| D1 主要表の行数 | `core_stocks` 3,810（active∧equity 3,700 / inactive 110）、`swing_daily_ohlcv` 336,169、`ir_disclosures` 37,641、`core_stock_annual_financials` 15,936、`yutai_benefits` 8,295（要約あり 8,295 / 推定額あり 5,327）、`rsi_percentile` 3,755、`jss_financials` 89、`jss_raw_files` 176 行 / 420 MB、`p_momentum` 0（初回充填は 09-15 の日次） | 2026-09-13〜14、台帳・精査 |
| D1 日次 cron の rows_read | kabulab-cf stock-sync ≈ 1.0M/run（EXPLAIN と既知行数からの推定。TARGET §9.1 の「月 900 万」と食い違い、未確認）、stockStock G-core-5 ≈ 95 万/日（推定）、freshness 58k/日 | 台帳 L-16 / L-17 / L-47 |
| 公開面 1 表示の rows_read | rsi 一覧 16,430、swing dashboard ≈ 17,600、emh 各タブ ≈ 17,000（count + rows の 2 クエリ）、ir home/signals 12,485、yuho 受注 27,393〜4 万、otakara 権利月絞込 ≈ 2.8 万 | 台帳 L-48 / L-50 / L-51 |
| Notion 要求 | prices_daily 1 run: query 118 + PATCH 3,720（36 分）、tdnet_hourly 毎 run ① 39 req（× 11/日）+ ④ 再 PATCH 395、master_sync 月 1 回 PATCH 3,841（43 分） | 台帳 L-18 / L-19 / L-20 |
| R2 | `jss_raw_files` 2 日で 407 MB（prices_daily の非圧縮 CSV 116 MB/日が主因 → 廃止で消える）。無料枠 10 GB-月 / Class A 100 万 / Class B 1,000 万 | 2026-09-11、台帳 L-22 |
| 調査で消費した本番 D1 読取 | 台帳 約 40.5 万行 + 精査 約 8.9 万行、書込 0 | 台帳 SUMMARY、精査各項目の合計 |

---

## 3. 決定事項（日付つき）

各行は「何が変わるか」「何を消し、何を残すか」を 1 行ずつ。

### 3.1 2026-09-13 の決定

| # | 決定 | 変わること | 消す / 残す |
|---|---|---|---|
| D-13-1 | 残作業は「推奨案でよろしく。ランニングコストは増やさないで」で委任 | 台帳の推奨案で進めてよい。ただし法的判断（JPX 規約など）はフラグ 1 つで戻せる形に残す | — |
| D-13-2 | `/emh` 投影 `p_momentum` の走査増（約 34.5 万行/回、金額不変）は案 A（現状維持）で確定 | 再提案しない | 投影は D1 の日足から導出（整合性優先） |
| D-13-3 | 優待要約はクラウド LLM で作る | ローカルモデルは DL しない | kabulab-cf #29 で撤去済み |
| D-13-4 | Notion ④ の重複は今の DB を掃除する（作り直さない） | 先に全件バックアップ | #50 で完了 |
| D-13-5 | 公開面の業種は EDINET の 33 業種で表示 | 東証割当とは約 4% の銘柄で分類が違う | JPX 由来の sector は公開面に出さない |
| D-13-6 | 公開面（スクリーニング・検索・一覧）から ETF・REIT・出資証券を外す | `instrument_type` は条件にだけ使い値は出さない | お宝優待ホームの件数 1,670 → 1,606 |
| D-13-7 | 非普通株 9 銘柄は元データから削除 | 実行済み（§2.3） | jss_*・Notion・R2 日足は残す |
| D-13-8 | 取込母集団は案 B | inactive の地域取引所単独上場の開示は取り込み続ける | 止めたい場合は述語を `activeEquityCondition` に替えるだけで戻せる |
| D-13-9 | 実装は Sonnet に任せる。調査・設計・反証レビューは既定モデル | Claude Code の実装用 workflow スクリプトの設定（implement 段は `model: 'sonnet'`）。人が実装する場合は無視してよい | — |
| D-13-10 | 両リポ全般をリファクタリングする（コスト・性能・古い文書とコメントの全削除・コード圧縮）。**Cloudflare 側の DB 構造を変えてよい** | 本番 D1 の構造変更は バックアップ → マージ → 手動 migration の順 | 契約・ライセンス境界・母集団ガード・鮮度監視を固定するテストは残す |

### 3.2 2026-09-14 09:30 JST の 5 問の回答

| # | 回答 | 変わること | 消すもの | 残すもの |
|---|---|---|---|---|
| D-14-1 | **移行計画 P4b〜P8（stockStock が銘柄マスタ・日足・③断面の writer になる計画）は中止。writer は TS 側に固定** | 設計書の移行節は削除し、CF-CANONICAL-DESIGN を「現在形」だけにする。P4b の「9 銘柄を戻すか」「外国株・PRO Market も公開面から外すか」「詳細ページ 404」「NULL 書換停止」の 4 件の判断は不要 | `universe_guards.py`、`fin_parity.py`、`core_stocks_migrate` の `--verify` 以外、JPX 信用残の Python 実装（`jobs/margin_weekly`・`collectors/jpx_margin`・`cloud_store/margin`・`margin_weekly.yml`）、不変条件 J4 | `contracts/stock_code.py` の `margin_code_to_key`（共有契約）、`--verify`（列ドリフト検査 E5） |
| D-14-2 | **Notion は ②株価テクニカル・⑥週次エクスポート・⑦ジョブログを廃止。⑤ の 20 MB 超は R2 参照** | `DB_REGISTRY` 7 → 4（①③④⑤）。Secret 3 本が不要に | `write_job_log`、`export_weekly`、②の writer と `--enable-history` 履歴子 DB | ①③④⑤、`jss_job_runs`（⑦の実体） |
| D-14-3 | **kabulab-cf の公開面の変更を許容**: CSS 統合・Noto Sans JP 外し・rsi JSON API 2 本の撤去・EDINET 由来のみのページへの 5 分キャッシュ・zod/mini 移行 | 見た目の微差と和文フォントの変更を受け入れる | `/rsi-screening/api/screening`・`/api/stocks/:code`（404 になる） | personal-only 値を含むページには public キャッシュを付けない |
| D-14-4 | **今も使っているので残す**: 端末B の PostgreSQL+FastAPI（`local_store`）、kabuMCP へのキャッシュ引渡し（`--kabumcp-cache-dir`）、`cloud_check` の手動診断、prettier | 台帳 L-01 / L-11 / L-14 と L-39 の prettier 部分は削除しない | — | 上記 4 件（コメント圧縮は可） |
| D-14-5 | **`prices_daily` は廃止**（cron −1。Yahoo の原本を R2 / Notion ⑤ に置く役割ごと） | `EXPECTED` の cron は 7 → 4（`master_sync` / `tdnet_hourly` / `edinet_daily` / `supply_daily`。`reconcile_weekly` `export_weekly` も消える。`ops_check` を含む実 schedule は 8 → 5）。台帳 L-18 / L-24 は不要 | `jobs/prices_daily.py`・`prices_daily.yml`・②の書込・yfinance 経路・Secret `NOTION_DB_PRICES` | **過去の原本（R2 `jp-stock-raw` の yfinance 行、`jss_raw_files`）は消さない**。D1 の日足断面 `core_stock_financials` は kabulab-cf `daily.ts` が書き続ける |

---

## 4. 壊してはいけない不変条件（台帳 INVARIANTS A〜J、ID 維持）

テストが固定しているものは削除せず「規則 + テスト名」に縮める。「決定により撤去」と注記した項目は §3 の決定で消える。

### 4.0 決定・台帳で変わる不変条件の一覧

| ID | 変わること | 起点 |
|---|---|---|
| A2 | `d1-license-map.json` を kabulab-cf にも置き、`pending-peer` を畳む | L-36 |
| B2 | 表数 29 → 30（+`jss_notion_pages`、+`p_yuho_growth`、−`swing_stock_screening`。適用の都度 `OBSERVED_TABLE_COUNT` を動かす） | L-20 / L-51 / L-52 |
| B3 | prices_daily データセットの claim / freshness の writer 表記を実 writer に | L-27、D-14-5 |
| C1 | `SECTOR_DAILY_PUBLIC_KEY_SINCE` を DELETE 後に撤去 | L-64 |
| D1 | stockStock 側の `test_universe_guards` 61 本（2026-09-14 collect 実測。台帳記載は 45）を撤去。kabulab-cf `universe.test.ts` 31 本が唯一の固定に | L-07、D-14-1 |
| E1 | `"COUNT("` 要求を EXISTS 許容に | L-17 |
| E4 | 日次の孤児検査は SOFT + `p_momentum` のみ | L-16 |
| E6 | `EXPECTED` 7 → 4。`margin_weekly` 条項を撤去 | L-02 / L-15 / L-26 / L-33 |
| F4 | `build_column_update` の「呼び出し 0」検査を「シンボル不在」に | L-08 |
| I1 | `DB_REGISTRY` 7 → 4。⑤ の「20 MB 超は R2 参照」は決定済み。実装要否（20 MB 超の発生源が残るか）だけ §8.2 で確認 | D-14-2 |
| J4 | 撤去 | D-14-1 |

### A. 言語横断の契約
- **A1** `tests/fixtures/contracts/stock-code-vectors.json` は両リポで同一バイト列。両 `ci.yml` の `cross-repo-contract` が相手 main を checkout して `diff`。移動・改名も失敗（片方の PR が open の間は赤になる＝意図した結合）。
- **A2** `d1-license-map.json` は現在 stockStock のみ（kabulab-cf 不在、`pending-peer` 猶予中）。生成元は `schema.MIXED_LICENSE_COLUMNS` / `governance.TABLE_LICENSE` / `WRITER_CLAIMS`。`test_governance.TestSharedContract` が等号照合。→ L-36 で kabulab-cf にも同一バイト列で置き、猶予を畳む。
- **A3** 銘柄コード正準形 `^[0-9]{3}[0-9A-Z]$` の実装は `contracts/stock_code.py` と kabulab-cf `stock-code.ts` の 2 箇所のみ。5 文字→4 文字は末尾 0 のときだけ（`margin_code_to_key`、`TestFiveCharCodeToKey`、kabulab-cf `margin.test.ts`）。D-14-1 で信用残 Python 実装を消しても `margin_code_to_key` は残す。
- **A4** kabulab-cf CI: `pnpm db:generate:d1` 後に `drizzle/d1` が clean。`drizzle-kit push` を D1 に使わない。本番に `d1_migrations` 表は無い（適用は手動）。移行 `0010` は本番へ流さない（適用済み扱い）。
- **A5** `test_fixture_policy.ALLOWED` 以外の fixtures 追跡禁止（JPX/JSF/TDnet/Yahoo は gitignore、EDINET は追跡可）。
- **A6** src の DESIGN.md §番号参照 528 箇所は触らず、文書側で ID を維持する（L-31）。

### B. ライセンス境界（宣言 = stockStock）
- **B1** `MIXED_LICENSE_COLUMNS` は allowlist。`sector`（JPX personal-only）と `sector33`（EDINET commercial-ok）は別出所で統合禁止。`id` / `is_active` / `is_yutai` / `created_at` / `updated_at` は意図的に未宣言。
- **B2** `TABLE_LICENSE` は本番の全表を登録（現在 29）。未知の表 = warning、宣言にあって本番に無い表 = failure（Issue）。**表を足す/消すときは `TABLE_LICENSE` / `RETIRED_TABLES` / `CHILD_TABLES` / `datasets.py` / `d1-license-map.json`（両リポ）/ tests `PROD_TABLE_NAMES` / `OBSERVED_TABLE_COUNT` を同じ PR 群で動かし、stockStock 側を先に main へ**（L-20 / L-51 / L-52 が該当）。
- **B3** `WRITER_CLAIMS` 18 件。`column_group` 語彙 all/base/enrich は改名不可。`core_stocks/base = kabulab-cf` は動かさない。`CLAIM_MISMATCH_IS_FAILURE=True`。D-14-5 で `prices_daily` を消したら claim / freshness の writer 表記を実 writer（kabulab-cf `daily.ts`）に揃える（L-27）。
- **B4** `jobs/license_map.py` が `seed_reference_tables` の唯一の入口。孤児の `column_license` は削除、`index_symbols` の孤児は削除しない。`apply_schema` を毎日呼ばない（呼ぶ job / workflow は無く、表を足すときに手で 1 回流す。§6.3）。
- **B5** `LicenseTag` 3 値、`inherit()` は厳しい側。
- **B6** `jss_financials` PK=(code, fiscal_period_end, disclosure_type, consolidated)、`consolidated` NOT NULL（'不明' 番兵）、`FINANCIALS_PK` はリテラル二重持ち（`test_cloud_schema`）。
- **B7** `cloud_store/r2.py` に `delete_object` を実装しない。mutable JSON は `check_no_regression`。

### C. ライセンス境界（実効防御 = kabulab-cf）
- **C1** `public-columns.ts`: `PUBLISH_JPX_DERIVED_COLUMNS=false` は 1 箇所のみ、`publicMarketColumn=NULL`、`publicSectorColumn=sector33`、`sector` へフォールバックしない。`SECTOR_DAILY_PUBLIC_KEY_SINCE` は一時ガードで L-64 の DELETE 後に撤去。
- **C2** `core-stocks-license-boundary.test.ts` の 5 検査はパス・識別子ベース（`PUBLIC_SURFACE` 12 ファイル）。公開面ファイルの移動/改名は `PUBLIC_SURFACE` と `source-scan.ts` を同時更新。
- **C3** `activeEquityCondition()` は WHERE/ON のみ・select しない。`instrument_type` の書き手は `universe.ts` だけ。取込は `disclosureIngestCondition()` (X-05 で改名)。
- **C4** `yutai_benefits.description` は公開面 `app.ts` で `name="description"` と `g.description` 以外に出さない。`src/routes`・`src/views` は存在してはならない。stockStock 側は `RESTRICTED_COLUMNS` と `yutai.EXPORT_COLUMNS`。
- **C5** 業種集計は分母・分子 `activeEquityCondition`、キー `sector33`、カバレッジ <90% で書かない、当日分のみ delete→insert。
- **C6** `notion-archive` は ir-catalog 公開ページの `fetchPageFileUrl` が使う（消すと PDF リンクが死ぬ）。L-44 で消すのは旧フラット DB 退避コードだけ。
- **C7** 公開 GET / 計算 POST は D1 に 1 文も書かない。`/emh?type=momentum` は `p_momentum` だけを読む。`p_momentum` に列を足さない。

### D. 母集団ガード
- **D1** `assertUniverseCoverage` (a)(b)(c)(d1)(d2) と定数 4 つ（kabulab-cf `universe.test.ts` 31 本、2026-09-14 実測）。(c) の分母は equity。**stockStock 側の対向 `test_universe_guards` 61 本は D-14-1 により撤去**（L-07）。定数 4 つを契約 JSON へ移す案は任意。
- **D2** `instrument_type` 語彙 6 値の正本は `instrument-type.ts`、equity は `isListedEquity` と完全一致。
- **D3** `planInstrumentTypeUpdates` は行を増やさない・対象外化行は書かない・全計画後に書く。
- **D4** JPX 取得は `.xlsx`（`.xls` は 404）。月次 rebuild は active∧equity∧is_yutai。

### E. 鮮度監視・運用層
- **E1** `DATASET_SOURCES` キー == `SLO_BY_DATASET` ∪ `NOT_REFRESHED`。1 データセット 1 文、UNION 禁止。`test_ops_slo:267` の `"COUNT("` 要求は L-17 で EXISTS 許容に直す（「0 行は赤」は保つ）。
- **E2** 鮮度はデータ基準日で測る。`updated_at` は実表の epoch であって記録時刻ではない（`now` を入れると永久に緑になる）。`core_stocks` の鮮度 writer は kabulab-cf `universe.ts`、`master_sync` は `updated_at` を進めない。
- **E3** `ops_check.yml`: step id {probe, judge, drift, license}、Issue タイトル完全一致 jq、閉じる条件は 4 outcome AND、cron `30 14 * * *`。**`ops_check.py` は何も書かない**（`jss_job_runs` の prune は runner 側に置く、L-17）。
- **E4** G-core-5 孤児検査は FK の無い `jss_financials`（SOFT）と `p_momentum` で残す（L-16 で FK 宣言のある 15 表は日次から外す）。`c.stock_id IS NOT NULL` の除外は保つ。
- **E5** E7 列ドリフト: `core_stocks` 21 列・索引 3 本を両方向突合。`PROTECTED_COLUMNS` 8 列は stockStock が SET しない。`core_stocks_migrate --verify` は残す（D-14-1）。
- **E6** cron 文字列は `TestWorkflowCrons.EXPECTED`（現在 7 件: master_sync / prices_daily / tdnet_hourly / edinet_daily / reconcile_weekly / export_weekly / supply_daily。`ops_check` は `jobs/` に無いので含まず、実 schedule は +1 の 8）で固定。新 cron を足さない。→ D-14-5 / L-02 / L-15 で 4 件に減る。「`margin_weekly` は切替日まで無効」の条項と `test_margin_weekly_schedule_is_deliberately_disabled` は**決定により撤去**（L-33 で workflow ごと削除）。

### F. D1 書込規律
- **F1** `jss_*` は `CREATE IF NOT EXISTS`、②日足・⑧XBRL ファクト・需給日次の表は持たない。
- **F2** バインド 100/文、compound SELECT 5 項、**rows_read は SELECT 列に依存しない（被覆索引では下がらない）**、JOIN 外側で桁が変わる。新クエリは `meta.rows_read` を実測。
- **F3** 書込順序 R2→D1。`assertDailySchema` で未適用 migration を取得前に検出。
- **F4** `sector33` 充填は差分だけ UPDATE・`updated_at` を SET しない（`test_core_stocks_sector33` 63 本、2026-09-14 実測）。「AST で `build_column_update` 呼び出し 0」の検査は D-14-1 で当該シンボルを消すため**「シンボル不在」に置き換える**。

### G. R2 契約と外部読者
- **G1** `vwap-data`: `daily/{code}.json` / `intra/{code}.json` / `margin/{date}.json`（rows はちょうど 5 キー）/ `weeks.json` は「1 バイトも変えない」。`/api/daily` `/api/intra` は素通し。
- **G2** `margin/` は削除しない、`weeks.json` を空にしない。`r2Delete` は呼び出し元 0（L-42 で撤去）。
- **G3** `daily/` の母集団は `public/vwap-analysis/data/stocks.json`（4,445 件・全種別・2026-06-18 凍結）であり `core_stocks` ではない。**日経225 連動 ETF 1 本の更新継続が外部読者（別リポジトリのブレイク検証）の前提。**
- **G4** 別リポジトリ（動画制作用）が `daily/intra/margin` を R2 直読し、D1 REST で `core_stocks` / `core_stock_financials` / annual / `yuho_*` / `ir_disclosures` / `rsi_percentile` を読む（列追加は耐える、改名・削除は壊れる）。
- **G5** `jp-stock-supply`: `supply/{code}.json`（schema=1、writer 必須、既存点を落とさない）と `backup/yutai/`。公開 Worker は bind しない。`jp-stock-raw`: `raw/{source}/{datatype}/{yyyy}/{date}/{scope}/{doc_id}/{sha16}.{ext}`（PR #56 マージ後、2026-09-11〜切替日の原本は `doc_id` セグメントが `_` のまま。索引の `r2_key` が正）。

### H. 公開 Worker / MCP
- **H1** `jss-api-public` は D1 + `jp-stock-raw` のみ bind、GET/HEAD 以外 405、personal-only は null 化。private は `JSS_API_KEYS` 未設定で 503。
- **H2** MCP `jp_supply_latest` / `jp_supply_series` / `jp_dataset_freshness` / `jp_xbrl_elements` / `jp_raw_file` は稼働中。ツール名・封筒を変えない。
- **H3** kabulab-cf の `/api/ingest/yahoo` は `CRON_SECRET` + ホスト allowlist、Node 同期は `YAHOO_PROXY_BASE` 必須。Yahoo 429 サーキットブレーカ。

### I. Notion
- **I1** `DB_REGISTRY`（D-14-2 で 7 → 4 キー）、dry-run は書かない、⑤原本アップロード必須（**D-14-2 で「20 MB 超は R2 キー参照」に緩める**）、冪等キー。`RECOMMENDED_VIEWS` が参照するプロパティ名（ROE%・開示種別・33業種・開示日時・ライセンスタグ）は改名しない。
- **I2** `upsert.py` の「最古を正・作成直後 1 回だけ再検索・自分のページだけ archive」(#46)・「開示日時が新しい既存行は上書きしない」、④日付窓マップの JST 境界、query 10,000 件打切り検知。
- **I3** Notion ① の 3 プロパティ（履歴 DB ID 等）はコードを消しても本番 DB から消さない（放置で無害）。

### J. 恒久制約
- **J1** 新 cron/workflow 禁止、Workers Paid 機能・Workers Cron 不使用、両リポ PUBLIC のまま、失敗通知は GitHub Issue 1 本。
- **J2** ライセンス（§2.2-4）。
- **J3** ダミー禁止・フォールバック禁止。
- **J4** ~~P4b/P5/P6 の前提（`fin_parity` の閾値、`writeCoreFinancials` フラグ、P6 の処理順、P2 の 2026-09-28 切替）はリファクタで先取りしない~~ → **決定 D-14-1 により撤去**（前提そのものが無くなった）。

---

## 5. 修正する内容（台帳の全 68 件）

凡例 — 状態: **実施** / **見送り** / **決定で不要**。「本番」= 本番で人が行う手順の要否（§7 の番号）。効果は台帳の推定値（実測日 2026-09-11〜14）。決定で内容が変わった項目は変更後で書く。

### 5.0 集計と効果の合計

- 件数: 68（stockStock 35 / kabulab-cf 30 / 両 3。台帳 TOTALS の「37 / 27 / 4」は誤記）。状態: **実施 59 / 見送り 7（L-01 L-11 L-14 L-23 L-54 L-55 L-58）/ 決定で不要 2（L-18 L-24）**。
- 本番手順を要するもの（§7）: L-03（任意）L-05 L-15 L-20 L-25 L-26 L-36 L-44 L-45 L-46 L-50 L-51 L-52 L-53 L-64、台帳外 X-01。L-09 の本番適用は完了済み。P4（0018 適用）は 2026-09-14 完了、L-36 の猶予畳み込み完了（両 main の地図 sha e175d94…一致、P6 手順で kabulab-cf #62 → stockStock #72）。
- 行数（台帳 TOTALS の見積もりを決定で補正）: stockStock ≈ −16,000（台帳 −18,500 から L-01 / L-11 / L-14 の約 −3,300 を戻し、L-33 の削除側 −800 を足す。`prices_daily` 廃止の追加削減は未算定）、kabulab-cf ≈ −37,000（うち drizzle 旧 snapshot 26,822。prettier を残す分は行数に影響なし）。
- 効果（台帳 TOTALS + 決定分。$ は不変。Actions は PUBLIC で無料、D1 / R2 は含み枠内）:
  - D1 rows_read: 月 −約 4,000 万超（kabulab-cf 日次 −2,000 万、stockStock G-core-5 −2,000 万、freshness −140 万）+ 公開面 1 表示あたり −1.5〜8 万。
  - D1 rows_written: 月 −約 35 万。
  - Notion 要求: −5,000〜7,000 req/日（台帳）に、②⑥⑦廃止と `prices_daily` 廃止の −3,800 req/日 が加わる。
  - R2 増分: 3.6 → 約 0.7 GB/月（台帳）。`prices_daily` 廃止で日次 144 MB の書込がゼロになるため更に減る（`export_weekly` 廃止で −1.4 GB/月）。
  - Actions: 台帳の −1,500〜2,500 分/月から `prices_daily` 高速化分（L-18、−900〜1,700）を除いた −600〜800 分/月 + `prices_daily` 廃止 −3,346 分/月（2026-09-14 実測の過去 30 日）。
  - cron −3（`reconcile_weekly` / `export_weekly` / `prices_daily`）、手動 workflow −1（`yutai_backup`）、依存 stockStock −2（xlrd / openpyxl）・kabulab-cf −3、Secret −3（`NOTION_DB_PRICES` / `NOTION_DB_EXPORTS` / `NOTION_DB_JOB_LOG`。`KABULAB_D1_DATABASE_ID` は現在存在せず、P1 で一時的に足して L-04 で消す）。

### 5.1 stockStock: 死んだコード・一度きりツール・依存

| ID | 題名 | 何をするか | 効果 | 本番 | 状態 |
|---|---|---|---|---|---|
| L-01 | `local_store`（PostgreSQL dual-write + FastAPI）と `deploy/` | **削除しない**（D-14-4）。docstring / README の経緯だけ圧縮 | — | — | 見送り |
| L-02 | stooq コレクター・`reconcile_weekly`（週次 cron）・`transform/reconcile.py` | 削除。`models.Source.STOOQ` と `licensing.py` の値は残す（2026-06 期の Notion 行が持ちうる）。`EXPECTED` から外す | cron −1、Actions −9 分/月 | — | 実施 |
| L-03 | Notion ①配下の株価履歴子 DB（`price_history` / `--enable-history`） | `prices_daily` 廃止（D-14-5）と同じ PR で `notion/price_history.py`・schema の `HISTORY_*` を削除。本番 ① のプロパティは消さない（I3） | Notion −39 req/日 | P3（任意） | 実施 |
| L-04 | `KABULAB_D1_DATABASE_ID`（2 DB 前提のフォールバック） | config / datasets / 3 job / 3 workflow から削除。**L-05 の退避を先に済ませてから** | — | — | 実施 |
| L-05 | `yutai_backup`（実行履歴 0・現状 no-op） | **一度流して ⑨ の LLM 派生値（要約・推定額）を R2 `backup/yutai/` に退避してから**、job・`cloud_store/yutai.py`・workflow・テストを削除。「新 cron 禁止」は `EXPECTED` が引き続き固定 | −590 行 | P1 | 実施 |
| L-06 | `cloud_store/fin_parity.py`（P5 判定 G-fin-1） | 削除（D-14-1）。`CONTRACTS.md` G-fin-1 節も削除 | −739 行 | — | 実施 |
| L-07 | `cloud_store/universe_guards.py`（①母集団 writer の Python 移植・呼び出し 0） | 削除（D-14-1）。kabulab-cf `instrument-type.ts` のコメント参照を「共有文字列 'equity'」に書換 | −830 行 | — | 実施 |
| L-08 | `core_stocks_migrate` の `--verify` 以外 | `--apply` / `--sql-dump` / `--snapshot` / `--state-dump` / `--compare-to` と `plan_ddl` / `normalize_sql` / `build_column_update` を削除（D-14-1）。`unexpected_columns` / `unexpected_indexes`（E5）は残す。テストは「適用済みなら何も計画しない」1 件に | −400 行 | — | 実施 |
| L-09 | `scripts/migrate_halfyear_labels.py` | 本番適用は完了（Notion ③④・D1、2026-09-13〜14。dry-run で残り 0 件）。スクリプトとテストを削除してよい。`local_store`（端末B）にも同じ移行が要るかは未確認（`--target local`。要るなら削除前に流す） | −997 行 | — | 実施 |
| L-10 | `scripts/dedupe_notion_disclosures.py`（④重複掃除の一度きり） | §8.3 の月曜チェック F（新規重複 0）を確認してから削除 | −1,384 行 | — | 実施 |
| L-11 | `edinet_daily` の kabuMCP キャッシュ引渡し | **削除しない**（D-14-4） | — | — | 見送り |
| L-12 | `convert/xls_to_csv.py` と xlrd / openpyxl | 削除して `uv.lock` 再生成 | −220 行、依存 −2 | — | 実施 |
| L-13 | 参照ゼロの小シンボル 13 個と `RETIRED_TABLES`（finmath 遷移） | 削除。**L-01 を残すため `local_store` が使うシンボルは対象から外して再確認** | −400 行 | — | 実施 |
| L-14 | `cloud_check`（手動疎通診断） | **削除しない**（D-14-4）。README に「資格情報ローテーション時の診断」と 1 行明記 | — | — | 見送り |
| L-15 | `export_weekly`（③④は 10,000 件打切りで毎週失敗、⑥は配布不能） | 削除（D-14-2）。`EXPECTED` から外す | cron −1、R2 −1.4 GB/月、Notion −1,400 req/月 | P3 | 実施 |

### 5.2 stockStock: コスト・性能・workflow

| ID | 題名 | 何をするか | 効果 | 本番 | 状態 |
|---|---|---|---|---|---|
| L-16 | G-core-5 孤児検査を FK 宣言のある 15 表で毎日回さない | 日次 `--verify` は SOFT（`jss_financials`）と `p_momentum` だけ。`PRAGMA foreign_keys` が 0 なら全表検査に戻す安全弁 | D1 rows_read −約 95 万/日（月 −2,000 万） | — | 実施 |
| L-17 | freshness の COUNT(*) 全走査を EXISTS に、`jss_job_runs` の窓を 30 日、`license_map` は差分時のみ書く | prune の DELETE は runner の `record_job_run` 側（E3） | rows_read −46k/日 | — | 実施 |
| L-18 | `prices_daily` の valuation 並走・②PATCH 並列化 | — | — | — | 決定で不要（D-14-5） |
| L-19 | `master_sync` が ① 全 3,841 行を毎月無条件 PATCH | 事前マップと比較して同値なら PATCH を省く | 47 → 約 4 分/月、Notion −3,800 req/月 | — | 実施 |
| L-20 | `tdnet_hourly` の ④ 毎時再 PATCH を同値 skip、① の code→page_id を D1 新表 `jss_notion_pages` に | 新表は `master_sync` が書き、tdnet/edinet は 1 SELECT。表数 +1 は B2 の手順で | Notion −2,000〜3,000 req/日 | P4 | 実施 |
| L-21 | ⑤ の sha256 重複検索を対象日 1 回のマップに | `find_raw_page_by_sha256` を日付スコープのマップに置換 | Notion −100〜200 req/日 | — | 実施 |
| L-22 | ⑤ の 20 MB 超を添付せず R2 キー（url プロパティ）を書く | **⑤ 部分のみ**（D-14-2）。gzip 化は `prices_daily` 廃止で対象が消えるため行わない。20 MB 超の原本が今後も出るかを §8.2 で確認してから（出なければ規則を足さない） | Notion 多パート UL 減 | — | 実施（縮小） |
| L-23 | `supply_daily` の `supply/{code}.json` 全置換 RMW を日次シャードに | G5 の schema=1 契約と MCP `jp_supply_series` の読み替えが要る（H2 の封筒は変えない） | R2 Class A/B 各 −91k/月 | P（未定） | 見送り（判断待ち） |
| L-24 | yfinance 404 固定 101 銘柄を母集団から外す | — | — | — | 決定で不要（D-14-5） |
| L-25 | Notion ⑦ 収集ジョブログ廃止 | `write_job_log`・`DB_REGISTRY.job_log`・`JOB_PROP_*`・Secret `NOTION_DB_JOB_LOG` を削除（D-14-2） | Notion −25 req/日 | P3 | 実施 |
| L-26 | Notion ② 廃止 → **`prices_daily` 廃止**に統合 | job・workflow・②の upsert・yfinance コレクター・Secret `NOTION_DB_PRICES` を削除（D-14-5）。`EXPECTED` から外す。**過去の原本は消さない**。D1 断面の writer は kabulab-cf `daily.ts` のまま | cron −1、Actions −約 3,300 分/月（2026-09 実測 3,346 分）、Notion −3,800 req/日 | P3 | 実施 |
| L-27 | `datasets.py` の prices_daily データセットの writer 名 | `writer="prices_daily"` を実 writer `kabulab-cf daily.ts` に、名前を実表に合わせる。`test_governance` の鮮度 writer 突合に追加（B3） | — | — | 実施 |
| L-28 | `ci.yml` を `on.push=main` 限定、pytest `-rs`、EDINET 由来フィクスチャ 5 件を追跡に戻す | 残り skip は `test_fixture_policy` で一覧固定。technicals の合成 OHLCV は `prices_daily` 廃止で対象が減るため実装時に再判断 | Actions −120 分/月 | — | 実施 |

### 5.3 stockStock: 文書・コメント・テスト

| ID | 題名 | 何をするか | 効果 | 本番 | 状態 |
|---|---|---|---|---|---|
| L-29 | `CLOUDFLARE-CONSOLIDATION.md` 削除、DESIGN / CONTRACTS / README / AGENTS を現在形に圧縮 | 実銘柄コード列挙は件数へ。`AGENTS.md` の `@RTK.md` 参照削除。「README に更新記録」ルールは git log / PR 本文に一本化 | −620 行 | — | 実施 |
| L-30 | `CF-CANONICAL-DESIGN.md`（3,661 行）と `TARGET-ARCHITECTURE.md`（717 行）を現在形の契約・配置表・未決事項だけに | 移行フェーズ P4b〜P8 の節は削除（D-14-1）。「残る判断事項」は §8 と同期 | −3,500 行 | — | 実施 |
| L-31 | docs を `SPEC.md`（現在形）/ `TARGET-ARCHITECTURE.md`（目標像）/ `DECISIONS.md`（決定と未決）の 3 本に | § ID を SPEC 見出しに維持（A6）。src の docs ファイル名参照 25 箇所だけ置換 | docs 5,081 → 約 830 行 | — | 実施 |
| L-32 | src の経緯コメントを規則 + テスト名に圧縮 | 上位 15 モジュールで約 −1,500 行。`slo.py` の祝日は「緑を 1 営業日緩める」案で未決コメントを消す | −2,100 行 | — | 実施 |
| L-33 | JPX 信用残 writer の Python 実装 | **削除側**（D-14-1）: `jobs/margin_weekly.py`・`collectors/jpx_margin.py`・`cloud_store/margin.py`・`margin_weekly.yml`・テスト 37 件。`margin_code_to_key` は残す（A3） | −800 行 | — | 実施 |
| L-34 | D1 ダブル 8 個・R2 ダブル 3 個を `tests/_doubles.py` に統合 | L-05 / L-08 / L-10 の削除後に差し替え | −280 行 | — | 実施 |
| L-35 | 文言固定→属性照合、重複リテラル固定の削除、`test_jobs_smoke` の主題分割 | L-02 / L-32 の後 | −80 行 | — | 実施 |

### 5.4 両リポジトリ

| ID | 題名 | 何をするか | 効果 | 本番 | 状態 |
|---|---|---|---|---|---|
| L-36 | `RESTRICTED_COLUMNS` を地図と突合、地図を kabulab-cf にも同一バイト列で置き `pending-peer` を畳む | worker/test に等号テスト、kabulab-cf `public-columns.test.ts` で突合、両 `ci.yml` を 2 ファイル突合に。**両 main 同時マージ**（§7 P6） | — | P6 | 実施 |
| L-37 | 「[ジョブ失敗] Issue を 1 本」シェルを composite action に、`license_tag` を `TABLE_LICENSE` から導出 | 4 箇所の部分一致検索を完全一致 jq に | −20 行 | — | 実施 |
| L-54 | `otakara_stock_financials` 廃止 | 読取が 1 表示 +1,600 行増える（制約 §2.2-1 に触れる） | — | — | 見送り |

### 5.5 kabulab-cf: 死んだコード・依存

| ID | 題名 | 何をするか | 効果 | 本番 | 状態 |
|---|---|---|---|---|---|
| L-38 | 到達しない一度きりスクリプト 7 本と `yuho:investigate` | 削除（`export-benefit-descriptions.ts` / `fetch-yutai-full.ts` は `all-monthly.ts` が spawn するので残す） | −1,100 行 | — | 実施 |
| L-39 | Neon(pg) 期の drizzle config 4 本・移行 SQL 15 本・scripts 12 本・`DATABASE_URL`・未使用依存 | `@neondatabase/serverless` / `vercel` / `@hono/node-server` を外す。**prettier は残す**（D-14-4）。README「残るは Neon 解約」は解約済みか未確認 → §8.2 | −1,330 行、node_modules −10 MB | — | 実施 |
| L-40 | `drizzle/d1/meta` の旧スナップショット 0000〜0011（26,822 行） | `git rm`。`_journal.json` と最新 snapshot は残す（実測: 消しても `generate` は No schema changes） | −26,822 行 | — | 実施 |
| L-41 | otakara の未マウント middleware・旧 v1 スクレイパ一式・`core-repo.ts` | 削除。`core-repo.ts` は共通化 P1 を続けないなら削除（推奨: 削除、参照 0） | −1,900 行 | — | 実施 |
| L-42 | VPN ローテーション・動作不能な `build_stocks.py`・`openvpn`・`sw.js`・`r2Delete` | 削除。**`stocks.json` 自体は触らない**（G3、§8.2 U3） | −300 行 | — | 実施 |
| L-43 | rsi-screening の JSON API 2 本 | 撤去（D-14-3）。公開 URL は 404 になる | −55 行 | — | 実施 |
| L-44 | 一過性の互換シム 3 つ（yuho `metric=backlog`・otakara `?sort=&order=`・Notion 旧フラット DB 退避） | 削除。Notion 側は「バックアップ」配下に旧フラット DB が無いことを目視確認してから | −70 行 | P8 | 実施 |

### 5.6 kabulab-cf: D1 構造・コスト・性能

| ID | 題名 | 何をするか | 効果 | 本番 | 状態 |
|---|---|---|---|---|---|
| L-45 | 冗長索引 4 本（同一列に UNIQUE + 通常索引）を DROP | 宣言を外す → `db:generate:d1` → 生成 SQL が `DROP INDEX` ×4 だけであることを読む → 手動適用 | 日次索引書込 −3,700 | P4 | 実施 |
| L-46 | 読み手の無い索引（swing 2 本・ir 2 本・`idx_rsi_percentile_min`）を DROP | L-48 で rsi JOIN 順を直すなら `idx_rsi_percentile_min` は残す。`ir_disclosures_pubdate_idx` の将来用途は未確認 | 日次索引書込 −7,400 | P4 | 実施 |
| L-47 | 日次 sync の `swing_daily_ohlcv` フルスキャン 3 本を消す | Phase 6 投影は Phase 3 のメモリから、Phase 1 は `latest_date` の LEFT JOIN、Phase 4 sweep は月曜のみ（同 run 内分岐、新 cron なし）。`/emh` の数値は不変 | rows_read 月 −約 2,000 万 | — | 実施 |
| L-48 | rsi / swing / emh の JOIN 順を派生表外側に固定 | EXPLAIN で外側が派生表であることを確認 | rsi 16,430 → 約 1,500/表示ほか | — | 実施 |
| L-49 | annual の毎日 15,900 行 upsert を週 1、TDnet 7 日窓を差分更新に | DDL なし | rows_written 月 −30 万 | — | 実施 |
| L-50 | ir-catalog の「高シグナル最新 25 件」を部分索引で | `CREATE INDEX … WHERE primary_tag IN (7 タグ)` | 12,485 → 約 50/表示 | P4 | 実施 |
| L-51 | yuho-quant の投影表 `p_yuho_growth`、otakara の権利月・ジャンル集計列 | EDINET catchup の末尾で再生成（新 cron なし）。表数 +1 は B2 の手順。地域バケット比率 4 列は投影に持つ（2026-09-14 決定。実装時に rows_read を実測して PR に載せる） | yuho −5〜8 万/表示 | P4 | 実施 |
| L-52 | `swing_stock_screening` を indicators の列に畳み、signals の無条件 DELETE を sweep 1 文に | 表 DROP は B2 の手順（stockStock 先行）。取得失敗銘柄の前日シグナルは**消す**（鮮度のない値を出さない。J3。2026-09-14 決定） | rows_written −3,755/run | P4 | 実施 |
| L-53 | サロゲート id の撤去・`swing_sector_daily` 保持 30 日・`pct_5d` DROP・`operating_margin_ttm` 二重持ち解消 | 表の作り直し SQL を手で読む。L-45 の後 | 索引 −4 本 | P4 | 実施 |
| L-55 | `swing_daily_ohlcv` 廃止（R2 日足を Worker から読む） | L-47 で十分効く。capm/bs/low-vol の日足が最大 2 営業日古くなる | — | — | 見送り |
| L-56 | D1 REST 書込の往復数削減（案 A: multi-row upsert） | 案 A（DDL なし）。D1 REST `/query` の複数文+params 可否を先に確認 | 日次 −12〜18 分 | — | 実施 |
| L-57 | stock-sync の失敗率 52% を下げる | 回収上限 100 を時間予算制に、失敗率 ≤1% は成功扱い + Issue コメント。vwap の Yahoo 404 は即 throw | Issue 誤報減 | — | 実施 |
| L-58 | VWAP 取込の母集団変更・日足二重取得の統合・5 分足シャード化 | 外部読者（G3 / G4）と衝突するため今回は不実施。`stocks.json` の判断（§8.2 U3）と一緒に別途設計 | — | — | 見送り |

### 5.7 kabulab-cf: 公開面・CI・文書・コメント

| ID | 題名 | 何をするか | 効果 | 本番 | 状態 |
|---|---|---|---|---|---|
| L-59 | zod classic → zod/mini | 20 ファイルの validators を書換（D-14-3）。`@hono/zod-validator` が mini を受けるか 1 ルートで先に確認 | バンドル −約 480 KB raw | — | 実施 |
| L-60 | Yahoo 取込プロキシ 2 系統と Yahoo クライアント 2 実装を 1 系統に、error-handler を共通化、re-export シム撤去 | `YAHOO_PROXY_BASE` は同じ値でパスだけ切替 | −260 行 | — | 実施 |
| L-61 | 7 ページ重複の layout CSS を `design.ts` に統合、Noto Sans JP を外す | D-14-3 | 外部 CSS −459 KB/ページ | — | 実施 |
| L-62 | SSR 一覧に `Cache-Control`、`public/_headers`、`lightweight-charts` を同梱 | **EDINET 由来のみのページに限定して 5 分**（D-14-3） | 再訪の Worker 呼び出し減 | — | 実施 |
| L-63 | vitest 2 回実行を 1 回に、テストの migration 適用を番号付き SQL に限定、lint 98 warnings をゼロに | `test:coverage` を残すかは未確認 | CI −18 秒/run | — | 実施 |
| L-64 | `SECTOR_DAILY_PUBLIC_KEY_SINCE` と日付ガードを、`swing_sector_daily` の 2026-09-14 未満の行を DELETE した上で撤去 | 月曜の日次 cron 実行後に DELETE（§7 P5） | −140 行 | P5 | 実施 |
| L-65 | ADR-0001 を 5 行要約に、README / overview / portal / deploy / drizzle README の経緯・誤記を直す | Workers プラン（無料 vs Paid）の記述は未確認 → §8.2 | −900 行 | — | 実施 |
| L-66 | docs/00N の関数名誤り・PR 記録・未追随機能を直し、`ci-typecheck-blind-spots.md` を 30 行に | rsi `docs/` と `final-quality-report.md` を削除 | −1,000 行 | — | 実施 |
| L-67 | サービス別 CLAUDE.md / README / docs/00N の三重化を 1 本に、CLAUDE.md ルール4（不在の `.claude/agents`）削除・ルール5 を「PR を作る」に | `AGENTS.md` の `.claude/agents` 参照も書換 | −1,100〜1,500 行 | — | 実施 |
| L-68 | コード内の経緯コメント約 1,000 行を現在形に圧縮、実装と食い違うコメント 6 箇所と死 URL の User-Agent を訂正 | `active-equity.ts` の「承認待ち」2 点は D-13-6 で回答済み（述語に使う・公開面に出さない）として記述を消す | −1,100 行 | — | 実施 |

### 5.8 台帳外の追加候補（残タスク精査で「リファクタで扱う」とされたもの）

台帳 ID は無い。実装計画に入れるときは L-69 以降を採番する。

| 仮 ID | リポ | 内容 | 出典 |
|---|---|---|---|
| X-01 | kabulab-cf | `yutai_benefits.estimate_value_source` / `estimate_source_url`（本番 0 行）と `app.ts` の「WEB推定」UI、import 側の同フィールドを削除。DROP COLUMN は §7 の手順（enrich-from-web を作り直さない前提、§8.2 U5） | 精査 (2) |
| X-02 | stockStock | `first_fetched_at` の上書き（`d1.py` の upsert が conflict 以外の全列を更新）。**完了**: #56 で `D1Store.upsert(keep=[...])` を足し `first_fetched_at` を保つようにした | 精査 D1、#56 |
| X-03 | stockStock | `schema.py` 「R2 カスタムメタデータ」のコメントと `r2.py put_bytes`（Metadata 無し）のドリフト、`sink.py` / `governance.py` / TARGET の実測値ハードコード（176 行・420 MB は 2026-09-14 実測） | 精査 D1 |
| X-04 | stockStock | `cloud_store/yutai.py` の `BASELINE_*` 定数を索引の最新エントリから取る（L-05 で job ごと消すなら不要） | 精査 (3) |
| X-05 | kabulab-cf | `ingestUniverseCondition` の命名と 100 行超 docstring（L-68 に相乗り） | 精査 P4b |
| X-06 | 両 | 地図 `core_stocks.name = commercial-ok「EDINET 由来」` と実 writer（`universe.ts` が JPX 一覧から書く）の不一致。§8.2 U3-(a) の結論に合わせて地図か writer を直す | 精査 stocks.json |
| X-07 | kabulab-cf | ライセンス系テストが `public/` を走査しない穴（`public/` に D1 由来の生成物以外を置かない規則のテスト） | 精査 stocks.json |
| X-08 | 両 | テストに残る JPX 一覧の実在行と種類株の実在コード約 40 行を「契約の根拠として残す許容リスト」に一本化（PR #57 / #34 の静的ガードの allowlist が入口） | 精査 D2 |
| X-09 | stockStock | TARGET の Actions 試算単価を $0.008 → $0.006（2026-09-14 取得）に | 精査 T1 |
| X-10 | kabulab-cf | `swing_sector_daily` 2026-09-11 に「機械」（209 銘柄）の行が無い件（不具合確認、リファクタではない）。09-14 分で再発すれば `aggregateSectorDaily` の書込経路を調べる | 精査 (4) |

---

## 6. 実施の順序と作法

### 6.1 ウェーブ制
- 同じリポジトリで**触るファイルを重ねない**単位を 1 ウェーブとし、ウェーブ内の PR は並列に作ってよい。ウェーブをまたぐ依存（台帳の `depends_on`）は前のウェーブのマージを待つ。
- 順序の骨格（依存の要点）:
  1. 先行 PR 6 本（stockStock #55 #56 #57、kabulab-cf #32 #33 #34）はマージ済み（2026-09-14）。§7 P0 の後片付けも (b) の wrangler ログ削除（T3）を除き完了。
  2. 本番の一度きり作業を先に終える: `yutai_backup` の退避（P1）→ 優待要約の取込（§8.2 U1）。**要約取込は内容キー（掲載文）に依存するので、`yutai_benefits` の列・内容を変える変更（X-01、手動の `pnpm sync:monthly`）より前に済ませる。** L-45〜L-53 の D1 DDL は U1 の判断を待たずに進めてよい。`migrate_halfyear_labels --apply` は完了済み。
  3. **stockStock の地図（`d1-license-map.json` と `TABLE_LICENSE` / `RETIRED_TABLES` / `CHILD_TABLES`）を先にマージ**してから kabulab-cf の表の追加・削除（L-20 / L-51 / L-52 / X-01）を入れる（B2）。L-36 はこの前提を作るので早いウェーブに置く。
  4. 削除系（L-02〜L-15、L-33、L-38〜L-44）→ 性能系（L-16〜L-21、L-47〜L-57）→ 公開面（L-59〜L-62）→ 文書（L-29〜L-31、L-65〜L-67）→ コメント圧縮（L-32、L-68）。文書とコメントは対象コードの存廃が決まってから。
  5. L-64 の DELETE は月曜の日次 cron 後（P5）。L-53 は L-45 の後。L-46 は L-48 の後。
- 台帳の `depends_on` を決定で補正した依存関係（後続 ← 先行）。ウェーブ割りはこの表を守る:

| 後続 | 先行 | 理由 |
|---|---|---|
| L-04 | P1（`yutai_backup` の退避） | `yutai_backup.py` が同フィールドで no-op する |
| L-05（削除） | P1、K1（要約取込後の再退避） | 退避してから消す |
| L-08 | L-04 | 同じ config / job を触る |
| L-09（削除） | —（本番適用は完了済み） | `local_store` に同じ移行が要るかだけ先に確かめる |
| L-10（削除） | §8.3 チェック F | 新規重複 0 の確認 |
| L-13 | L-02 | 削除対象が減る（L-01 は残すので `local_store` 依存分は除く） |
| L-20 | L-04、L-36 | 表数 +1 を 4 箇所同時更新（B2） |
| L-22 | L-15、U-⑤ の判断 | 20 MB 超の発生源が消える |
| L-26（`prices_daily` 廃止） | L-15、L-25 | Notion 側の撤去順 |
| L-30 | L-29 | 文書の骨格 |
| L-31 | L-29、L-30 | 3 本構成 |
| L-32 | L-06、L-07、L-33 | 削除するモジュールのコメントは触らない |
| L-34 | L-05、L-08、L-10 | 差し替え対象が減る |
| L-35 | L-02、L-32 | 文言変更と同時 |
| L-39 | L-38 | Neon 移送ツールが唯一の利用者 |
| L-46 | L-48、L-52 | 残す索引が決まる |
| L-51 / L-52 / X-01 | L-36、stockStock の地図 PR | B2（stockStock 先行） |
| L-53 | L-45 | 索引整理の後 |
| L-56 | L-52 | 書込文の形が決まる |
| L-60 | L-41 | 旧スクレイパ削除の後 |
| L-64（DELETE） | P5（09-14 分の cron 実行） | 当日行を書いた後 |
| L-65 | L-39 | Neon 節の扱い |
| L-67 | L-65 | README 統合 |
| L-68 | L-64 | `public-columns.ts` を二度触らない |
| K1（要約取込） | P1 | 退避 → 取込 → 再退避 |
| X-01（列 DROP） | K1 | 内容キーが変わる前に取込を終える |
| 見送り: L-55 ← L-47、L-58 ← L-42 + U3 | — | 再開するときの前提 |

- 具体的なウェーブ割りは §6.6。

### 6.2 PR 単位
- 1 PR = 1 台帳 ID が原則。同じファイルを触る小さな ID は 1 PR にまとめてよい（例: L-45 + L-46）。
- PR 本文の必須節: **「コスト影響」**（D1 rows_read / rows_written、Actions 分数、R2 個数・バイト、Notion 要求数。実測値と測り方。増える箇所は隠さない）、**「不変条件」**（触れる ID と、それを固定するテスト名）、**「本番手順」**（§7 の番号、または「不要」）。
- PR 本文・コミット・Issue に実在銘柄コードと区分の対応・掲載文・秘密・ローカルパスを書かない（§2.2-5）。
- マージは squash。**マージはユーザーの合図で行う**。ブランチ保護は無い（2026-09-14 実測）ので CI が赤でもマージできてしまう。赤のままマージしない。

### 6.3 D1 の DDL / DELETE
1. 対象（表・索引・行数）をユーザーに示す。
2. バックアップ（Time Travel の bookmark を控える。表単位なら `wrangler d1 export` か SELECT の JSONL）。
3. PR をマージ（コード側は未適用 migration を `assertDailySchema` で検出する、F3）。**マージから 21:00 UTC の日次 cron（kabulab-cf stock-sync）までに手順 4 を終える**。間に合わず cron が失敗したら、適用後に `gh workflow run stock-sync.yml -f target=daily` で流し直す。
4. 手動 migration: `pnpm db:generate:d1` の生成 SQL を**読んでから** `wrangler d1 execute kabulab-cf --remote --file=…`（Cloudflare 認証は §9.3 の前提）。stockStock の `jss_*` 表（例: L-20 `jss_notion_pages`）は `cloud_store.schema.apply_schema` を手元で 1 回流す。**これを呼ぶ job / workflow は無い**ので、L-20 の PR にその 1 行コマンド（`.env` の `CF_*` を読んで `D1Store` を作り `apply_schema` を呼ぶ）を書き、`license_map` の「欠けている表」報告が消えることで確かめる。
5. 検証: `sqlite_master` で表・索引を確認、EXPLAIN で計画が変わっていないこと、`ops_check` が緑（B2 の表数照合を含む。`gh workflow run ops_check.yml` で即時に回せる）。
6. 戻し方: Time Travel で bookmark に戻す（DELETE / DROP の場合）。**Time Travel は DB 全体を戻す**ので、戻した後は失われた時間帯の cron（stock-sync / catchup / stockStock の各ジョブ）を dispatch で書き直す。表単位で戻すなら手順 2 の export / JSONL から復元する。

### 6.4 反証レビュー
- マージ前に、実装者と別の担当が **PR の主張を反証する**: コード・`gh`・本番 D1 の SELECT（読み取りのみ）で「効果の数字」「触れない不変条件」「本番手順の要否」を確かめ、外れていれば PR を差し戻す。2026-09-13〜14 の精査でも、メモ・報告の食い違い 10 件以上を実測で決着させた（例: 走査削減の方法、P4b の writer、timeout の危険度）。
- レビューで「新しい値は誤り」と言われても出典（API・実表）で確かめてから判断する（EDINET の書類名が後から改まる例あり）。

### 6.5 コミット規約
- 題名は `type(scope): 日本語の要約`（type は feat / fix / perf / refactor / chore / docs / test。直近 PR の題名に倣う）。
- 本文に目的・変更内容・検証結果を書く（README への更新記録は L-29 で廃止し、git log / PR 本文に一本化）。
- 末尾に `Co-Authored-By` トレーラー（Claude Code / Codex いずれも既定のもの）。
- テストの合成コードは 1000〜1299。1300 以上の 4 桁コードは allowlist 以外を静的ガードが拒む（PR #57 / #34）。

### 6.6 実装計画（ウェーブ割り）

台帳の `depends_on`・§6.1 の依存表・各件の `files_touched`（`HANDOFF-2026-09-LEDGER.md` の action 節）から切った。**同じウェーブ内のレーンは、同じリポジトリで触るファイルが重ならない**ように分けてある。着手前に、open 中の PR と自分の PR で `git diff --name-only origin/main...` を比べ、重なりが無いことをもう一度確かめる（台帳の対象ファイル一覧は 2026-09-14 時点のもの）。両リポは独立に進めてよいが、`stockStock` の地図（B2）が要るレーンは stockStock 側を先にマージする。

**stockStock**（ウェーブ内は並列、ウェーブ間は前のウェーブの全 PR マージ後）

| ウェーブ | レーン | 台帳 ID | 主に触るファイル | 前提・備考 |
|---|---|---|---|---|
| S1 | S1 廃止ジョブと Notion ②⑥⑦ の撤去 | L-26（prices_daily 廃止）L-03 L-02 L-15 L-25 L-27 | `jobs/{prices_daily,reconcile_weekly,export_weekly}.py`、`collectors/{yfinance_prices,stooq_prices}.py`、`notion/{price_history,upsert,schema}.py`、`transform/{reconcile,technicals}.py`、`config.py`（`DB_REGISTRY`）、`runner.py`（`write_job_log`）、`cloud_store/datasets.py`、`.github/workflows/{prices_daily,reconcile_weekly,export_weekly}.yml`、`tests/test_jobs_smoke.py`（`EXPECTED`）ほか対応テスト、README / DESIGN の該当節 | 単独レーン（同じファイルを多く触るので 1 本にまとめる。PR は「② / prices_daily / stooq」→「⑥ / ⑦ / writer 表記」の 2 本に分けてよい）。マージ後に §7 P3 の Secret 削除・P9 の確認 |
| S2 | S2 移行計画の準備コードの削除 | L-06 L-07 L-08 L-33 | `cloud_store/{fin_parity,universe_guards,margin}.py`、`cloud_store/core_stocks.py`（DDL 生成・`build_column_update`）、`jobs/{core_stocks_migrate,margin_weekly}.py`、`collectors/jpx_margin.py`、`.github/workflows/margin_weekly.yml`、`docs/CONTRACTS.md`（G-fin-1 節）、対応テスト、kabulab-cf `instrument-type.ts` のコメント参照 | S1 の後（`test_jobs_smoke.py` を両方が触る）。`contracts/stock_code.py` と共有ベクタは触らない（A3） |
| S3 | S3a 一度きりツール・2 DB フォールバック・未使用 convert | L-04 L-05（P1 の退避後）L-09 L-10 L-12 L-13 | `config.py`、`cloud_store/{datasets,governance,yutai}.py`、`jobs/{freshness_probe,master_sync,core_stocks_migrate,yutai_backup}.py`、`.github/workflows/{master_sync,ops_check,yutai_backup}.yml`、`scripts/{migrate_halfyear_labels,dedupe_notion_disclosures}.py`、`convert/xls_to_csv.py`、`pyproject.toml` + `uv.lock`、L-13 の小シンボル、対応テスト | S2 の後（`core_stocks_migrate.py` / `core_stocks.py` を両方が触る）。L-05 は §7 P1 の退避が済んでから。L-13 で `local_store` が使うシンボルは除く |
| S3 | S3b 契約ファイルの両リポ配置 | L-36 | `worker/` のテスト、`.github/workflows/ci.yml`（`pending-peer` を外す）、`scripts/export_contracts.py`（新規）、kabulab-cf 側は `tests/fixtures/contracts/d1-license-map.json` と `public-columns.test.ts`、`ci.yml` | 両リポ同時マージ（§7 P6）。S3a とファイルは重ならない |
| S4 | S4a 孤児検査の縮小 | L-16 | `cloud_store/core_stocks.py`（`_observe` / `ALL_CHECKED_TABLES`）、`jobs/core_stocks_migrate.py`、`tests/test_core_stocks_migrate.py` | — |
| S4 | S4b 鮮度・claim・ジョブ履歴の走査削減 | L-17 | `cloud_store/datasets.py`、`jobs/{ops_check,license_map}.py`、`runner.py`（`record_job_run` の prune）、`tests/test_ops_slo.py`（`"COUNT("` を EXISTS 許容に） | E1 / E3 を守る |
| S4 | S4c master_sync の差分 PATCH | L-19 | `jobs/master_sync.py`、`notion/upsert.py`（`_master_map_from_pages`） | Notion 要求 −3,800/月 |
| S4 | S4d ⑤ の sha256 マップ | L-21 | `notion/file_upload.py`、`jobs/{edinet_daily,tdnet_hourly}.py` | — |
| S5 | S5a ① の code→page_id を D1 新表に | L-20（+ 必要なら L-22 の ⑤ 20 MB 規則） | `cloud_store/{schema,governance}.py`（`TABLE_LICENSE` に `jss_notion_pages`、`OBSERVED_TABLE_COUNT` 30）、`tests/fixtures/contracts/d1-license-map.json`（両リポ）、`jobs/master_sync.py`、`jobs/{tdnet_hourly,edinet_daily}.py`、`notion/upsert.py`、`tests/test_governance.py`（`PROD_TABLE_NAMES`） | S4c / S4d の後（同じファイル）。S3b の後（地図が両リポにある前提）。§7 P4（`apply_schema` を手で 1 回） |
| S5 | S5b CI 衛生と Issue 通知の共通化 | L-28 L-37 | `.github/workflows/ci.yml`、`pyproject.toml`（`-rs`）、`tests/test_fixture_policy.py`、EDINET フィクスチャ 5 件、`cloud_store/datasets.py`（`license_tag` の導出）、`tests/test_ops_slo.py` | S3b（ci.yml）・S4b（datasets / test_ops_slo）の後 |
| S6 | S6a 文書の圧縮と再編 | L-29 → L-30 → L-31（順に 3 PR） | `docs/*`、`README.md`、`AGENTS.md`、最後に src の docs ファイル名参照 25 箇所 | S1〜S5 で消えるものが決まってから。`docs/HANDOFF-2026-09*.md` は残す |
| S6 | S6b src の経緯コメントの圧縮 | L-32 | `src/jp_stock_pipeline/**`（上位 15 モジュール。`local_store` は残すので docstring だけ縮める） | S6a とはファイルが重ならない（src のみ）。`slo.py` の祝日は「緑を 1 営業日緩める」で未決コメントを消す |
| S6 | S6c テストの共通ダブル | L-34 | `tests/_doubles.py`（新規）、`tests/conftest.py`、D1 / R2 ダブルを持つ 11 テスト | S6b と `tests/` の docstring で重なりうるので、S6b の後にマージ |
| S7 | S7 テスト固定の緩和 | L-35 | `tests/test_jobs_smoke.py` の分割、文言固定の属性照合化 | S6 の後 |

**kabulab-cf**

| ウェーブ | レーン | 台帳 ID | 主に触るファイル | 前提・備考 |
|---|---|---|---|---|
| K1 | K1a Neon 遺物・一度きりスクリプト・旧 snapshot | L-38 L-39（prettier は残す）L-40 L-42 | `scripts/migrate/`、`drizzle.{rsi-screening,otakara-yutai,swing-trading,ir-catalog}.config.ts`、`drizzle/*.sql`、`services/*/drizzle/`、`drizzle/d1/meta/0000〜0011`、`package.json` + `pnpm-lock.yaml`、`tsconfig.json`、`eslint`、`.env.example`、`scripts/vpn/`、`scripts/vwap/build_stocks.py`、`scripts/vwap/lib/r2.ts`（`r2Delete`）、`flake.nix`（openvpn）、`public/sw.js` | `stocks.json` は触らない（G3） |
| K1 | K1b otakara の未使用コードと core-repo | L-41 | `services/otakara-yutai/src/{middleware,services/yutai-scraper.ts,services/yutai-data-provider.ts,validators}`、`data-scripts/fetch-yutai-data.ts`、`tests/unit/*`、`src/shared/db/core-repo.ts` | — |
| K1 | K1c 互換シムと rsi JSON API | L-43 L-44 | `services/rsi-screening/src/{index.ts,routes/screening.ts,routes/stocks.ts}`、`services/yuho-quant/src/routes/pages.ts`（`detectDeprecatedParams`）、`services/otakara-yutai/app.ts`（`?sort=&order=` の 4 行）、`src/shared/notion-archive/dataset.ts`（旧フラット DB 退避）、`docs/001` | §7 P8（Notion の目視）を先に |
| K2 | K2a 日次 sync のコスト改修 | L-47 L-49 L-57 | `src/cron/daily.ts`（Phase 1 / 4 / 6、annual の週 1 化、回収の時間予算）、`services/ir-catalog/src/services/ingest.ts`（`setWhere`）、`scripts/vwap/lib/r2.ts`（404 を即 throw）、`src/cron/*.test.ts` | rows_read の前後を EXPLAIN と `meta.rows_read` で実測 |
| K2 | K2b 公開面の JOIN 順 | L-48 | `services/rsi-screening/src/services/screening-service.ts`、`services/swing-trading/src/routes/pages.ts`、`services/financial-math/src/routes/pages.ts` | EXPLAIN で外側が派生表であることを確認 |
| K2 | K2c CSS 統合とフォント | L-61 | `src/shared/design.ts`、`services/{financial-math,rsi-screening,swing-trading}/src/views/layout.ts`、`services/otakara-yutai/app.ts`（CSS 部分のみ）、rsi の view テスト | K1c と `app.ts` を両方触るので K1c の後 |
| K2 | K2d CI の二重実行と migration 適用ヘルパ | L-63（lint ゼロ化を除く） | `.github/workflows/ci.yml`、`vitest.config.ts`、テストの migration 適用ヘルパ 1 本 | lint の warning ゼロ化は K5 |
| K3 | K3a 索引の整理 | L-45 L-46（swing 以外）L-50 | `src/shared/db/core-schema.ts`、`services/otakara-yutai/src/db/schema.ts`、`services/ir-catalog/src/db/schema.ts` + `services/ir-catalog/src/services/query.ts`（`recentHighSignal`）、`drizzle/d1/`（生成） | §7 P4（`DROP INDEX` / 部分索引 `CREATE`）。K2b の後（`idx_rsi_percentile_min` を残すか決まる） |
| K3 | K3b swing の表統合 | L-52（+ L-46 の swing 索引 2 本） | `services/swing-trading/src/db/schema.ts`、`src/cron/daily.ts`（`writeStockSnapshot` の screening / signals 部分）、swing の読み手 3 箇所、`drizzle/d1/` | §7 P4。stockStock の地図（`RETIRED_TABLES` に `swing_stock_screening`、B2）を先にマージ。K2a の後（`daily.ts`）。前日シグナルは消す（U-L52） |
| K4 | K4a D1 書込の往復削減 | L-56（案 A）+ L-51 の otakara 集計列 + L-60 の `src/cron` import 切替 | `src/cron/{daily,monthly,universe}.ts`、`src/shared/db/d1-http-client.ts` | K3b の後（書込文の形が決まる）。`/query` の複数文 + params 可否を先に確認 |
| K4 | K4b yuho の投影表 | L-51（yuho 部分） | `src/shared/db/projection-schema.ts`、`services/yuho-quant/src/**`、`src/cron/yuho-edinet.ts`、`drizzle/d1/` | §7 P4（`CREATE TABLE p_yuho_growth`）。stockStock の地図を先に |
| K4 | K4c Yahoo 系の統合と zod/mini | L-59 L-60（`src/cron` の import 切替は K4a） | `src/shared/yahoo/client.ts`、`services/vwap-analysis/{app.ts,lib/yahoo.ts}`、`src/shared/error-handler.ts`（新規）+ 各サービスの error-handler、`services/*/src/db/core-schema.ts` シム、validators 20 ファイル | `@hono/zod-validator` が mini を受けるか 1 ルートで先に確認 |
| K5 | K5a 一時ガードの撤去とコメント圧縮 | L-64 L-68 | `src/shared/db/public-columns.ts`、`services/swing-trading/src/routes/pages.ts`（日付比較）、`sector-ranking-key-switch.test.ts`、上位 20 ファイルのコメント、User-Agent 3 箇所 | §7 P5（`swing_sector_daily` の DELETE）の後 |
| K5 | K5b 配信キャッシュと同梱 | L-62 | SSR 一覧の `Cache-Control`、`public/_headers`、`public/vwap-analysis/vendor/`、フロントの参照 | EDINET 由来のみのページに限定 |
| K5 | K5c 文書の圧縮 | L-65 → L-66 → L-67（順に 3 PR） | `docs/**`、`README.md`、`CLAUDE.md`、`AGENTS.md`、`services/*/{CLAUDE,README}.md`、`wrangler.toml` / workflow のコメント | K1〜K4 の存廃が決まってから |
| K5 | K5d 小さなスキーマ整理 | L-53 | 1 銘柄 1 行の表の PK、`swing_sector_daily` の保持と `pct_5d`、`rsi_percentile.operating_margin_ttm` | §7 P4（表の作り直し）。K3a の後 |
| K6 | K6 残り | L-37（composite action）、L-63 の lint ゼロ化、X-01（K1 の要約取込後）、X-05 / X-06 / X-07 | `.github/actions/notify-failure/`、各 workflow、lint 対象、`yutai_benefits` の schema と `app.ts` の WEB 推定 UI、`active-equity.ts`、`source-scan.ts` | X-01 の `DROP COLUMN` は §7 P4 |

各ウェーブの終わりに §7 の本番手順（P1 は S3 の前、P3 は S1 の後、P4 は K3 / K4 / K5d / S5 の各 PR 直後、P5 は K5 の前）を挟む。見送り（L-23 / L-54 / L-55 / L-58）と Q4 で残すもの（L-01 / L-11 / L-14）は入れていない。

---

## 7. 本番で人が行う手順の一覧

| # | 何を | いつ | どう検証 | どう戻す |
|---|---|---|---|---|
| P0 | 先行 PR のマージ後の後片付け。**(a)(c) は 2026-09-14 に完了**: (a) #56 マージ後に 2026-09-11 分の `tdnet_hourly` / `edinet_daily` を `-f date=` で再実行し、文書単位の行（EDINET csv 23 / pdf 67、TDnet xbrl 77）の `doc_id` NULL が 0、全行が旧キーのまま（R2 に新規 PUT なし）を確認。09-14 以降は定時実行が新コードで処理する。(c) #34 → #57 の順で squash、両 main の `stock-code-vectors.json` の blob sha 一致と `cross-repo-contract` 緑を確認。(b) ローカルの wrangler ログ削除は §8.1 T3 | — | (a) `SELECT source, datatype, COUNT(*), SUM(doc_id IS NULL), SUM(instr(r2_key,'/_/')>0) FROM jss_raw_files GROUP BY 1,2` | (a) 再実行は冪等 (c) revert を同じ順で |
| P1 | **`yutai_backup` を 1 回流す**: Secret `KABULAB_D1_DATABASE_ID` を追加（値は kabulab-cf `wrangler.toml` の `database_id`。既存 Secret `CF_D1_DATABASE_ID` と同じ値と推定、未確認。代替は §8.1 T2 の 1 行 PR）→ `gh workflow run yutai_backup.yml -f check_only=true -f allow_shrink=false` → ログに「退避対象: 8295 行（推定額あり 5327 / 要約あり 8295）」→ `check_only=false` で本番 → 優待要約の取込後にもう 1 回（`with_value` が減っていれば `allow_shrink=true`） | L-04 / L-05 の削除 PR より前。要約取込（U1）の前後 | ログ「退避完了: jp-stock-supply/backup/yutai/<sha16>.json」。R2 索引 `backup/yutai/index.json`（R2 読取権限つきトークンで `wrangler r2 object get jp-stock-supply/backup/yutai/index.json --pipe`。手元に無ければジョブログの「退避完了」行で代替） | 退避なので戻す対象なし。R2 約 1.7 MB（推定）、rows_read 約 33k、$0 |
| P2 | **`migrate_halfyear_labels.py --apply`** — **完了**（2026-09-13〜14: 選択肢の追加 → ③ 8,315 件 → ④ 8,547 件 → D1 31 行。dry-run で残り 0 件）。残るのは `local_store`（端末B）に同じ移行が要るかの確認（`--target local`、未確認） | — | §8.3 チェック E の SELECT が 0（09-15 の `edinet_daily` 後にもう一度） | — |
| P3 | **Notion の Secret 除去**: `NOTION_DB_PRICES`（L-26 マージ後）、`NOTION_DB_EXPORTS`（L-15）、`NOTION_DB_JOB_LOG`（L-25）を `gh secret delete`。Notion 上の ②⑥⑦ DB と ① 配下の履歴子 DB（L-03）のアーカイブは任意（放置で無害） | 各 PR のマージ後、revert 判断が固まってから（目安: 1 週間） | 次の cron が success（該当 DB を読まない） | Secret を戻す（値は `db_ids.json` にある） |
| P4 | **D1 DDL**（§6.3 の手順で 1 PR ずつ。**完了**: 0018 を 2026-09-14 14:28 UTC に適用。4 表 PK 化 + `pct_5d` DROP、行数不変 3755/3755/1636/1636/2727。退避は表単位 JSONL + 復元点 14:28:01Z。EXPLAIN は索引使用、ops_check は適用前からの鮮度 yellow 3 件のみ）: L-45 `DROP INDEX` ×4 / L-46 `DROP INDEX` ×4〜5 / L-50 部分索引 `CREATE INDEX` / L-51 `CREATE TABLE p_yuho_growth` + `otakara_stock_scores` に 2 列 ADD / L-52 `swing_stock_indicators` に列 ADD → `DROP TABLE swing_stock_screening` / L-53 表の作り直し + `DROP COLUMN` ×2 / X-01 `yutai_benefits` の `DROP COLUMN` ×2 / L-20 `jss_notion_pages` は `apply_schema` を手で 1 回（§6.3-4） | 各 PR マージ直後・21:00 UTC の日次 cron より前（表数が変わるものは stockStock の地図を先に） | `sqlite_master`、EXPLAIN、`ops_check` 緑、B2 の表数照合 | Time Travel の bookmark（DB 全体が戻る。§6.3-6） |
| P5 | **`swing_sector_daily` の `date < '2026-09-14'` を DELETE**（L-64） | **未実行・21:00 UTC cron 待ち**: 2026-09-14 分の日次 cron が 34 行（33 業種 + 未分類）を書いたのを確認した後 | `SELECT date, COUNT(*) FROM swing_sector_daily GROUP BY 1` が 09-14 以降だけ。公開面の業種ランキングが出る | Time Travel |
| P6 | **共有コード置換の 2 リポ同時マージ**（L-36 の地図同梱、および今後の契約ファイル変更すべて）: 両 PR を同時 open → kabulab-cf 側を先に squash → stockStock 側の `cross-repo-contract` を Re-run → マージ → kabulab-cf main を Re-run | 契約ファイルを変えるとき | 両 main の contracts ファイルの sha 一致、両 CI 緑 | revert を同じ順で |
| P7 | **`doc_id` 救済後の再実行** — **完了**（2026-09-14、09-11 分。P0-(a)）。09-14 以降の営業日は定時実行が新コードで処理するので再実行は不要 | — | P0-(a) の SELECT | — |
| P8 | **Notion 旧フラット DB の目視確認**（L-44）: 「バックアップ」配下に `適時開示｜ir-catalog` が無いこと | L-44 マージ前 | 目視 | 削除前なので戻す対象なし |
| P9 | **`prices_daily` 廃止**（L-26）: PR マージ後、`gh run list --workflow prices_daily.yml` に schedule が現れないこと。R2 `jp-stock-raw` の yfinance 行と `jss_raw_files` はそのまま | マージ後の翌営業日 | `ops_check` の鮮度データセット（`core_stock_financials` は kabulab-cf が更新）が緑 | revert + P3 で消した Secret の復元（値は `db_ids.json`） |

---

## 8. 残タスクと判断待ち

### 8.1 ユーザー本人の作業（本人のログイン・恒久削除・Secret が要る）

| # | 作業 | 備考 |
|---|---|---|
| T1 | **GitHub Support への purge 依頼 2 件**（リポジトリごと）。手順・対象コミット・依頼文の下書き・検証コマンドは運用者の非公開手順書。送信前に、対応する PR 本文の編集履歴を UI（edited → Delete revision）で消す（対象 PR も非公開手順書）。完了後、非公開手順書の検証コマンドで旧 URL が 404 になることを確認し、未了節を消す | fork 0・外部アーカイブ無し（2026-09-14 実測）。任意で squash 済み PR の途中コミットも同梱（推奨: 同梱） |
| T2 | **`yutai_backup` の Secret 追加と 1 回実行**（§7 P1） | 代替: `yutai_backup.py` を他 3 job と同じフォールバック（`kabulab_d1_database_id or d1_database_id`）に直す 1 行 PR |
| T3 | **wrangler ログの削除**: ローカルの wrangler ログ（`WRANGLER_LOG_PATH` 未設定時の既定置き場 = xdg config 配下の `.wrangler/logs`。D1 応答＝掲載文を含みうる）を削除する。手順は運用者の非公開手順書 | #55 / #33 は 2026-09-14 マージ済み。以後この 2 リポ由来の wrangler は既定でログを書かず自動掃除も呼ばないので、既存分は手で消す |
| T4 | **`doc_id` の後片付け**: 旧セッションの作業ツリー（`git worktree list` で main の祖先・変更 0 のもの）を削除。#56 のマージと 09-11 分の再実行は完了 | — |
| T5 | **優待要約の取込**（U1 で経路を決めた後）: 結果 JSONL を gitignore 済みディレクトリに置く → `pnpm yutai:summary:import --tasks services/otakara-yutai/data-scripts/data/summary-tasks/tasks-2026-09-13-violations.jsonl --results <結果>`（dry-run）で「60 タスク / 85 行・はじいた 0」→ `--apply` → `short_summary` の契約違反集計が 0。P1 の退避を前後に | 取込前にローカルで `pnpm sync:monthly` を流さない（内容キーが変わり全件 stale）。出力を Issue / PR / Notion に貼らない |

### 8.2 判断待ち（推奨を付す。決まるまで着手しない）

| # | 判断 | 選択肢 | 推奨 | 判断点 |
|---|---|---|---|---|
| U1 | 優待要約 85 行（60 タスク）をどの LLM 経路で作るか | (A) Cursor にタスクファイルを手渡し (B) Cursor cloud agent に D1 Read 専用トークンを預ける (C) Claude Code で作り dry-run を人が見て `--apply` | **(C)**（追加課金・新トークン・新 cron なし。(B) は掲載文 8,295 行を含む DB 全体の読み取り権を第三者環境に預ける） | 掲載文を Anthropic 経由の LLM に読ませてよいか（Cursor に渡すのと同じ性質）。→ **ユーザー決定 (A)**。2026-09-14 時点で D1 に missing 0 / 契約違反 0（8295 行全件要約済み、公開 API で表示確認）を検証、手渡し対象なし |
| U3 | `public/vwap-analysis/data/stocks.json`（2026-06 の JPX 一覧の写し 4,445 件・区分列を含む。生成経路が壊れていて再生成できない） | (A) 非普通株と区分を落とす (B) D1 の active∧equity から `[code,name]` を月次生成し、取込母集団は「R2 `daily/` 既存キー ∪ D1」に切り離す (C) Worker API 化 (D) 現状維持 | **(B)**（(A) は生成元が壊れており結局 (B)、(C) は実行時 D1 読みが増える。取込を切り離せば ETF を含む R2 `daily/` は今どおり更新され外部読者を壊さない） | (a) JPX 由来の銘柄名を公開面に残すか（X-06）(b) 公開 git 履歴と旧 vwap リポの写しを消すか（消すなら Support の 3 件目、T1 とは別便） |
| U-⑤（確認事項。決定 D-14-2 は維持） | Notion ⑤ の「20 MB 超は R2 キー参照」を実装する必要があるか | 20 MB 超を出していたのは `prices_daily`（116〜291 MB CSV）と `export_weekly` で、両方廃止 | 規則は入れずに済む可能性が高い（未確認: EDINET / TDnet 原本で 20 MB 超が出るか。`SELECT source, datatype, MAX(size_bytes) FROM jss_raw_files GROUP BY 1,2` で分かる） | L-22 を実装するか、I1 を「原本添付必須」のまま据え置くか |
| U5 | enrich-from-web（楽天 API の優待金額推定）を作り直すか | (A) 作り直さない（列と UI を削除 = X-01）(B) 要約タスクに「推定根拠 web」を足して再設計 (C) 保留 | **(A)**（契約 `llm-summary-task.md` §6 が相場推定を禁じる。本番反映 0 行で効果未実証。対象は上限 336 行程度） | — |
| U-L23 | L-23（`supply/{code}.json` の日次シャード化） | G5 の schema=1 契約と MCP の読み替えを許容するか | 今回は見送り | R2 Class A/B 各 −91k/月の価値 vs 契約変更 |
| U-L51（決着） | L-51 の地域バケット比率 4 列 | 投影に持つ / 地域指定時だけ従来クエリ | **投影に持つ**（2026-09-14 決定） | 実装時に rows_read を実測して PR に載せる |
| U-L52（決着） | L-52 で取得失敗銘柄の前日シグナル | 残す / 消す | **消す**（J3、2026-09-14 決定） | — |
| U-L46 | `ir_disclosures_pubdate_idx` に将来の期間検索の用途があるか | — | DROP | — |
| U-L63 | `test:coverage`（`@vitest/coverage-v8`）を残すか | — | 未提示 | — |
| U-L39 | Neon は解約済みか（README「残るは Neon 解約」） | — | 解約済みなら節ごと削除 | 未確認 |
| U-L65 | Workers のプラン（`wrangler.toml` は「無料プラン」、TARGET §9.1 は「Paid $5 確定」） | — | ダッシュボードで確認して記述を揃える | 未確認（`wrangler whoami` は権限不足） |

決着済みで着手不要になったもの: U4（P4b 前の 4 件）は D-14-1 で不要。U6（`prices_daily` の timeout）は #54 で 300 に戻し、D-14-5 で job ごと廃止。U2（Support 依頼の範囲）は T1 に統合（推奨: 任意項目も含めて 2 件）。

### 8.3 月曜（2026-09-14）以降の確認チェックリスト

共通: D1 は kabulab-cf で `nix develop -c npx wrangler d1 execute kabulab-cf --remote --json --command "..."`（#33 マージ後は devShell が既定でログを書かない）。**SELECT のみ。`description`（掲載文）は SELECT しない。** cron は実績で 2〜4 時間遅れる。C は #54 で決着したため欠番。

| # | いつ（JST） | 何を | 期待値 |
|---|---|---|---|
| A | 09-14 10:00 以降 | `gh run list --workflow tdnet_hourly.yml --limit 3 --json event,conclusion` / `SELECT data_date, source, COUNT(*) AS n, SUM(doc_id IS NULL) AS doc_null FROM jss_raw_files GROUP BY 1,2 ORDER BY 1,2` | schedule が success。09-14 の TDnet 行が増え、#56 マージ（01:20 UTC）以降の run では文書単位の行の `doc_id` が埋まる。→ **確認済み**（14:30 UTC: schedule success、09-14 TDnet 91 行・doc_null 2 のみ） |
| B | 09-14 13:00 以降 | `gh run list --workflow supply_daily.yml --limit 1` / `SELECT dataset, latest_data_date FROM jss_dataset_freshness WHERE dataset='jsf_supply'` | success、2026-09-14。→ **確認**（14:30 UTC: success、latest=09-11＝直近金曜の一次データ。月曜分は未公表のため 09-15 再確認） |
| D | 09-15 朝 | `gh run list --workflow prices_daily.yml --limit 1 --json conclusion,createdAt,updatedAt` | success（timeout は #54 で 300 分）。cancelled なら `gh workflow run prices_daily.yml` で再実行。廃止（L-26）までの暫定 |
| E | 09-15 朝（半期報告書の提出期限 9/14 の翌日） | `gh run list --workflow edinet_daily.yml --limit 1` / `SELECT COUNT(*) AS n2q FROM jss_financials WHERE disclosure_type='2Q' AND fiscal_period_end>='2024-06-30'; SELECT disclosure_type, COUNT(*) FROM jss_financials GROUP BY 1` | success、n2q=0（09-14 朝: 0、「中間」31）。残っていれば `migrate_halfyear_labels.py --target d1` の SQL を fold → drop_folded → apply の順に 1 文ずつ |
| F | 09-15 朝 | `nix develop -c uv run python scripts/dedupe_notion_disclosures.py --backup-dir <ローカルの退避先> --plan-out <退避先>/plan.json` → `plan.json` の `groups` 件数 | 0（読み取りのみ。バックアップ 60,810 + 当日分、約 10 分）。`--apply` は流さない。0 を確認したら L-10 で削除可 |
| G | 09-15 02:00 頃 | `gh run list --workflow ops_check.yml --limit 1` / steps の conclusion / `gh issue list --state open --search "[SLO違反]"` / `SELECT dataset, row_or_object_count, latest_data_date FROM jss_dataset_freshness WHERE dataset IN ('core_stocks','prices_daily','yutai_benefits')` | 4 step とも success、Issue 0、core_stocks 3,810（09-14 朝は削除前の 3,819 のまま）、yutai_benefits 8,295 |
| H | 09-15 08:00 頃 | kabulab-cf `gh run list --workflow stock-sync.yml --limit 1` / `SELECT COUNT(*), MAX(as_of), MAX(source_max_date) FROM p_momentum; SELECT date, COUNT(*) AS sectors, SUM(stock_count) FROM swing_sector_daily WHERE date>='2026-09-11' GROUP BY date; SELECT sector, stock_count FROM swing_sector_daily WHERE date='2026-09-14' AND sector IN ('機械','未分類')` / `curl -s '<Worker URL>/financial-math/emh?type=momentum' \| grep -c '該当 0 件'` | p_momentum ≈ 3,700（09-14 朝 0 行 = 初回充填）、as_of=2026-09-14。swing_sector_daily 09-14 は 34 行・機械 ≈ 209・未分類 1（09-11 の「機械」欠落 X-10 が再発しないこと）。grep 0 |
| I | 10-02 06:00 の `master_sync` 後 | `SELECT COUNT(*) FROM core_stocks WHERE is_active=1 AND instrument_type='equity' AND (sector33 IS NULL OR sector33='')` | 1（EDINET コードリストに居ないグロース 1 社。09-14 朝実測）。L-19 適用後は run 時間が 47 → 約 4 分 |
| J | Support 完了後 | 旧 URL の curl が 404、`git ls-remote origin 'refs/pull/*' \| wc -l` が減る（依頼前 kabulab-cf 31 / stockStock 48） | 全部 404 で T1 完了 |
| K | 要約取込後 | `SELECT COUNT(*) AS total, SUM(short_summary IS NULL) FROM yutai_benefits` と契約違反集計（`short_summary` 列のみ） | any_violation=0。公開面のカードで 1 行表示を目視。→ **確認済み**（14:36 UTC: total 8295・missing 0・違反 0、公開 API で benefitSummary 表示） |
| L | 完了（2026-09-14） | チェック A の SELECT | 09-11 分: 文書単位の行の NULL 0、一覧・yfinance・日証金の行だけ NULL |
| M | 完了（2026-09-14） | `gh api repos/<owner>/<repo>/contents/tests/fixtures/contracts/stock-code-vectors.json?ref=main --jq .sha` が両リポで一致、両 `cross-repo-contract` 緑 | 一致（blob sha 6ac047f…） |

---

## 9. 用語と参照

### 9.1 Notion の DB 番号（設計書の呼称。⑧⑨ は Notion に対応する DB が無い）

| 番号 | 名前 | 実体 | 状態 |
|---|---|---|---|
| ① | 銘柄マスタ | Notion `NOTION_DB_STOCK_MASTER`。D1 `core_stocks` が対応（writer は kabulab-cf `universe.ts`、`sector33` だけ stockStock `master_sync`） | 残す |
| ② | 株価テクニカル | Notion。毎日 3,720 行全置換、読み手なし | **廃止**（D-14-2 / D-14-5） |
| ③ | 財務サマリ | Notion + D1 `jss_financials`（writer stockStock `edinet_daily` / `tdnet_hourly`） | 残す |
| ④ | 開示書類 | Notion + D1 `ir_disclosures`（kabulab-cf）/ `jss_raw_files` の索引 | 残す |
| ⑤ | 原本ファイル | Notion（添付）+ R2 `jp-stock-raw` + D1 `jss_raw_files` | 残す（20 MB 規則は §8.2） |
| ⑥ | 時系列エクスポート | Notion。`export_weekly` が書く | **廃止**（D-14-2） |
| ⑦ | 収集ジョブログ | Notion。D1 `jss_job_runs` と完全重複 | **廃止**（D-14-2）。実体は `jss_job_runs` |
| ⑧ / ⑧' | XBRL 全ファクト / 需給 | ⑧ は端末B の PostgreSQL（`local_store`、D-14-4 で残す）と設計上の R2 Parquet。⑧' は R2 `jp-stock-supply/supply/{code}.json`（日証金・JPX 信用残） | 残す |
| ⑨ | 株主優待 | D1 `yutai_benefits`（kabulab-cf）。LLM 派生値（`short_summary` / `estimated_value`）は再取得不能 → R2 `backup/yutai/` に退避（P1） | 残す |

### 9.2 主要ファイル

stockStock:
- 収集ジョブ `src/jp_stock_pipeline/jobs/`（`runner.py` が骨格。`edinet_daily` / `tdnet_hourly` / `supply_daily` / `master_sync` / `ops_check` / `freshness_probe` / `license_map` / `core_stocks_migrate` が残る）。
- Cloudflare 層 `src/jp_stock_pipeline/cloud_store/`（`d1.py` / `r2.py` / `sink.py` / `keys.py` / `schema.py` / `governance.py` / `datasets.py` / `slo.py` / `core_stocks.py`）。
- Notion 層 `src/jp_stock_pipeline/notion/`（`upsert.py` / `schema.py` / `file_upload.py`）。
- 契約 `src/jp_stock_pipeline/contracts/stock_code.py`、`tests/fixtures/contracts/{stock-code-vectors.json, d1-license-map.json}`。
- 公開 Worker と MCP `worker/`（`src/shared/routes.ts` / `mcp.ts` / `license.ts`）。
- workflow `.github/workflows/`（残る schedule: `master_sync` / `tdnet_hourly` / `edinet_daily` / `supply_daily` / `ops_check`。手動: `cloud_check`。push / PR: `ci`）。
- 文書 `docs/`（L-31 後は `SPEC.md` / `TARGET-ARCHITECTURE.md` / `DECISIONS.md` + 本文書）。

kabulab-cf:
- 取込 `src/cron/{daily,monthly,universe,yuho-edinet,ir-catalog-tdnet}.ts`、`scripts/sync/`、`scripts/vwap/`。
- 共有 `src/shared/db/{core-schema,active-equity,public-columns,projection-schema,d1-http-client}.ts`、`src/shared/jpx/`、`src/shared/yahoo/`、`src/shared/notion-archive/`。
- サービス `services/<slug>/`（`app.ts` + `src/db/schema.ts` + views）。
- スキーマ `drizzle/d1/`（`_journal.json` + 最新 snapshot + 番号付き SQL。適用は手動）。
- 静的 `public/`（`vwap-analysis/data/stocks.json` は §8.2 U3）。
- workflow `.github/workflows/{stock-sync,vwap-ingest,catchup,ci}.yml`。

### 9.3 テストの走らせ方

前提（手元に要るもの）:
1. 依存の同期: stockStock は `nix develop -c uv sync`、`worker/` と kabulab-cf は `pnpm install --frozen-lockfile`（devShell に node / pnpm がある）。
2. Cloudflare の認証（D1 の SELECT / 手動 migration / R2 の確認）: `CLOUDFLARE_API_TOKEN`（D1 Edit、R2 は別途権限）と `CLOUDFLARE_ACCOUNT_ID` を環境変数か kabulab-cf の `.env` に置く（`wrangler.toml` に account_id は無い）。`wrangler login` の OAuth でもよい。
3. stockStock の `.env`: `NOTION_TOKEN`、`NOTION_DB_*`（値は `db_ids.json` と同じ）、`EDINET_API_KEY`、`CF_*`（D1 / R2）。`local_store` を使うなら `LOCAL_DB_*`。
4. kabulab-cf の `.env`: `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` / `D1_DATABASE_ID`（`wrangler.toml` の値）。要約取込の `--apply` は D1 Edit 権限が要る。

```bash
# stockStock
nix develop -c uv run ruff check src tests
nix develop -c uv run pytest            # 2026-09-14: 1,478 passed / 103 skipped（フィクスチャ未取得分は skip）
cd worker && pnpm test                  # 公開 Worker / MCP

# kabulab-cf
nix develop -c pnpm typecheck && nix develop -c pnpm lint && nix develop -c pnpm test
nix develop -c pnpm db:generate:d1 && git status --short drizzle/d1   # 変更が無いこと（A4）
```

- 両リポの `ci.yml` は `cross-repo-contract` で相手 main の契約ファイルと `diff`。契約を変える PR は §7 P6 の順で。
- D1 の実測は `wrangler d1 execute … --remote --json` の `meta.rows_read` を使う（F2）。1 セッションの読み取り上限は 5 万行を目安にし、掲載文（`description`）は SELECT しない。

### 9.4 用語

- **地図**: `d1-license-map.json`（列単位ライセンス地図）。宣言は stockStock、kabulab-cf は L-36 で同一バイト列を持つ。
- **claim / writer**: `jss_writer_claims`。どの表・列群を誰が書くかの宣言（`core_stocks/base = kabulab-cf` 等）。`ops_check` が実表と照合する。
- **投影表**: `p_*`（`p_momentum`、L-51 で `p_yuho_growth`）。読み取り面のために事前集計した表。rows_read を下げる唯一の手段（被覆索引は効かない、F2）。
- **G-core-5 / E7**: `core_stocks_migrate --verify` の検査名（孤児検査 / 列ドリフト）。
- **案 B（取込母集団）**: `instrument_type='equity' OR (is_active=0 AND instrument_type IS NULL)`。
- **P4b〜P8**: 中止した移行計画の段階名（D-14-1）。文書に残る参照は L-30 で削除する。
- **先行 PR**: 2026-09-14 に open した 6 本（§2.3 末尾）。
