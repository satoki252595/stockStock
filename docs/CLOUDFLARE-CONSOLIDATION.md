# Cloudflare 統合と重複解消の監査 (2026-09-11)

依頼者: satoki252595 / 調査: Claude Opus 5 / 読み取り専用調査（Cloudflare へは `wrangler d1 execute --remote` の SELECT と R2 の一覧照会のみ）

一次制約（ユーザー指示）:
1. 正本は Cloudflare 上のコスパが良い場所に置く。Notion はサブ。
2. 将来 MCP / API から呼ばれる前提で設計する。
3. **同じデータが Cloudflare 上に二重に存在してはならない**（コスト）。
4. **同一リソースを更新する仕組みが他にもある場合は、勝手に触らずユーザーの判断を仰ぐ。**

---

## 0. 出発点の事実

**stockStock は現時点で Cloudflare リソースを 1 つも持たない。**
`grep -rniE "cloudflare|wrangler|r2\.|boto3|d1_database|workers\.dev|R2_BUCKET" src/ .github/` がヒット 0 件。`.env` にも `CLOUDFLARE_*` / `R2_*` が無く、物理的に書き込めない。

したがって制約3は現時点で満たされており、以下の「重複」はすべて **将来 stockStock を Cloudflare に載せた場合に発生するもの**である。未コミットの ⑧需給・⑨株主優待は Notion 側も実測 0 行のため、**今なら撤退コストがゼロ**。

## 1. 既存 Cloudflare 資産（実測）

アカウントは単一（`5880d85c320ca5fee8baf9efddd005ed`）。kabulab-cf / kabuMCP / domain が同居し、**無料枠はアカウント共有**。

### D1 は 1 個（drizzle config が 6 個あるだけ）

`kabulab-cf` (`639c0315-0d41-4345-95ab-aec5906224ed`)。テーブル接頭辞で全サービスが同居。2026-09-11 実測:

| テーブル | 行数 | 粒度 | 備考 |
|---|---|---|---|
| `core_stocks` | 3,818 | 銘柄 | JPX 由来。market / sector / is_yutai |
| `core_stock_financials` | 3,764 | 銘柄・最新1行 | Yahoo 由来の市場派生値 |
| `core_stock_annual_financials` | 15,943 | 銘柄×年度 | revenue のみ |
| `swing_daily_ohlcv` | 336,185 | 銘柄×日付 | 3,764銘柄 / 2025-11-13〜2026-09-09。**90営業日でローリング削除** |
| `swing_stock_indicators` | 3,764 | 銘柄・最新1行 | SMA/RSI/MACD/ATR/フィボナッチ |
| `rsi_percentile` | 3,764 | 銘柄・最新1行 | 履歴テーブルは 2026-04 に削除済み |
| `finmath_price_snapshot` | 3,759 | 銘柄・最新1行 | |
| `finmath_daily_ohlcv` | 3,435 | 銘柄×日付 | **7シンボルのみ**。オンデマンドの遅延キャッシュ |
| `yutai_benefits` | 8,314 | 銘柄×優待条件 | 1,670銘柄。**2026-06-22 で更新停止** |
| `ir_disclosures` | 37,338 | 開示 | 3,807銘柄 / **2023-06-05〜2026-09-10** |
| `yuho_documents` | 21,928 | 書類 | EDINET 120/130 のみ |
| `yuho_order_facts` / `yuho_overseas_facts` | 23,114 / 14,658 | | 受注・海外売上 |

`kabumcp` D1 (`4a6b318e-...`) は会員・課金専用で株式データ 0 行。

### R2 バケット `vwap-data`

| プレフィックス | 件数 | 内容 |
|---|---|---|
| `daily/{code}.json` | 4,444 | 日足 **10年** / 0.694 GB |
| `intra/{code}.json` | 4,272 | 5分足・直近約1年 |
| `margin/{week}.json` | 11 | **JPX 週次信用残高** |

**既存構成は「R2 = 長期時系列（per-code JSON）／ D1 = 直近・断面」という二層になっている。** これは今回目指す形と同じであり、発明し直す必要はない。

### 書き込み経路（既に実証済み）

GitHub Actions の Node が **D1 REST API**（`src/shared/db/d1-http-client.ts`）と **R2 の S3 互換 API**（`@aws-sdk/client-s3`）へ直接書く。stockStock も同じパターンを踏襲でき、**Notion の実効 1.3 req/s から解放される**。

## 2. 重複マトリクス

| stockStock | 既存 Cloudflare | 重複度 | 寄せ先 |
|---|---|---|---|
| ①銘柄マスタ | `core_stocks` / Static `stocks.json` / kabuMCP `entities` | 部分・**CF 上に既に3本** | `core_stocks` に列追加。業種は JPX 由来と EDINET 由来を混ぜず別列に |
| ②株価（日足） | R2 `daily/`(10年) / `swing_daily_ohlcv`(90日) / `finmath_daily_ohlcv`(7銘柄) | **完全重複・源泉も同一 Yahoo。CF 内で既に3重** | R2 `daily/{code}.json` 単一正本。stockStock の日足を CF に持ち込まない |
| ②テクニカル | `swing_stock_indicators` / `rsi_percentile` / `otakara_stock_financials` | 部分・**SMA/RSI/MACD が3重** | `swing_stock_indicators` に列追加（SMA200・BB±2σ・52週高安・売買代金） |
| ③財務サマリ | `core_stock_financials` ほか | 部分（**用途が別物**） | CF に載せない。③は期別の一次開示、CF 側は最新の市場派生値 |
| ④開示（TDnet） | `ir_disclosures` 37,338行 | ほぼ完全重複（同一 yanoshin API） | `ir_disclosures` に `split_ratio`/`split_factor`/`effective_date` を列追加 |
| ④開示（EDINET） | `yuho_documents`(120/130) / kabuMCP `filings` | 部分・**CF 内で2箇所に分散** | **要判断** |
| ⑤原本ファイル | **対応物なし** | 重複なし＝stockStock 固有の価値 | **要判断**（R2 が唯一の置き場） |
| ⑥⑦ | 対応物なし | 重複なし | CF に載せない（読み手がいない） |
| ⑧XBRL全ファクト | `yuho_order_facts` ほか | 部分（切り口が別物） | stockStock 側が上位素材 |
| **⑧需給（JPX信用残）** | **R2 `margin/{week}.json` 10週** | **完全重複（同一 JPX PDF）** | **要判断** |
| **⑨株主優待** | **`yutai_benefits` 8,314行** | **完全重複（取得元・手順・URL まで同一）** | **要判断** |

### Cloudflare 内部の重複（stockStock と無関係に既に存在）

- 日足 OHLCV が 3 重（アクセスパターンが違うため単純な一本化は不可）
- ファンダ・スナップショットが 3 重（`price/per/pbr/dividend_yield/eps/bps/roe/roa/market_cap` が同一列構成）→ 解消可能
- SMA/RSI/MACD が 2 重。`swing_stock_indicators` のスキーマ自身が「sma_25 は swing では使わず otakara のために計算している」と明記 → 解消可能

## 3. 触ってはいけないリソース（更新主体が複数）

| リソース | 更新主体 | 理由 |
|---|---|---|
| R2 `vwap-data` 全体 | **4系統**（kabulab-cf の `vwap-ingest.yml`、手動 `scripts/sync/all-daily.ts`、vwap リポジトリの未追跡 workflow 2本、vwap Worker の Cron） | 読み手も Worker 以外に2つ |
| R2 `margin/` の既存10週 | 同上 | **2026-06-12〜07-31 は JPX が既に削除済みで再取得不能**（07-03・07-10 は恒久欠測） |
| D1 `core_stocks` | **5系統** | どの writer がどの列を上書きするかの整理が先 |
| D1 `ir_disclosures` | 2系統 | `notion_page_id` で Notion 子DBと結合。TDnet 原本は約31日で purge されるため **Notion が実質の原本保管庫** |
| D1 `yuho_*` | Worker 認証ルート + 手動2本 | |
| D1 `yutai_benefits` | 手動チェーン | **Phase3 で全削除→再投入**。`short_summary` と `estimated_value` は LLM 解釈と楽天API推定で積んだ再取得不能な資産 |
| Notion『バックアップ』ツリー | kabulab-cf の2 workflow + 株ラボ-Youtube | stockStock とは別ツリーだが **同一ワークスペース「はぴまね」＝課金とAPIレート制限を共有** |
| Neon PostgreSQL | kabulab-cf と kabulab_tool が同一 `DATABASE_URL` | 実在未確認。確認前に何も操作しない |

## 4. 設計を縛る硬い制約（公式ドキュメント実取得）

- **D1 の 10GB/DB 上限は申請でも引き上げ不可**（"the 10 GB limit of a D1 database cannot be further increased"）。②株価（年95.5万行）と⑧XBRLファクト（年792万行）は無限に伸びるため、**時系列は最初から D1 に置かない**。
- **⑤原本は R2 以外に置き場が物理的に存在しない**（D1 は1行2MB、KV は25MiB、DO は10GB）。
- **原本サイズは実測で 5年約52GB**（年10.36GB）。DESIGN.md の「690GB」は約13倍の過大。R2 なら月 $0.79 程度。
- **R2 にオブジェクトバージョニングが存在しない**（API 未実装）。原本キーは docID・取得日・ハッシュ入りの immutable 設計にし、追記のみとする。
- **D1 の課金軸は走査行数**（"rows read measure how many rows a query reads (scans)"）。`LIMIT` では安くならない。外部に自由な絞り込み面を出さず、索引でカバーされる述語のみ受ける固定パラメータのツール／エンドポイントに限定する。
- **Notion ホストのファイル URL は 1 時間で失効**（"Don't cache or statically reference these URLs."）。**物理ファイルを外部サーバから取る用途に Notion 添付は使えない。**
- **Notion のクエリ結果は 10,000 件で silent truncation**、1DB 250,000 行上限。全銘柄横断の 1DB に日次で積むと約2.6日でクエリ上限。
- Notion の実効書き込みレートは **1.04〜1.27 req/s**（`_Throttle` の実効間隔は `max(0.4s, レイテンシ)`）。データ種別を3つに増やすと日次 8h21m で GitHub Actions の timeout を超える。

### リサーチの誤りの訂正

「GitHub Actions の private 無料枠 2,000分/月が先に破綻する」は **当たらない**。`stockStock` も `kabulab_tool_cloudflare` も PUBLIC リポジトリで Actions は無制限・無料（private なのは手動デプロイの `kabuMCP` のみ）。

## 5. 未確認・ユーザー確認が必要

**解決済み（2026-09-11）:**

- **Workers プランは Paid**（ユーザー確認）。kabulab-cf の README「無料プランで運用」という記載は実態と食い違っており、いずれ訂正が要る。
- **D1 `kabulab-cf` は 71.7 MB / 20テーブル / APAC / read replication disabled**（`wrangler d1 info`）。Paid の 1DB 10 GB に対し 0.7%。本設計の5年増分を足しても 5.7%。

**未解決:**

- R2 バケットの現使用量（アカウント共有枠）。
- Neon PostgreSQL の実在と課金の有無（D7）。

## 6. 判断待ち事項

| # | 論点 | 決定 |
|---|---|---|
| D1 | vwap リポジトリの未追跡 workflow | **アーカイブ済み**（vwap `79642b6`。`archive/superseded-ingest/` へ退避） |
| D2 | ⑨株主優待のライセンス評価 | **みんかぶを使わず TDnet/EDINET から構築**。kabulab-cf の掲載文は公開面から外す |
| D3 | ⑧需給の取得一本化と R2 `margin/` の扱い | **stockStock に一本化し `margin/` を後方互換で拡張**。既存10週は削除しない |
| D4 | stockStock を Cloudflare に載せる範囲 | **全面正本化**。kabulab-cf の既存を段階的に置き換える |
| D5 | `swing_daily_ohlcv` の扱い | 設計に反映（[CF-CANONICAL-DESIGN.md](CF-CANONICAL-DESIGN.md) の移行フェーズ P7） |
| D6 | Notion の二重保持 | 未決（設計の「残る判断事項」へ） |
| D7 | Neon PostgreSQL の実在確認と廃止判断 | **未決・ユーザー確認待ち** |
| D8 | 休眠リポジトリ（kabulab_tool / kabu-insight-saas）の処遇 | **未決・ユーザー確認待ち** |

設計仕様は [CF-CANONICAL-DESIGN.md](CF-CANONICAL-DESIGN.md) を参照。

### みんかぶ利用規約の逐条確認（2026-09-11 実取得）

D2 の根拠。利用規約（info.minkabu.jp/terms/、令和3年4月25日改定、全18条）:

| 条項 | 原文 | 段階 |
|---|---|---|
| 第7条(18) | 「本サイト公認以外の方法による、プログラム、スクレイピング等により本サイトのコンテンツを機械的に取得する行為」 | 取得 |
| 第7条(19) | 「本サイトのコンテンツを私的使用の範囲を超えて蓄積する行為」 | 蓄積 |
| 第14条1項 | 「運営者及びライセンサーの許諾を得ずにコンテンツを第三者に使用させたり公開させたりすることはできません」 | 公開 |
| 第7条(22) | 「事業者（個人、法人問わず）が本サイトを利用して調査する行為」 | 収益化の有無を問わない |

第2条12 が「コンテンツ」を「データ等の情報をいいますが、これらに限りません」と定義しており、**事実データだけを切り出す行為を許す条項は存在しない**。robots.txt は `User-agent: GPTBot / Disallow: /` を明示。ヘルプセンターは「著作権侵害が確認された場合には、必要に応じて法的措置を講じる場合がございます」と予告。

`licensing.py` の `Source.MINKABU` → `PERSONAL_ONLY` を緩める根拠は規約内に存在しない。優待掲載文の実体は発行会社のIR文の転記であるため、**同じ内容を TDnet/EDINET から直接取得すればみんかぶ規約の適用外**になる。これが公開可能にする唯一の経路。

