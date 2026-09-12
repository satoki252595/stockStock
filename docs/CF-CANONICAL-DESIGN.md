# stockStock 全面正本化 設計仕様 (2026-09-11)

依頼者: satoki252595 / 設計: Claude Opus 5

前提の事実は [CLOUDFLARE-CONSOLIDATION.md](CLOUDFLARE-CONSOLIDATION.md)（既存資産の棚卸し・重複マトリクス・触ってはいけないリソース）を参照。

## ユーザー決定（この設計の与件）

| # | 決定 |
|---|---|
| 1 | 正本は Cloudflare。Notion はサブ。将来 MCP/API から呼ばれる前提 |
| 2 | **同じデータが Cloudflare 上に二重に存在してはならない**（コスト） |
| 3 | **stockStock を全面的な正本にし、kabulab-cf の既存を段階的に置き換える** |
| 4 | 信用残は取得を stockStock に一本化し、R2 `margin/` を後方互換で拡張（既存10週は削除しない） |
| 5 | **株主優待はみんかぶを使わず、TDnet/EDINET の適時開示から自前で構築する** |
| 6 | **kabulab-cf `/otakara-yutai` の掲載文（description）を公開面から外す** |
| 7 | vwap リポジトリはアーカイブ（実施済み: vwap `79642b6`） |
| 8 | 全面正本化は「stockStock が同等以上を書けるようになってから writer を切り替える」段階を踏む |

## 確定した前提（2026-09-11 追記・本文の U1/U2 を解決）

| # | 論点 | **確定値** | 出典 |
|---|---|---|---|
| U1 | Workers プラン | **Paid** | ユーザー確認 |
| U2 | D1 `kabulab-cf` の現使用量 | **71.7 MB** / 20テーブル / リージョン **APAC** / read replication **disabled** | `wrangler d1 info kabulab-cf` |

### これにより本文から取り消される制約

本仕様の本文は Free/Paid 両にらみで書かれている。Paid 確定により次を上書きする。

- **D1 の上限は 1DB 10 GB。** 現行 71.7 MB ＋ 本設計の5年増分 約500 MB ＝ **約572 MB（10 GB の 5.7%）**。余裕がある。
- **`ir_disclosures` / `jss_raw_files` の24ヶ月保持縮退は不要。** 本文が「Free なら必須条件」としていた縮退は、保持期間を運用判断で選べる「案」に戻る。
- **移行フェーズの順序の循環が解消。** 本文の F8 が指摘した「Free なら rows read 枯渇を止めるため P7 を先頭に持つ必要があるが、P7 は P4/P5/P6 完了が前提」という循環は、Paid の 250億行/月 込み枠により発生しない。**移行は設計どおり P1 から順に進めてよい。**
- Workers の CPU 上限は 5分/request（既定30秒）、subrequest 10,000/request、リクエスト無制限。Workers Cron / Durable Objects / 長時間 CPU が最初から使える。

### 新たに判明した論点

- **D1 のリージョンは APAC**。日本からのレイテンシは良好で、read replication は現在 **disabled**。Sessions API による読み取りレプリカを使うかは、API/MCP の実測レイテンシを見てから判断する（本設計の必須要件ではない）。

### 既存同期の稼働状況（2026-09-11 実測）

置き換え対象の kabulab-cf 側は健全に稼働中。`stock-sync` 2026-09-10T23:00Z success (29m3s)、`catchup` success、`vwap-ingest` success。移行は「動いているものを段階的に差し替える」前提で正しい。

---

### 決定5・6 による本仕様への差分

本仕様の本文は「みんかぶ取得を継続し personal-only タグを維持したまま公開面を是正する」前提で書かれている。決定5・6 により次を上書きする。

- **`collectors/minkabu_yutai.py` と `jobs/yutai_monthly.py` は採用しない**（未コミットのまま破棄）。みんかぶ利用規約 第7条(18) がスクレイピングを、(19) が私的使用を超えた蓄積を、第14条1項が許諾なき公開を明文で禁じており、`personal-only` を緩める根拠が規約内に存在しないため。
- ⑨株主優待は **TDnet の適時開示（優待の新設・変更・廃止）と EDINET から構築**する。これはみんかぶ規約の適用外であり、`factual-cite` として公開面に出せる。
- 引き継げないもの: みんかぶ由来のジャンル分類、`short_summary` の LLM 解釈、楽天API由来の推定金銭価値。既存 `yutai_benefits` の該当列は**読み取り専用の資産として保全**し、新規取得はしない。
- 取込設計の ⑨月次ジョブ（みんかぶ Phase1+Phase2 で約43分）は不要になり、`yutai_monthly` の timeout マージン問題（本文 F12）は消滅する。
- kabulab-cf 側は `services/otakara-yutai/app.ts:680` の掲載文レンダリングを外す（別リポジトリのため別 PR）。


---

# 全体像と確定数値

## 全体像の要約

### 1. 二層構造（変更なし）

**R2 = 長期時系列・原本・大容量派生 / D1 = 断面・索引・イベント行**。この分離は D1 の 1DB 10GB 上限が申請でも引き上げ不可であること、および D1 の課金軸が走査行数であることから導かれる。

### 2. D1 に置いてよいかの判定基準（この設計の中心規則）

次の3条件を**すべて**満たすものだけ D1。1つでも欠けたら R2。

1. 年間増加行数が **10万行以下**
2. 索引でカバーされる述語だけで引ける
3. 1行が 2MB 未満（D1 の文字列/BLOB上限）

この基準を機械的に当てると、②日足（年95.5万行）・⑧XBRL全ファクト（年**883万行**）・⑧'需給日次（年210万行）は自動的に D1 から外れ、③財務（年2万行）・④開示（年3万行）は残る。

### 3. レビューで訂正が確定した数値（旧設計値 → 確定値）

すべて 2026-09-11 に `data/raw` を `stat -f %z` で実測、または一次データからの再計算。

| 項目 | 旧設計値 | **確定値** | 訂正の根拠 |
|---|---:|---:|---|
| ⑤原本の年産（現行8種） | 3.46 GB/年 | **4.4〜4.8 GB/年** | 年別実測: 2024年 27,786件/4.821GB、2025年 19,022件/4.428GB。旧値はバックフィルの薄い2022–2023を含む4年平均 |
| ⑤原本の年産件数 | 年7万件 | **年1.9〜2.8万件** | 70,463 は**4年総数**。年別実測は上記 |
| TDnet PDF の年産 | 年3万件 / 9.5 GB | **年11,420件 / 3.4 GB** | `ir_disclosures` 37,338行 ÷ (2023-06-05〜2026-09-10 = 3.27年)。単価は EDINET PDF 実測 12.617GB/37,205 = 339KB（2024年に限れば 279KB）を採用 |
| ⑧XBRL ファクト総数 | 3,190万 | **約3,530万**（年産 **883万**） | 150doc サンプルで平均910行/doc、600doc サンプルで1,061行/doc。後者×33,257doc を採用 |
| `jss_xbrl_elements` の行数 | 約2万行 | **7.5万〜10万行** | 150doc で distinct element **3,489** を実測。Heaps' law β = log(7677/3489)/log(4) ≈ **0.57** → 33,257doc へ外挿して約75,000。TDnet XBRL と提出者独自拡張を足すと10万超 |
| R2 5年容量 | 73.6 GB | **約100 GB** | 下表 |
| R2 月額（全 Standard） | $0.95 | **$1.35** | (100−10) × $0.015 |
| R2 月額（raw/derived を IA） | $0.69 | **$0.94** | 94GB × $0.01、残6GB は Free枠内 |
| R2 Class A（書込） | 337万/年 | **約228万/年**（月19万・Free枠の19%） | supply を **§6.3 の決定どおり半減**（日証金と JPX の `supply/` 書込を1回に統合）。旧値は半減前の 2,131,990 のまま二重計上していた |
| R2 Class B（読取） | 「未計上・要監視」 | **約262万/年**（月21.8万・Free枠の2.2%） | writer 自身の RMW GET が確定的に発生させる量。daily 109万 + supply 107万 + export 23万 + reconcile 23万 |
| D1 5年容量 | 354 MB | **約500 MB** | 索引込みの逐行見積り（下記 d1_spec） |
| `jss_raw_files` の1行 | 200 B | **650 B** | `r2_key`(65B) + `derived_key`(69B) + `sha256`(64B) の本文3列だけで198B。加えて PK の暗黙索引＋3明示索引で約200B |
| `ir_disclosures` の1行 | 500 B | **875 B** | title(日本語120B) + document_url(80B) + tags JSON(60B) + raw_sha256(64B) + 5索引(170B) |

**R2 5年容量の内訳（確定）**

| 項目 | 5年 |
|---|---:|
| `jp-stock-raw/raw/` | 74.3 GB（既存13.8 + (現行4.6 + TDnet 3.4 + EDINET拡張4.1) × 5） |
| `derived/*.parquet` | 7.4 GB |
| `derived/*.txt` | 12.3 GB |
| `vwap-data/daily/` | 1.04 GB |
| `vwap-data/intra/` | 2.0 GB（**未実測・仮**） |
| `vwap-data/margin/` | 1.2 GB |
| `jp-stock-supply/supply/` | 1.09 GB |
| `export/`（トラックAのみ） | 0.6 GB |
| **合計** | **約100 GB** |

### 4. 本仕様で確定した設計変更（レビュー指摘の反映）

| # | 変更 | 理由 |
|---|---|---|
| 1 | **R2 原本キーに `doc_id` を必須化し、`RawArtifact` に `doc_id` フィールドを追加することを前提条件とする** | 現行 `raw_filename` は `{source}_{datatype}_{scope}_{YYYYMMDD}` で、実測 `edinet_pdf_8306_20240729` に **250個**の別内容原本が衝突している。`(scope, date)` では文書を一意に指せず、`doc_id` を `_` にフォールバックすると `jss_raw_files.doc_id` が全EDINET原本で NULL になり ④⇄⑤ の結合が成立しない。なお tidy CSV のヘッダは `code,doc_id,element,...` で **doc_id は変換層に既に存在する** |
| 2 | **`export/track_b_prices.parquet` を廃止**し、`export/latest.json` に `daily/` プレフィックスのポインタのみを置く | R2 `daily/` と同一粒度・同一期間の完全な重複（R1違反）。しかも旧容量表に1行も計上されていなかった（2.9〜3.9 GB の未計上） |
| 3 | **`margin/` 既存10週の `_legacy/` へのコピーを作らない** | CF 上に同一内容を2本置く行為で、ユーザーの一次制約への直接違反。守るべきは「削除しない」ことだけで、それは `delete_object` を実装しない・expiration ルールを置かない、で達成される |
| 4 | **`margin/` rows の並び順規約を導入しない**（PDF 出現順を保持し `row_index` を付す） | 007 の `/api/margin` は `rows.find()` で最初の1行を返すため、順序を変えると重複コードで返る行が変わる。かつ `primary` の扱いが3文書で矛盾していた（「全行false」vs「普通株式で判定」）。全行 false なら規約は実質 `code,isin` 順になり PDF 順とは別物 |
| 5 | **`daily/` の payload に `instrument` を入れない**、**`margin/` の rows に `name`/`isin` を入れない** | 007 は `/api/daily` を `passthrough` で素通しし、`/api/margin` を `{week, ...row}` でスプレッドする。`instrument` は JPX data_j.xls 由来＝personal-only、ISIN と銘柄名も同様で、追加した瞬間に無認証の公開APIに出る |
| 6 | **`core_stock_annual_financials` に列を追加しない**（現状維持） | 配置表自身が「×（`jss_financials` からの派生断面）」と認めながら列を11本足して太らせていた。既存15,943行との互換のためだけに残す |
| 7 | **`otakara_stock_financials` を VIEW にしない**（実テーブルのまま） | `src/cron/monthly.ts:125` が `.insert(otakaraSchema.stockFinancials)` を実行しており、VIEW では INSERT が例外になる。物理重複は1,645行/約0.5MB で、得るものより壊すリスクが大きい |
| 8 | **`jss_index_symbols` の slug に日経VI を追加**（計7） | 旧設計の6 slug では `finmath_daily_ohlcv`（7シンボル）の入力が1つ欠ける |
| 9 | **後退禁止ガードを「配列型の全置換 PUT は要素数が減ったら拒否」へ汎用化** | 旧ガードは `bars` の本数と `first_date` しか見ておらず、`weeks.json`（007 の `/api/margin` の唯一の入口）が空配列で上書きされると既存10週が画面上消える |

### 5. 本仕様で**確定できなかった**論点（勝手に決めていない）

| # | 論点 | 状態 | 影響 |
|---|---|---|---|
| U1 | **Workers プランが Free か Paid か** | **未確定** | D1 5年 約500MB は Free の 1DB 500MB を**超える**。Free なら `ir_disclosures` / `jss_raw_files` の24ヶ月保持縮退が「案」ではなく**必須条件**になる（縮退後 約270〜280MB / 54〜56%）。Paid（10GB）なら 5.0% で問題なし |
| U2 | **D1 `kabulab-cf` と R2 の現使用量** | **未実測** | U1 の判断そのものが現使用量なしには下せない。`wrangler d1 info` の `database_size` で取得可能 |
| U3 | **④開示メタの正本の所在** | **矛盾が未解消** | 配置表は「④ = D1 `ir_disclosures` ○正本」と断言する一方、取込層は「第6波まで `ir_disclosures` に書かない」と決めている。本仕様は配置表側を「**条件付き正本**」と明記するに留め、どちらに寄せるかは決めない |
| U4 | R2 `intra/` の実サイズ | **未実測**（仮 2 GB） | 5年容量の 2% |
| U5 | EDINET 追加種別（180/190/220/230/240–310）の年間件数 | **すべて推定** | `raw/` の年産 +4.1 GB/年 の根拠が未実測 |
| U6 | 日証金 CSV の実ヘッダ文字列 | **未突合** | `supply/{code}.json` の `series.jsf_zandaka` のフィールド名が確定できない |
| U7 | `margin/` の rows 並び順を変えたとき `/api/margin` の返り行が変わるか | **未検証** | 変更4（並び順規約を入れない）は「検証していないので変えない」という保守的判断。重複6コード（2593/5076/7550/9201/9202/9434）での実測突合が済めば再検討可 |
| U8 | `rawstore.save_raw` が3つ目の別内容で `ValueError` を投げる実装なのに、実測で250変種が存在する理由 | **要確認** | 別経路の書込が存在する可能性。変更1（doc_id 必須化）の実装前に調査が要る |
| U9 | R2 S3 API の条件付き PUT（`If-None-Match`） | **未確認** | 設計は依存させず D1 の sha256 照合で重複 PUT を避けている（実害なし） |
| U10 | D1 FTS5 のトークナイザ（unicode61 / trigram / icu） | **未確認** | 日本語全文検索は端末B の PGroonga に置く設計なので結論に影響しない |

---

# 配置表

## データ→格納先の対応表

凡例: `⑧` は確定文書で「XBRL全ファクト」と「需給」に重複して振られているため、後者を **`⑧'`** と表記する。年間量はすべて本タスクで再計算した確定値。

| # | データ | 粒度 | 年間量（確定値） | 格納先 | 形式 | 正本か | ライセンス区分 |
|---|---|---|---:|---|---|---|---|
| **①** | 銘柄マスタ | 銘柄 | 4,445行（増分ほぼ0） | **D1** `core_stocks`（列追加） | 行 | **○正本** | EDINETコードリスト由来列=commercial-ok ／ JPX data_j.xls 由来列（`market`/`sector33`/`sector17`/`instrument_type`）=**personal-only**（列単位で混在） |
| **②** | 株価 日足OHLCV（10年） | 銘柄×営業日 | 約95.5万行 / +0.07 GB | **R2** `vwap-data/daily/{code}.json` | JSON（per-code） | **○正本** | personal-only（yfinance） |
| **②** | 株価 指数・為替・先物 | シンボル×営業日 | **7シンボル**×245 | **R2** `vwap-data/index/{slug}.json` | JSON（per-slug） | **○正本** | personal-only |
| **②** | 株価 5分足 | 銘柄×5分 | （**未実測**・仮2 GB/5年） | **R2** `vwap-data/intra/{code}.json` | JSON（per-code） | ○正本 | personal-only |
| **②** | テクニカル 断面（最新1行） | 銘柄 | 4,445行 | **D1** `core_stock_financials`（列追加） | 行 | **○正本** | personal-only（`licensing.inherit` で継承） |
| **②** | テクニカル **履歴** | 銘柄×営業日 | 約95.5万行 | **どこにも置かない** | — | ×（日足から決定的に再計算） | — |
| **②** | バリュエーション断面（PER/PBR/配当利回り/時価総額/EPS/BPS/ROE/ROA/営業利益率） | 銘柄 | 4,445行 | **D1** `core_stock_financials` | 行 | **○正本** | personal-only |
| **③** | 財務サマリ | 銘柄×決算期×開示種別 | 約2万行 | **D1** `jss_financials` | 行 | **○正本** | commercial-ok（EDINET）／ factual-cite（TDnet短信） |
| **③** | 年次サマリ | 銘柄×年度（最新10期） | 44,450行 | **D1** `core_stock_annual_financials`（**列追加しない=現状維持**） | 行 | ×（`jss_financials` からの派生断面） | 同上 |
| **④** | 開示メタ | 開示1件 | 約3万行 | **D1** `ir_disclosures`（列追加・`doc_id` UNIQUE 併設） | 行 | **△条件付き正本**（下記注1） | factual-cite（TDnet）／ commercial-ok（EDINET） |
| **⑤** | 原本ファイル（本体） | 取得単位1件 | **年1.9〜2.8万件 / 4.4〜4.8 GB**（現行8種）<br>＋TDnet PDF 年11,420件 / 3.4 GB<br>＋EDINET拡張 年約1.3万件 / 4.1 GB（**推定**） | **R2** `jp-stock-raw/raw/{source}/{datatype}/{yyyy}/{date}/{scope}/{doc_id}/{sha16}.{ext}` | 生バイト（無加工） | **○正本** | ソース別に混在（`jss_raw_files.license_tag` で判定） |
| **⑤** | 原本 派生（tidy Parquet） | 取得単位1件 | 約0.67 GB/年（実測 2.677GB / 4年） | **R2** `jp-stock-raw/derived/….parquet` | Parquet | ×（原本から決定的に再生成可） | 原本を継承 |
| **⑤** | 原本 派生（PDFテキスト） | 取得単位1件 | 約1.14 GB/年（実測 4.575GB / 4年） | **R2** `jp-stock-raw/derived/….txt` | txt | × | 原本を継承 |
| **⑤** | 原本 派生（tidy CSV） | — | 実測 9.021 GB（Parquet 2.677 GB と同一内容） | **R2 に置かない**（ローカル `data/raw` のみ） | — | × | — |
| **⑤** | 原本 索引 | 取得単位1件 | 約6.3万行/年 | **D1** `jss_raw_files` | 行 | **○正本（所在の）** | メタのみ |
| **⑤** | 原本 yfinance日足バッチCSV | 日次 | 約29.8 GB/年（**推定**） | **CF に置かない**（ローカルのみ） | — | × | personal-only |
| **⑥** | 時系列エクスポート（トラックA=財務・開示） | データセット | 2ファイル/週・180日保持=26世代 | **R2** `jp-stock-raw/export/{yyyy-mm-dd}/track_a_{financials,disclosures}.parquet` | Parquet | ×（再生成物） | commercial-ok / factual-cite |
| **⑥** | 時系列エクスポート（トラックB=株価） | — | — | **R2 に置かない**（`export/latest.json` に `daily/` へのポインタのみ） | — | × | 下記注2 |
| **⑦** | 収集ジョブログ | ジョブ実行1回 | 約3,000行 | **D1** `jss_job_runs`（18ヶ月保持） | 行 | **○正本** | N/A |
| **⑧** | XBRL全ファクト | doc×element×context | **約883万行** / 0.67 GB | **R2** `jp-stock-raw/derived/….parquet`（本体） | Parquet | **○正本** | commercial-ok（EDINET）／ factual-cite（TDnet） |
| **⑧** | XBRL ファクトの所在索引 | 書類1件 | 約8,300行/年 | **D1** `jss_xbrl_documents` | 行 | ○正本（所在の） | メタのみ |
| **⑧** | XBRL 勘定科目の語彙表 | element | **7.5万〜10万行**（累積・増分は逓減） | **D1** `jss_xbrl_elements` | 行 | ○正本（語彙の） | メタのみ |
| **⑧** | XBRL 日本語全文索引 | — | — | **端末B PostgreSQL + PGroonga**（CF には置かない） | 索引 | **○正本** | 内部限定 |
| **⑧'** | 需給 JPX信用残 | 銘柄×ISIN×基準日 | 約104万行 | **R2** `jp-stock-supply/supply/{code}.json` の `series.jpx_margin` | JSON（per-code） | **○正本** | personal-only |
| **⑧'** | 需給 JPX 互換シム | 基準日 | 245件 / 0.24 GB | **R2** `vwap-data/margin/{YYYY-MM-DD}.json` + `weeks.json` | JSON（per-date） | ×（**期限付き互換シム**・R5例外1） | personal-only |
| **⑧'** | 需給 日証金 zandaka | 銘柄×取引所区分×申込日 | 約107万行 | **R2** `jp-stock-supply/supply/{code}.json` の `series.jsf_zandaka` | JSON | **○正本** | personal-only（「第三者の利用に供することを固く禁じます」） |
| **⑧'** | 需給 日証金 shina | 銘柄×日（約1,030行/日） | 約25万行 | 同上 `series.jsf_shina` | JSON | **○正本** | personal-only |
| **⑧'** | 需給 日証金 meigara | 銘柄（貸借銘柄区分・変化が遅い） | 4,333行＋変更のみ | 同上 `attrs.jsf_class` + 変更履歴 | JSON | **○正本** | personal-only |
| **⑧'** | 需給 日証金 seigenichiran | 銘柄×措置期間 | 変更のみ | 同上 `attrs.restrictions` | JSON | **○正本** | personal-only |
| **⑧'** | 需給 断面 | 銘柄×データ種別 | 13,200行（増えない） | **D1** `jss_supply_latest` | 行 | **○正本（断面のみ）** | personal-only |
| **⑨** | 株主優待 | 銘柄×優待品×権利月 | 8,314行 | **D1** `yutai_benefits`（列追加・`record_months` は行展開） | 行 | **○正本** | **personal-only** |
| **⑨** | 優待 原本 | 月次全件JSON 1件 | 12件 | **R2** `jp-stock-raw/raw/minkabu/yutai_monthly/…` | JSON | **○正本** | personal-only |

### 注

**注1: ④の正本の所在は未解消（U3）**
配置表は D1 `ir_disclosures` を正本とするが、取込層の設計は「`tags`/`primary_tag` が NOT NULL で stockStock が供給できないため第6波まで書かない」と決めている。両者は矛盾しており、本仕様はどちらにも寄せず「**条件付き正本**」と表記する。条件＝(a) `doc_id` バックフィル完了、(b) `tags`/`primary_tag`/`pdf_sentiment*` を別 writer が埋める列単位排他の成立。条件が満たされるまで ④の実効的な正本は R2 原本 + `jss_raw_files` にある。

**注2: ⑥トラックBを置かない理由**
`export_weekly` のトラックB（`prices_all_*.parquet`）は 5年×4,445銘柄×245営業日 = 5.45M行で、R2 `daily/` と**同一粒度・同一期間**。重複禁止規則 R1 に直接違反し、かつ旧容量表に1行も計上されていなかった（2.9〜3.9 GB の未計上）。代わりに `export/latest.json` に `daily/` のキー一覧と各 `last_date` を書く。

**注3: 母集団は 3,818/3,900 ではなく 4,445**
R2 `daily/` の 4,444ファイルの母集団は `core_stocks`(3,818) ではなく `kabulab-cf/public/vwap-analysis/data/stocks.json`（プライム1,558/スタンダード1,575/グロース595/ETF・ETN 466/PRO 181/REIT等63/外国株5/出資証券2）。`core_stocks.instrument_type` の追加は、stockStock の母集団で `daily/` を上書きして ETF 1321 等を更新停止させる事故を**型で防ぐ**ためのもの。ただし `instrument_type` は JPX 由来＝personal-only なので、**R2 `daily/` の payload には出さない**（r2_spec 参照）。

**注4: 銘柄マスタの物理コピーは CF 上に依然として複数残る**
(1) D1 `core_stocks`、(2) `kabulab-cf/public/vwap-analysis/data/stocks.json`（ビルド生成物に降格しても Static Assets 上の物理コピーは残る）、(3) kabuMCP の Static Assets `entities` 27シャード、(4) R2 `daily/{code}.json` のキー空間そのもの。本仕様が解消を確定できるのは (1)(2) の**生成元の統一**までで、(3) は別サービス・別デプロイのため**要判断**。新たな検索索引を作る場合は (3) の再利用を優先し、同機能のシャード索引を2本目として作らないこと。

**注5: `⑧'需給` と `②日足` を混ぜない**
日証金（貸借取引の融資・貸株残）と JPX（信用取引残高）は**別データ**であり、両方を `supply/{code}.json` の `series` に持っても重複ではない。一方、日証金を `vwap-data` に置くことは禁じる（源泉もライセンスも別で、`vwap-data` は公開 Worker がバインドしている）。

---

# R2 キー設計

## R2 キー設計（確定仕様）

### 1. バケットは3本（既存1 + 新規2）

| バケット | 新規/既存 | 役割 | 公開Workerのバインド | ライフサイクル |
|---|---|---|---|---|
| `vwap-data` | **既存・改名不可** | 公開Worker(007)と外部読者2つが読む時系列 | **あり**（現状どおり） | `margin/` のみ90日で IA。**expiration ルールを設定しない** |
| `jp-stock-raw` | **新規** | ⑤原本(immutable) + 派生(Parquet/txt) + ⑥エクスポート | **バインドしない** | `raw/`・`derived/` を90日で IA。`export/` を180日で削除。未完了 multipart を7日で abort |
| `jp-stock-supply` | **新規** | ⑧'需給の per-code 時系列（日証金・JPX） | **バインドしない** | ルール無し（日次更新のため IA は不利） |

**`vwap-data` を改名・移設しない理由**: 外部読者2つがバケット名をハードコードしている（`株ラボ-Youtube/src/kabulab_yt/data/r2.py` の `bucket="vwap-data"`、`株ラボ-新高値ブレイク検証` が `_bootstrap.py` 経由で同じ層を使う）。R2 にバケット rename は無く、移設は全件コピー＋外部2リポジトリの改修を伴う。得るものが名前だけなので行わない。

**`jp-stock-supply` を分ける理由**: 日証金は「第三者の利用に供することを固く禁じます」と明文があり、公開 Worker がバインドしているバケットに置くと事故の距離がゼロになる。バケット分離＝**R2 API トークンのスコープ分離**（R2 のトークンはバケット単位でスコープでき、プレフィックス単位ではできない）。

---

### 2. 全プレフィックス共通の不変条件

#### 2.1 後退禁止ガード（PUT 前に必ず検証。1つでも満たさなければ **PUT せず失敗として記録**）

mutable キー（`daily/` `index/` `intra/` `supply/` `margin/` `weeks.json` `margin/index.json` `export/latest.json`）への全置換 PUT に適用する。

1. 既存オブジェクトの GET が **404 以外のエラー**で失敗した → PUT しない（「無かったこと」にしない）
2. **配列型フィールドの要素数が減った** → PUT しない（`bars` / `splits` / `series.*` / `weeks.json` の配列 / `margin/index.json` の `entries` すべてに適用）
3. **既存の全要素が新配列に含まれない** → PUT しない（要素数が同じでも中身が入れ替わっていたら拒否。`weeks.json` のように要素数が少ないものは集合として検証する）
4. `new.first_date > old.first_date` → PUT しない
5. 読み戻した payload の `writer` が自分以外 → PUT しない
6. 契約必須キー（§2.2）が1つでも欠けた → PUT しない

> **ガード2・3を配列型全般へ汎用化した理由**: 旧設計のガードは `bars` の本数と `first_date` しか見ていなかった。`margin/weeks.json` は 007 の `/api/margin` の**唯一の入口**で（`services/vwap-analysis/app.ts:86-88` は `weeks.json` が無ければ即 `weeks:[]` を返す）、これを空配列で上書きすると R2 のオブジェクト自体は残っていても画面上はデータが消えたのと同じになる。既存10週のうち 2026-06-12〜07-31 は JPX から再取得不能。

#### 2.2 契約キー（削除・改名が禁止されたキー）

`_schema/{prefix}.json` に機械可読で置き、PUT 前に JSON Schema で検証する。

| プレフィックス | 削除・改名が禁止されたキー |
|---|---|
| `daily/` | `code` / `updated` / `bars[].date,o,h,l,c,v,adj` / `splits[].date,ratio` |
| `intra/` | `code` / `updated` / `bars[].ts,o,h,l,c,v` |
| `margin/{date}.json` | `week`（トップレベル・文字列） / `rows[].code,sell,sell_chg,buy,buy_chg` |
| `margin/weeks.json` | **ソート済み文字列の素の配列**（オブジェクトにしない） |
| `index/` | `slug` / `updated` / `bars[].date,o,h,l,c,v,adj` |
| `supply/` | （新規のため契約なし。`schema` バージョンで管理） |

出典: `kabulab-cf/services/vwap-analysis/app.js:279,286,321,385`、`app.ts:62-69,86-95`、`株ラボ-Youtube/src/kabulab_yt/data/{prices,intraday,margin}.py`。

#### 2.3 削除の禁止

`cloud_store/r2.py` に **`delete_object` を実装しない**。これにより既存10週（2026-06-12〜、07-03・07-10 は恒久欠測）が物理的に消せなくなる。`vwap-data/margin/` に expiration ルールを設定しないことと併せて二重に守る。

> **`_legacy/` へのバックアップコピーは作らない**。旧設計は `jp-stock-raw/raw/jpx/margin_weekly/_legacy/{week}/…` へのコピーを新設していたが、これは同一内容を CF 上に2本置く行為で、ユーザーの一次制約（同じデータを Cloudflare 上に二重に置かない）への直接違反。守るべきは「削除しない」ことだけで、それは上記2点で達成される。CF 外（ローカル `data/raw` か端末B）へのバックアップは制約の対象外なので、必要ならそちらに置く。

#### 2.4 投入経路とレート制約

- 投入は **S3互換API**（`@aws-sdk/client-s3` 相当 / boto3 / aiobotocore）。**REST API は 1,200 req/5分**の制限があり、日次 4,445 PUT には使えない。
- **同一キーへの並行書込は 1/秒**（超過は HTTP 429）。ただし `daily/` は 4,445キー、`supply/` は 4,351キーに分散するため、**2本の writer が別銘柄を処理していれば 429 は出ない**。したがって 429 を writer 二重稼働の検知手段として当てにしてはならない（§2.5）。
- オブジェクトキー上限 **1,024 バイト**。最長キーでも約70バイト（§3.2）。

#### 2.5 writer の宣言（429 に依存しない検知）

全ての mutable JSON のトップレベルに `"writer"` キーを置き、`_schema/{prefix}.json` にプレフィックス単位の writer 名を書く。PUT 直前に GET した payload の `writer` が自分と一致することを検証し、不一致なら**異常終了**する（§2.1 ガード5）。

> R2 の 429 は同一キー競合でしか出ず、キーが分散する `daily/` `supply/` では実質的に発火しない。R2 と D1 の防御強度に差があるという前提は誤りで、**どちらもコード規律での検知**になる。最も確実なのは writer ごとに R2 トークンを分け、切替時に旧トークンを revoke して物理的に書けなくすること。

---

### 3. `jp-stock-raw`（⑤原本 — immutable）

R2 にオブジェクトバージョニングが無い（API未実装）ことを前提に、**キーに SHA256 を含めて物理的に上書きが起こらない**設計にする。

#### 3.1 原本キー

```
raw/{source}/{datatype}/{yyyy}/{yyyy-mm-dd}/{scope}/{doc_id}/{sha256_16}.{ext}
```

| セグメント | 値域 | 説明 |
|---|---|---|
| `source` | `edinet` `tdnet` `jpx` `jsf` `minkabu` `yfinance` | `models.Source` を小文字化 |
| `datatype` | `xbrl` `csv` `pdf` `documents_list` `codelist` `tdnet_list` `tdnet_pdf` `tdnet_xbrl` `splits` `valuation` `margin_weekly` `margin_daily` `zandaka` `shina` `meigara` `seigen` `yutai_monthly` | 現行 `rawstore.raw_filename` の datatype 語彙を拡張 |
| `yyyy` | `2026` | `data_date` の年（プレフィックス列挙とライフサイクル適用の単位） |
| `yyyy-mm-dd` | `2026-09-10` | `data_date`。不明時は `nodate` |
| `scope` | 銘柄コード or `ALL` | 現行 `RawArtifact.scope` |
| `doc_id` | EDINET docID / TDnet PDFファイル名 / 無ければ `_` | **④開示との結合キー。§3.2 を必ず読むこと** |
| `sha256_16` | SHA256 先頭16hex | 内容識別子 |

実例:
```
raw/edinet/csv/2026/2026-06-22/7203/S100XXXX/9f2c1ab4d7e30516.zip
raw/edinet/pdf/2024/2024-07-29/8306/S100U1XX/3a71c0de99b41f27.pdf
raw/tdnet/tdnet_pdf/2026/2026-09-10/6758/140120260910567733/b41e77c209a3d6f8.pdf
raw/jpx/margin_daily/2026/2026-09-28/ALL/_/1c99ae40b7f2033d.pdf
raw/jsf/zandaka/2026/2026-09-10/ALL/_/70bd12c4a8e5f931.csv
raw/minkabu/yutai_monthly/2026/2026-09-01/ALL/_/5e8d3f20a1b7c964.json
```

最長キー（`raw/edinet/documents_list/2026/2026-09-10/ALL/_/xxxxxxxxxxxxxxxx.json`）で **69 バイト**。上限 1,024 に対して十分な余裕がある。

#### 3.2 `doc_id` セグメントは必須（**前提条件つき**）

**現行の `RawArtifact` に `doc_id` フィールドは存在しない**（`source` / `datatype` / `scope` / `data_date` / `fetched_at` / `url` / `local_path` / `sha256` / `size_bytes` / `license_tag` / `converted_paths` / `convert_status` / `notion_page_id` のみ）。したがってこのキー設計は **`RawArtifact` への `doc_id: str | None` の追加を前提条件とする**。

**なぜ `_` へのフォールバックでは駄目か（実測）**:

`rawstore.raw_filename` は `{source}_{datatype}_{scope}_{YYYYMMDD}.{ext}` で、`(scope, data_date)` だけでは文書を一意に指せない。実測で `data/raw` の衝突を数えると:

| プレフィックス | 別内容の原本数 |
|---|---:|
| `edinet_pdf_8306_20240729` | **250** |
| `edinet_pdf_8306_20231016` | 105 |
| `edinet_pdf_8306_20240318` | 37 |
| `edinet_pdf_8306_20250630` | 32 |
| `edinet_pdf_8306_20220920` | 28 |

`doc_id` を `_` に倒すと、これら250件が `raw/edinet/pdf/2024/2024-07-29/8306/_/{sha16}.pdf` に並び、`jss_raw_files.doc_id` が全 EDINET 原本で NULL になる。その結果 **④開示⇄⑤原本の結合が成立しない**。

**`doc_id` は変換層には既に存在する**。tidy CSV の実ヘッダは `code,doc_id,element,context_ref,period_start,period_end,instant_date,consolidated,unit,value` で、`doc_id` 列を持つ。つまり EDINET docID は収集経路の中に存在しており、`RawArtifact` まで持ち上げるだけでよい。

**`doc_id` に `_` を使ってよい datatype**（`{source}_{datatype}_{scope}_{date}` で一意になるもの）:
`documents_list` / `codelist` / `tdnet_list` / `margin_weekly` / `margin_daily` / `zandaka` / `shina` / `meigara` / `seigen` / `yutai_monthly` / `valuation`

**`doc_id` が必須の datatype**: `xbrl` / `csv` / `pdf` / `tdnet_pdf` / `tdnet_xbrl`

> **未確認（U8）**: `rawstore.save_raw` は primary + sha8 の2スロットしか持たず、3つ目の別内容で `ValueError` を投げる実装に見える（`rawstore.py:105` 付近）にもかかわらず、実測で250変種が存在する。別経路の書込がある可能性があり、`doc_id` 追加の実装前に調査が要る。

#### 3.3 日中に繰り返し取得する datatype の扱い

`tdnet_list` は平日 9:00–19:00 の毎時取得（11回/日）で、同一 `data_date` について**内容が少しずつ異なるスナップショット**が最大11個生まれる。キーに sha256 が入っているので衝突はしないが、**旧設計の「同一内容の再取得 → 同一キー」という説明はこのケースを解いていない**。

確定仕様:
- **11個すべてを保持する**（新しい開示が降ってきた瞬間のスナップショットは、それぞれ別の事実）。sha256 が同じなら PUT はスキップされるので、実際の個数は「新規開示が現れた回数」に自動で収束する（≤11、通常はもっと少ない）。
- 容量影響: JSON の実測平均は 292 KB（0.236 GB / 807件）。最悪ケースで 11 × 245 × 292 KB = **0.79 GB/年**。`raw/` 年産 4.4〜4.8 GB に対して 16〜18%。許容する。
- D1 `jss_raw_files` に同日複数行が並ぶため、`(source, datatype, data_date)` の最新1件を引けるよう `idx_jss_raw_src_type` を張る（d1_spec 参照）。

#### 3.4 派生キー

```
derived/{source}/{datatype}/{yyyy}/{yyyy-mm-dd}/{scope}/{doc_id}/{sha256_16}.parquet
derived/{source}/{datatype}/{yyyy}/{yyyy-mm-dd}/{scope}/{doc_id}/{sha256_16}.txt
```

- `sha256_16` は **原本の** SHA256 を使う（派生は原本から決定的に再生成できるので、原本と同じ識別子で紐づけるのが正しい）。
- **CSV は R2 に置かない**。tidy CSV と tidy Parquet は同一内容で、実測 **CSV 9.021 GB に対し Parquet 2.677 GB**（3.4分の1）。CSV はローカル `data/raw` にのみ残す。
- Parquet の列は `convert/xbrl_to_csv.py` の `TIDY_COLUMNS`（実ヘッダで確認済み: `code, doc_id, element, context_ref, period_start, period_end, instant_date, consolidated, unit, value`）に `is_text_block BOOLEAN` を1列だけ追加する。値は一切変更しない。
- **この Parquet が ⑧XBRL全ファクトの格納そのもの**。専用バケット・専用プレフィックスは作らない。1ファイル平均 **359 KB**（実測）、平均 **910〜1,061 行/doc**。

#### 3.5 ⑥エクスポート

```
export/{yyyy-mm-dd}/track_a_financials.parquet
export/{yyyy-mm-dd}/track_a_disclosures.parquet
export/latest.json
```

**`track_b_prices.parquet` は置かない**（重複禁止規則 R1 違反。r2_spec §3.5 注）。代わりに `export/latest.json` に `daily/` へのポインタを書く:

```json
{
  "schema": 1,
  "generated_at": "2026-09-13T00:12:00.000Z",
  "track_a": [
    {"key":"export/2026-09-13/track_a_financials.parquet","bytes":18432000,"rows":100000,"license":"commercial-ok"},
    {"key":"export/2026-09-13/track_a_disclosures.parquet","bytes":24100000,"rows":190000,"license":"factual-cite"}
  ],
  "track_b": {
    "kind": "pointer",
    "bucket": "vwap-data",
    "prefix": "daily/",
    "license": "personal-only",
    "note": "株価時系列は per-code JSON が正本。Parquet 版は生成しない（R1）。",
    "objects": [{"code":"1301","key":"daily/1301.json","last_date":"2026-09-11"}]
  }
}
```

保持: `export/` は180日で削除（26世代）。トラックAのみなので実容量は **約0.6 GB**（旧設計はトラックB込みで 2.9〜3.9 GB を容量表に計上していなかった）。

#### 3.6 ⑤原本のうち R2 に置かないもの

**判定基準: 「再取得不能なものだけ CF に置く」**

| ソース | 再取得可能性 | CF格納 |
|---|---|---|
| TDnet PDF / 一覧JSON | 原本が約31日で purge | **必須** |
| JPX 週末・日次残高PDF | 一覧に直近約5週のみ | **必須** |
| 日証金 zandaka/shina/meigara/seigen | 当日分のみ・過去は残らない | **必須** |
| EDINET XBRL/CSV/PDF | APIに残るが期限あり・件数が多く再取得コストが高い | 置く |
| みんかぶ優待JSON | 再取得可だが規約上の取得回数を増やしたくない | 置く（内部のみ） |
| yfinance splits / valuation | 小容量・再取得可だが来歴保持の価値が高い | 置く |
| **yfinance 日足バッチ CSV** | Yahoo からいつでも再取得可 | **置かない** |

yfinance 日足バッチを外す理由は容量。4,445銘柄×490営業日＝約218万行、1行約60バイトで**1回あたり約0.12 GB、年245回で約29.8 GB/年**（**推定**。手元の実測は3銘柄 smoke run の 33KB のみ）。前日との差分は 0.4% 以下で、構造化済みの `daily/{code}.json` が同じ内容を10年分 1.04 GB で保持している。

---

### 4. `vwap-data`（既存プレフィックスは1バイトも変えない）

#### 4.1 `daily/{code}.json` — 日足10年（②の正本）

既存フィールドは全部そのまま。**追加は任意キーのみ**（既存読者は無視する）。

```json
{
  "code": "7203",
  "updated": "2026-09-11T08:12:33.456Z",
  "bars": [
    {"date":"2026-09-10","o":2895.5,"h":2910.0,"l":2880.0,"c":2905.0,"v":12345600,"adj":2905.0,
     "adj_source":"yfinance-adjclose"}
  ],
  "splits": [{"date":"2021-09-30","ratio":5}],

  "schema": 2,
  "source": "yfinance",
  "license": "personal-only",
  "first_date": "2016-09-12",
  "last_date": "2026-09-10",
  "writer": "stockStock/prices_daily"
}
```

**追加キーの意味**
- `adj_source` — 既存実装は `adjclose` が無いとき `c` と同値を `adj` に書いており、真の調整済みか代用かが区別できない。`"yfinance-adjclose"` / `"close-fallback"` の**2値のみ**で明示する（推定しない）。
- `first_date` / `last_date` — 鮮度判定で `bars` 全走査をさせない。
- `writer` — 唯一の writer を宣言し、二重書込を検知可能にする（§2.5）。

**`instrument` を入れない（旧設計からの変更）**
旧設計は `instrument` を `equity|etf|etn|reit|pro|foreign|preferred` で持たせ、母集団事故を型で防ぐとしていた。しかし `instrument` の供給元は `core_stocks.instrument_type` ＝ JPX data_j.xls 由来で **personal-only** に分類される列であり、007 の `/api/daily` は `passthrough(o.body, 3600)` で本文を素通しするため（`services/vwap-analysis/app.ts:62-69`）、payload に入れた瞬間に無認証の公開 API に出る。

代替の事故防止策: **PUT 前に「対象コードが `core_stocks` に存在し `instrument_type` が非 NULL である」ことを writer 側で検査する**。さらに「対象コード集合 ⊇ R2 に既に存在する `daily/` のキー集合」を書込前に検査し、満たさなければ1件も書かずに異常終了する。これで ETF 1321 等の更新停止を防げる。

> 同じ理由で `license` と `writer` と `schema` も公開される。これらは personal-only の**分類を示す文字列**であって personal-only の**データそのものではない**ため許容するが、007 側を許可キーのホワイトリストで再構成するレスポンダに変える選択肢も残る（本仕様では決めない）。

**投入経路**: `prices` テーブル経由にしてはならない。`jobs/prices_daily.py` は最新1本のスナップショットだけを書くため過去バーが遡って入らず、`local_store/schema.py` の `prices` には `adj` 列も `splits` も無い。yfinance の生 DataFrame（`Open/High/Low/Close/Adj Close/Volume`）を直接流す。

#### 4.2 `index/{slug}.json` — 指数・為替・先物の日足（新規）

`daily/` と同一スキーマ。`code` の代わりに `slug`。`daily/{code}` の code 検証が `/^[0-9A-Za-z]{4}$/` のため `^N225` を同居させられないので別プレフィックスにする。

**slug は7つ**（旧設計は6つで、`finmath_daily_ohlcv` の7シンボル目が欠けていた）:

| slug | 対象 | 用途 |
|---|---|---|
| `n225` | 日経平均 | `swing_market_context` / `finmath` |
| `topix` | TOPIX | `finmath` |
| `vix` | VIX | `swing_market_context` / `finmath` |
| `gspc` | S&P500 | `swing_market_context` / `finmath` |
| `usdjpy` | USD/JPY | `finmath` |
| `niy_f` | 日経225先物（CME） | `swing_market_context` |
| **`nkvi`** | **日経VI** | **`swing_market_context`**（旧設計に欠落） |

Yahoo シンボルの対応は D1 `jss_index_symbols` が持つ（`nkvi` の Yahoo シンボルは**要確認**）。

#### 4.3 `intra/{code}.json` — 5分足

スキーマ `{code, updated, bars[{ts,o,h,l,c,v}]}` を**変更しない**。`adj` も `splits` も無い。保持は `KEEP_DAYS`（既定365）。

#### 4.4 `margin/{YYYY-MM-DD}.json` + `margin/weeks.json` — JPX信用残（後方互換シム）

**既存10週（2026-06-12〜）は絶対に削除しない**（2026-06-12〜07-31 は JPX から再取得不能、07-03・07-10 は恒久欠測）。

```json
{
  "week": "2026-09-28",
  "rows": [
    {"code":"1301","sell":9300,"sell_chg":-400,"buy":152000,"buy_chg":3100,
     "row_index":0,
     "sell_nego":1200,"sell_nego_chg":0,"sell_std":8100,"sell_std_chg":-400,
     "buy_nego":22000,"buy_nego_chg":100,"buy_std":130000,"buy_std_chg":3000}
  ],
  "schema": 2,
  "kind": "daily",
  "date": "2026-09-28",
  "source": "JPX",
  "license": "personal-only",
  "row_count": 4251,
  "writer": "stockStock/supply_daily"
}
```

**不変**: `week`（トップレベル・文字列）、`rows[].code,sell,sell_chg,buy,buy_chg`。`weeks.json` は**ソート済み文字列の素の配列**のまま。

**並び順の規約は導入しない（旧設計からの変更）**

旧設計は `ORDER BY code ASC, primary DESC, isin ASC` を規約化していたが、これを**採用しない**。理由は3つ。

1. **007 は `rows.find(r => r.code === code)` で最初の1行を返す**（`services/vwap-analysis/app.ts:94-95`）。並び順を変えると重複コードで返る行が変わり、画面の数値が動く。実測で 2026-07-24週は rows 4,228 に対し distinct code 4,221、**9434 は3行あり2行は buy/sell とも0**。先頭が0行になると信用残がゼロ表示になる。
2. **`primary` の扱いが3文書で矛盾していた**。「判定できないので全行 false」とする案と「PDF 表記名が『普通株式』を含むかで決定論的に判定」とする案が併存していた。全行 false なら規約は実質 `code, isin` 順になり、PDF 出現順とは別物になる。
3. **順序を変えて現行と一致する保証が無い**（U7・未検証）。

**確定仕様**: **PDF 出現順をそのまま保持**し、各行に `row_index`（0起点の整数）を付して順序の来歴を明示する。将来、並び順を変えたくなった場合は、切替前に既存の直近週で「旧パーサ出力の `find()` 結果 == 新パーサ出力の `find()` 結果」を全コードで検証し、重複6コード（2593 / 5076 / 7550 / 9201 / 9202 / 9434）が完全一致した場合にのみ採用する。

**`name` と `isin` を rows に入れない（旧設計からの変更）**

007 は `{ week: w, ...row }` で rows の中身を丸ごとスプレッドして無認証の公開 API に返す（`app.ts:94-95`）。`isin` と JPX PDF の銘柄表記名を rows に入れると、それらがそのまま公開面に出る。ISIN は主キーとして必要だが、**per-code 側（`jp-stock-supply/supply/{code}.json` の `series.jpx_margin`）にだけ持たせる**。銘柄名は ①マスタから引けるので rows には不要。

> なお、この変更後も payload は 5 フィールド → 13 フィールドに増え、`n=260` 指定時のレスポンスは約2.6倍になる。制度・一般の内訳8列は後方互換拡張として許容するが、007 側の帯域増は認識しておくこと。

**2026-09-28 の日次化への対応**: キー形式は `margin/{YYYY-MM-DD}.json` のままで、`week` に**営業日**を入れる。`weeks.json` にも営業日を追記する。意味が「週」から「基準日」に変わる事実は新設の `margin/index.json` に書く（`weeks.json` 自体は形を変えない）。

```json
{"schema":2,"entries":[
  {"date":"2026-09-25","kind":"weekly","source":"JPX","rows":4253,"key":"margin/2026-09-25.json"},
  {"date":"2026-09-28","kind":"daily","source":"JPX","rows":4251,"key":"margin/2026-09-28.json"}]}
```

**この `margin/` は正本ではなく期限付き互換シム（R5 例外1）**。廃止条件: (1) 007 の `/api/margin` を `jp-stock-supply` の per-code 読みに変更、(2) `株ラボ-Youtube/margin.py` を per-code に変更（現在は `weeks.json` を読まず `^margin/(\d{4}-\d{2}-\d{2})\.json$` でキー名を直接列挙している）。両方が済んだ時点で新規書込を停止する（既存オブジェクトは削除しない）。

#### 4.5 `_schema/{prefix}.json` — 契約の自己記述

各プレフィックスの `schema_version` / 必須キー / 唯一の writer / 読者一覧を機械可読で置く。PUT 前の検証（§2.1 ガード6）の正本。

---

### 5. `jp-stock-supply`（⑧'需給の per-code 正本）

```
supply/{code}.json
supply/_index.json
```

per-code を選ぶ理由: 主用途が「銘柄詳細で信用残推移を出す」であり、既存 `daily/{code}.json` と同じアクセスパターン。per-date のまま60営業日を引くと 0.95MB×60＝57MB の転送になる。

```json
{
  "schema": 1,
  "code": "7203",
  "updated": "2026-09-11T02:30:00.000Z",
  "license": "personal-only",
  "writer": "stockStock/supply_daily",
  "attrs": {
    "isin": "JP3633400001",
    "jsf_class": "貸借銘柄",
    "jsf_class_since": "2024-04-01",
    "restrictions": [{"from":"2026-08-12","to":null,"kind":"増担保","note":"（日証金の生表記のまま）"}]
  },
  "series": {
    "jsf_zandaka": [
      {"d":"2026-09-10","exch":"東証およびＰＴＳ",
       "loan_new":120000,"loan_rep":98000,"loan_bal":3450000,
       "stock_new":41000,"stock_rep":52000,"stock_bal":880000,
       "loan_bal_amt":9990000000,"stock_bal_amt":2550000000,
       "turn_days":8.4}
    ],
    "jsf_shina": [
      {"d":"2026-09-10","hibu":null,"rank":1,"excess":0}
    ],
    "jpx_margin": [
      {"d":"2026-09-28","kind":"daily","isin":"JP3633400001","row_index":12,
       "sell":9300,"sell_chg":-400,"buy":152000,"buy_chg":3100,
       "sell_nego":1200,"sell_nego_chg":0,"sell_std":8100,"sell_std_chg":-400,
       "buy_nego":22000,"buy_nego_chg":100,"buy_std":130000,"buy_std_chg":3000}
    ]
  }
}
```

**設計上の確定事項**
- `series` のキーで源泉を分ける。**日証金（貸借取引の融資・貸株残）と JPX（信用取引残高）は別データであり、両方持っても重複ではない**。
- 主キーは日証金が `(申込日, 銘柄コード, 取引所区分名)`、JPX が `(基準日, 銘柄コード, ISIN)`。同一コードに複数証券（普通株/優先株）が載るため **ISIN を落とさない**。
- `attrs.jsf_class`（`meigara.csv`）と `attrs.restrictions`（`seigenichiran.csv`）は変化が遅いので、日次で全量を積まず**最新値＋変更履歴**だけを持つ。
- 配列は `d` 昇順で保持し、追記時に既存を Map マージして全置換（§2.1 のガードを適用）。
- **マスク値・空欄を 0 に潰さない**。日証金 `shina.csv` の品貸料率・品貸日数には `*****` が実在し、`zandaka.csv` の `制度信用・買残高株数` / `制度信用・売残高株数` は実測で全行空。いずれも **`null`** にする（`0` にすると「品貸料率ゼロ」「制度信用残ゼロ」という嘘のデータが生まれる）。
- コードは `^[0-9A-Za-z]{4}$` で検証する（`meigara.csv` に `130A` が実在。4桁数字を仮定しない）。

> **未確定（U6）**: `jsf_*` の列名は日証金 CSV のヘッダに1対1で対応させる方針だが、**実ヘッダ文字列との突合が未了**。上記のフィールド名（`loan_new` 等）は暫定で、実装時に cp932 の原文ヘッダと突き合わせて確定すること。

**`_index.json`**: 銘柄数・最終日・欠測日を持つ。日証金は過去分が残らないため、取り逃した日は永久欠測になる。欠測日を明示的に記録しておく。

---

### 6. ライフサイクルルール（プレフィックス指定）

| バケット | プレフィックス | ルール |
|---|---|---|
| `jp-stock-raw` | `raw/` | 90日で Infrequent Access へ遷移。**expiration なし** |
| `jp-stock-raw` | `derived/` | 90日で Infrequent Access へ遷移。**expiration なし** |
| `jp-stock-raw` | `export/` | 180日で削除（毎週再生成される派生物） |
| `jp-stock-raw` | （全体） | 未完了 multipart upload を7日で abort |
| `vwap-data` | `margin/` | 90日で Infrequent Access へ遷移。**expiration は設定しない**（既存10週の保護） |
| `vwap-data` | `daily/` `intra/` `index/` | **ルール無し**（日次で上書きされるため IA は不利。IA は最低保存期間30日・取得課金あり） |
| `jp-stock-supply` | `supply/` | **ルール無し**（日次更新） |

R2 の lifecycle は expiration / IA遷移 / multipart abort をサポートし、プレフィックスでスコープでき、1バケット1000ルールまで。

> **未計上のコスト**: IA からの取得には `$0.01/GB` の retrieval 課金がある。`raw/` を公開ファイル配信の対象にする場合、90日超のオブジェクトへのアクセスは課金される。また **IA 遷移そのものが Class A としてカウントされるかは未確認**。

---

### 7. 操作回数（Class A / Class B）

旧設計は Class A で supply を二重計上し（`§6.3` で「日証金と JPX の R2 書込を1回にまとめて年213万→107万に半減」と決めたのに表は半減前のまま）、Class B は「未計上・要監視」としていた。確定値:

**Class A（書込）**

| 経路 | 年間 |
|---|---:|
| 原本 PUT | 63,000 |
| 派生 Parquet PUT | 33,000 |
| 派生 txt PUT | 37,000 |
| `daily/{code}.json`（4,445 × 245営業日） | 1,089,025 |
| `index/{slug}.json`（7 × 245） | 1,715 |
| `supply/{code}.json`（4,351 × 245・**日証金とJPXを1回に統合**） | 1,066,000 |
| `margin/` シム + `weeks.json` + `index.json` | 735 |
| `export/` | 260 |
| **合計** | **約228万/年 = 月約19万** |

Free枠 1,000,000/月 に対して **19%**。**Class A は $0**。

**Class B（読取）** — writer 自身が確定的に発生させる量

| 経路 | 年間 |
|---|---:|
| `daily/` の RMW GET | 1,089,025 |
| `supply/` の RMW GET | 1,066,000 |
| `export_weekly` の `daily/` GET（4,445 × 52） | 231,140 |
| `reconcile_weekly` の GET | 231,140 |
| `margin/` + `ListObjectsV2` | 3,500 |
| **合計** | **約262万/年 = 月約21.8万** |

Free枠 10,000,000/月 に対して **2.2%**。**Class B も $0**（公開 Worker のトラフィック分はこれに加算され、そちらは依然として未実測）。

**転送量（旧設計が無視していた軸）**

`daily/{code}.json` の1ファイルは **1.04 GB / 4,445 = 約160 KB**。RMW は GET 160KB + PUT 165KB を 4,445 銘柄ぶん = **2.9 GB/日**。

旧設計の所要時間見積り「8,890 ops × 0.2s ÷ 16 = 111s = 1.9分」は**1オペのレイテンシだけ**を見ており実データ量が入っていない。実効帯域から出し直すと:

| 実効帯域 | 所要 |
|---|---|
| 100 Mbps (12.5 MB/s) | 2.9 GB ÷ 12.5 MB/s = **約4分** |
| 50 Mbps (6.25 MB/s) | **約8分** |

R2 のエグレスは無料なので課金は発生しないが、**所要時間はレイテンシではなく帯域で決まる**。

---

# D1 テーブル設計

## D1 テーブル設計（確定仕様）

### 0. 前提

- **新しい D1 を作らない**。既存 `kabulab-cf`（`639c0315-0d41-4345-95ab-aec5906224ed`）にテーブル接頭辞で同居させる。D1 はクロスDB JOIN ができないため、DB を分けた瞬間に `core_stocks` と新テーブルを JOIN できなくなる。
- stockStock 由来の新規テーブルは接頭辞 **`jss_`**（既存の `core_` / `swing_` / `otakara_` / `yutai_` / `ir_` / `yuho_` / `finmath_` / `rsi_` と衝突しない）。
- **D1 に置いてよいかの判定基準**（3条件を**すべて**満たすものだけ）:
  1. 年間増加行数が **10万行以下**
  2. 索引でカバーされる述語だけで引ける
  3. 1行が 2MB 未満

この基準により ②日足（年95.5万行）・⑧XBRL全ファクト（年**883万行**）・⑧'需給日次（年210万行）は自動的に D1 から外れる。

---

### A. 既存テーブルへの列追加

#### A-1. `core_stocks`（拡張）— ①銘柄マスタ

`core_stocks.id` は integer autoIncrement のサロゲートキーで、**14個の子テーブルが `stock_id` で参照する**（多くは `onDelete: cascade`）。

> 2026-09-12 実測で訂正: 「12個」は誤りで **14個**
> (`core_stock_annual_financials` / `core_stock_financials` / `ir_disclosures` /
> `otakara_stock_financials` / `otakara_stock_scores` / `rsi_percentile` /
> `swing_daily_ohlcv` / `swing_entry_signals` / `swing_stock_indicators` /
> `swing_stock_screening` / `yuho_documents` / `yuho_order_facts` /
> `yuho_overseas_facts` / `yutai_benefits`)。加えて `jss_financials` が FK 宣言の
> 無い soft 参照を持つため、孤児検査は **15表**を対象にする
> (`cloud_store/core_stocks.CHILD_TABLES`)。

**絶対規則**
1. upsert は `INSERT … ON CONFLICT(code) DO UPDATE` のみ。**`id` を SET 句に絶対に入れない**
2. `TRUNCATE` / `DELETE` / `DROP` を発行しない
3. 対象外化は `is_active = 0` の UPDATE のみ
4. `src/cron/universe.ts:82-120` の `assertUniverseCoverage` の**4条件をそのまま移植**（JPX raw 行数下限 4,000 / 内国株式 3,000 / 既存 active 比 98% / 一括対象外化 2% 上限）。1つでも破れたら書込ゼロで異常終了

> 2026-09-12 実測で訂正: 参照先 `scripts/sync/universe.ts:155-201` は誤り。
> 同ファイルは 29 行の薄い CLI で、ガードの実体は `src/cron/universe.ts:82-120`
> （定数は :55/:57/:59/:61）。また「3段」ではなく **4条件**で、「取込ジョブ 確定仕様」節の
> `master_sync` のガード（本文中の (a)-(d) の列挙）のほうが
> 実コードと一致する。移植は
> `cloud_store/universe_guards.assert_universe_coverage` に 1:1 で入れた。
>
> **分母の取り違えに注意**: (c)(d) の分母は `core_stocks` の total ではなく
> **is_active=1 の件数**（実測 3,715。total は 3,818）。
>
> **(c) は P4b で必ず発火する**: active が +725 されて 4,440 になると
> **3,700/4,440 = 0.833 < 0.98** となり、kabulab-cf の月次 universe sync が毎月
> throw して止まる（現行配布 `data_j.xlsx` 2026-08-31 実測）。
>
> **(d) も同時に直すこと。** (d) は同じ `existingActiveCount` を分母に使うため、
> P4b で active が膨らむと一括対象外化の上限が **74 件 → 88 件**へ自動的に緩む。
> 対象外化の候補は実質すべて内国普通株なので、分母だけが増えると防御が弱くなる。
>
> **2026-09-12 訂正（D4 実装時）: 「(c)(d) の分母を揃える」は誤り。**
> 当初ここには「(c)(d) の分母を `is_active=1 AND instrument_type='equity'` に
> **揃える**」と書いていたが、実装時の反証レビューで (d) がそれでは壊れることが
> 分かった。(d) の分子 `deactivatedIds` は active **全件**から算出される
> （`shouldDeactivate` は data_j の全行集合と突き合わせるので ETF や REIT の
> 上場廃止も候補に入る）。分母だけを equity に絞ると**分子 ⊄ 分母**になり、
> 「守っている母集団に対する割合」という意味が消える。ETF/ETN/PRO の上場廃止が
> 月 74 件を超えれば (d) が誤発火し、D4 が直そうとした「毎月止まる」が形を変えて
> 残る（極端には比率が 1 を超える）。
>
> **確定した分母（`cloud_store/universe_guards.py` の実装が正本）**
>
> | 条件 | 分子 | 分母 | 移植元から変えたか |
> |---|---|---|---|
> | (c) 被覆率 | `isListedEquity` 件数 | `is_active=1 AND instrument_type='equity'` | **変えた** |
> | (d1) 一括対象外化 | 対象外化候補（全銘柄種別） | `is_active=1`（全件） | 据え置き |
> | (d2) 一括対象外化（新設） | 対象外化候補のうち `instrument_type='equity'` | `is_active=1 AND instrument_type='equity'` | **新設** |
>
> (d) を 1 本のまま分母だけ動かすのではなく **2 本に割る**。(d1) は「銘柄種別を
> 問わない大量対象外化」（ETF が一斉に消える事故）を、(d2) は「内国普通株の
> 大量対象外化」を equity 内の比率で拾う。どちらも分子と分母が同じ母集団なので
> 比率としての意味が壊れず、かつ P4b 後に実効上限が 74 → 88 件へ緩むことも無い。
>
> **遷移期（`instrument_type` が全 NULL）の扱い。** 本番 `core_stocks` は P4a の
> 列追加のみが済んでおり、`instrument_type` は **3,818 行すべて NULL**（充填は
> P4a の範囲外。語彙が未決）。この状態で equity に絞ると**分母が 0** になる。
> → equity 件数が `NULL`／`0` のときは「未充填」と判断して**従来の分母
> （active 全体）へ縮退**する。充填前の active 3,715 件は集合として内国普通株と
> ほぼ一致する（ETF/ETN/PRO/外国株は P4b で入る +725 行の側にある）ので、
> 縮退しても意味が変わらない。
> **かつこの縮退は fail-closed**: 充填せずに P4b を実行すると分母が active 全体の
> ままなので (c) は 0.833 で発火して止まる。これは誤検知ではなく「充填は P4b の
> 前提条件」という表明で、`assert_instrument_type_backfilled()` が同じことを
> 先に原因の言葉で止める。
>
> **`instrument_type='equity'` の充填述語は `isListedEquity` と一致させること。**
> (c) の分子は `isListedEquity`（「内国株式」かつ プライム|スタンダード|グロース
> かつ 4文字コード）を通った件数。PRO Market の内国株を `equity` に入れると
> 分母が分子より構造的に大きくなり、(c) が恒久的に 0.98 を割る。
>
> （`tests/test_universe_guards.py` に (c) の縮退・(d1)/(d2) の役割分担・
> 遷移期の挙動を仕様として固定済み。**設計書と食い違っていた「+707 / active
> 4,422」は 2026-05-31 版の data_j による数字で、テスト側の docstring だけが
> それを引いていた。基準日ごとに定数を分けて解消した**）

#### 対向改修（kabulab-cf 側。stockStock からは触れない）

D4 を直したのは stockStock の `cloud_store/universe_guards.py` だが、**実際に月次で
throw するのは kabulab-cf の `src/cron/universe.ts`** であって、stockStock 側には
まだ呼び出し元が無い。別リポジトリなので stockStock からは変更しない。
kabulab-cf 側に必要な改修は次の 3 点で、**P4b より前に入れる**。

1. `assertUniverseCoverage` の引数に `existingEquityActiveCount` を足し、(c) の
   分母をそれに切り替える。`NULL`／`0` は「未充填」として従来の分母へ縮退させる
   （fail-closed。上の遷移期の節と同じ挙動）
2. (d) を (d1)/(d2) に割る。(d2) の分子は `deactivatedIds` のうち
   `instrument_type='equity'` の件数
3. 分母を数える SELECT を追加する
   （`SELECT COUNT(*) FROM core_stocks WHERE is_active = 1 AND instrument_type = 'equity'`）

stockStock 側の実装と定数（`MIN_EXISTING_COVERAGE` / `MAX_DEACTIVATION_RATIO`）は
`universe_guards.py` の行番号つきコメントで手動同期している。**この二重実装自体が
`docs/TARGET-ARCHITECTURE.md` §「共有の同期」で「やめる」と決めた対象**であり、
D4 はその手動同期を 1 回ぶん増やしている。契約ファイルからの生成へ移すまでの
暫定である点を明示しておく。

| 追加列 | 型 | 説明 | ライセンス |
|---|---|---|---|
| `instrument_type` | TEXT | `equity/etf/etn/reit/pro/foreign/preferred`。母集団を 3,818 → 4,445 へ拡張するために必須 | **personal-only**（JPX data_j.xls 由来） |
| `sector33` | TEXT | 33業種 | **personal-only**（同上） |
| `sector17` | TEXT | 17業種 | **personal-only**（同上） |
| `edinet_code` | TEXT | EDINETコード | commercial-ok |
| `listing_status` | TEXT | 上場/監理/整理/上場廃止 | commercial-ok |
| `listing_date` | TEXT | YYYY-MM-DD。一次開示で判明した場合のみ | commercial-ok |
| `delisting_date` | TEXT | 同上 | commercial-ok |
| `license_tag` | TEXT | 行としての代表タグ | メタ |
| `src_source` | TEXT | `EDINET` / `JPX` | メタ |
| `src_data_date` | TEXT | データ基準日 | メタ |
| `src_fetched_at` | INTEGER | epoch秒 | メタ |
| `quality` | TEXT | 正常/要確認/欠損あり | メタ |

追加索引: `idx_core_stocks_active_market (is_active, market)` / `idx_core_stocks_edinet (edinet_code)`。既存の `code` UNIQUE は維持。

**既存 `sector` 列との関係（未解消）**: 既存 `core_stocks.sector` と新設 `sector33` は同一の事実を指す可能性が高いが、本仕様は「**既存 `sector` を触らない・`sector33` を別列として足す**」に留める。統合・廃止の計画は立てていない（同一行に同値2列が残る状態を許容している）。統合するなら既存 consumer（001 のスクリーニング、`株ラボ-Youtube/data/d1.py` の `stocks()`）の洗い出しが先。

**ライセンスが列単位で混在する点の帰結**: ① は「EDINETコードリスト由来=commercial-ok / JPX由来=personal-only」で1行に混在するため、**行の `license_tag` 1列では公開可否を表現できない**。判定は列単位の地図（`jss_column_license`）に従う必要がある。

行数: 4,445（増分はほぼ0）。

#### A-2. `core_stock_financials`（拡張）— ②価格・バリュエーション・テクニカルの唯一の断面

| 追加列 | 型 | 由来 |
|---|---|---|
| `open` `high` `low` | REAL | `PriceTechnicalRecord` |
| `prev_close_pct` | REAL | 前日比率% |
| `turnover` | REAL | 売買代金（当日 close×volume） |
| `week52_high` `week52_low` | REAL | **下記の入力条件を満たす場合のみ非NULL** |
| `sma200` | REAL | **同上** |
| `sma25_dev_pct` | REAL | SMA25乖離率% |
| `bb_upper` `bb_lower` | REAL | 20日±2σ |
| `atr14` | REAL | |
| `volume_ratio25` | REAL | 出来高25日平均比 |
| `license_tag` | TEXT | `personal-only`（yfinance 継承） |
| `src_source` | TEXT | `yfinance` / `stooq` |
| `quality` | TEXT | |

既存列（`price` `per` `pbr` `dividend_yield` `market_cap` `eps` `bps` `roe` `roa` `operating_margin` `data_date` `fetched_at`）はすべて維持。`stock_id` UNIQUE も維持。

> **【必須の前提条件】テクニカル列の入力は「マージ後の10年系列」でなければならない**
>
> `transform/technicals.py` を実読すると、指標には次の窓が必要:
>
> | 列 | 必要な窓 |
> |---|---|
> | `sma5` | 5本 |
> | `sma25` / `bb_upper` / `bb_lower` | 20〜25本 |
> | `sma75` | **75本** |
> | `sma200` | **200本** |
> | `week52_high` / `week52_low` | `span_days >= 364`（**暦日**） |
> | 分割検出 `has_probable_split` | lookback **260営業日** |
>
> yfinance の取得を `period="3mo"`（≒62営業日 / 92暦日）に縮めると、**`sma75` / `sma200` / `week52_high` / `week52_low` が全4,445銘柄で恒久的に NULL になる**（`_sma_last` は本数不足で None、`week52` は `span_days >= 364` を満たさず None を返す仕様＝推定禁止の帰結）。さらに分割検出の窓が 62本に縮み、3ヶ月より古い分割を永久に検出できなくなるため、`daily/{code}.json` の `splits` 配列が固定化する。
>
> **確定仕様**: `core_stock_financials` のテクニカル列は、**R2 `daily/{code}.json` をマージした後の系列**を `compute_technicals` の入力とする。yfinance の `period` は「欠測追いつき用の差分取得」に限定する。処理順は (1) yfinance 差分取得 → (2) R2 `daily/` を GET → (3) マージ → (4) **マージ後系列でテクニカル計算** → (5) R2 PUT + D1 断面。分割検出も (4) のマージ後系列で 260営業日を見る。
>
> この前提が満たされない構成では、上表の `sma200` / `week52_high` / `week52_low` と既存の `sma75` を**列として追加してはならない**（恒久 NULL の列を作ることになる）。

**`operating_margin` の供給経路**: Yahoo `financialData.operatingMargins` の TTM 生値で、stockStock の ③（決算期単位）とは**定義が別物**。`collectors/yfinance_prices.py` の `_VALUATION_KEYS` へ `operatingMargins` / `trailingEps` / `bookValue` / `returnOnEquity` / `returnOnAssets` を追加すれば供給できる見込みだが、**実レスポンスにこれらのキーが存在するかは未検証**。存在しない場合は列を消さず、kabulab-cf 側の取得を列単位排他で残す。

行数: 4,445。

#### A-3. `swing_stock_indicators`（縮小）

**残す**（stockStock が供給できない swing 固有の派生列）:
`sma_20` `sma_60` `range_20d_high` `range_20d_low` `range_width` `fib_high` `fib_low` `fib_382` `fib_500` `fib_618` `trend_long` `trend_short` `perfect_order_long` `perfect_order_short` `avg_turnover_20d` `volume_20d` `volume_ratio` `atr_pct`

**削除候補**（`core_stock_financials` に寄せる）:
`sma_5` `sma_25` `sma_75` `rsi_14` `macd` `macd_signal` `macd_hist` `atr_14` `latest_close` `latest_volume` `latest_date` `pct_change_1d`

> **削除前の必須確認（未了）**: SQLite の `DROP COLUMN` は索引・VIEW・トリガ・生成列が参照していると失敗する。かつ 003/004/002 側の `SELECT *` 依存を**全走査していない**。削除は 003/004/002 のクエリ書き換えが済み、かつ `SELECT *` 依存がゼロであることを確認してからでなければ実行できない。確認が済むまでは両方に書く**移行期の一時的な二重**を許容し、廃止期限をチケット化する。

行数: 4,445。

#### A-4. `core_stock_annual_financials`（**列を追加しない = 現状維持**）

旧設計は `operating_income` / `ordinary_income` / `net_income` / `eps` / `bps` / `dps_actual` / `equity_ratio_pct` / `disclosure_type` / `doc_id` / `license_tag` / `src_source` の11列を足して「年次サマリとして完成させる」としていた。**これを行わない。**

理由: 配置表自身がこのテーブルを「×（`jss_financials` からの派生断面）」と分類している。`jss_financials` の PK は `(code, fiscal_period_end, disclosure_type)` なので、年次サマリは `disclosure_type='本決算'` で索引から直接引ける。列を11本足すのは、派生と認めたテーブルを太らせて重複を深める方向。

**確定仕様**: 既存の `revenue` のみ（15,943行）を**互換目的で維持**する。保持期間は銘柄あたり最新10期（4,445 × 10 = 44,450行）。新規 consumer は `jss_financials` を引く。既存 consumer（`株ラボ-Youtube/data/d1.py` の `annual_revenue()`、`株ラボ-新高値ブレイク検証`、001 の銘柄詳細）を `jss_financials` へ向け終えたら DROP する、を別チケットにする。

#### A-5. `ir_disclosures`（拡張）— ④開示メタ

**冪等キーの不一致が最大の罠**: kabulab-cf は `tdnet_id = yanoshin API の生 id`（`ingest.ts:194`）、stockStock は PDF ファイル名（例 `140120260610567733`）を `doc_id` にする。このまま writer を移すと既存37,338行と一致せず全件二重挿入になる。

| 追加列 | 型 | 説明 |
|---|---|---|
| `doc_id` | TEXT | stockStock のキー（PDFファイル名 / EDINET docID）。**UNIQUE**（NULL は複数可） |
| `source` | TEXT | `TDnet` / `EDINET`。EDINET開示を同一テーブルに統合 |
| `split_ratio` | TEXT | 例 `1:3` |
| `split_factor` | REAL | 例 3.0 / 0.2 |
| `effective_date` | TEXT | 効力発生日 |
| `license_tag` | TEXT | `factual-cite` / `commercial-ok` |
| `raw_sha256` | TEXT | `jss_raw_files` への結合キー |

**既存 `tdnet_id` UNIQUE は残し、両キーで冪等化**する。

**既存列の格納形式を変えない**: `tags`（JSON配列文字列）・`primary_tag`・`pdf_sentiment*` は現状のまま。`株ラボ-Youtube/data/d1.py` が `tags LIKE '%"タグ名"%'` で検索しており、形式を変えると静かに壊れる。

**NOT NULL 制約（実読で確認済み）**: `stock_id`(FK) / `tdnet_id`(unique) / `company_code` / `company_name` / `title` / `pubdate` / `document_url` / `tags`(JSON) がすべて NOT NULL。stockStock はこのうち `tdnet_id` と `tags` を現状供給できない。

**列単位の writer 排他**（同一テーブルに2 writer を許す唯一の形）:

| 列グループ | writer | 許される操作 |
|---|---|---|
| 行の作成 + `tdnet_id` / `doc_id` / `title` / `pubdate` / `document_url` / `stock_id` / `source` / `split_*` / `effective_date` / `license_tag` / `raw_sha256` | stockStock | INSERT + UPDATE |
| `tags` / `primary_tag` / `pdf_sentiment` / `pdf_sentiment_method` / `pdf_sentiment_score` | kabulab-cf | **既存行の UPDATE のみ**（INSERT / DELETE 禁止） |

実装上の担保: stockStock 側の UPSERT は SET 句を**ホワイトリストで列挙**する（`SELECT *` 起点の動的 UPSERT を禁止。列が増えた瞬間に他 writer の列を潰すため）。

追加索引: `idx_ir_doc_id (doc_id)` UNIQUE / `idx_ir_source_pubdate (source, pubdate DESC)`。

行数: 既存37,338 + 年約3万 → 5年で約19万行。1行 **875 B**（title 日本語120B + document_url 80B + tags JSON 60B + raw_sha256 64B + 5索引 170B）。

#### A-6. `yutai_benefits`（拡張）— ⑨株主優待

| 追加列 | 型 | 説明 |
|---|---|---|
| `license_tag` | TEXT NOT NULL DEFAULT `'personal-only'` | |
| `source` | TEXT | `minkabu` |
| `source_url` | TEXT | |
| `data_date` | TEXT | |
| `fetched_at` | INTEGER | |

**確定規約**
1. `record_months: list[int]` を既存の `record_month INTEGER NOT NULL`（単数）へ**行展開**する（既存粒度を維持し 002 の権利月絞込を壊さない）。PK は `(stock_id, item_name, record_month)`。
2. ジャンル名 → `yutai_genres.slug`（16種固定）のマッピング表をコード側に持つ。未知ジャンルは `other`。**`yutai_genres` テーブル自体は stockStock が触らない**。
3. **`estimated_value` / `short_summary` / `estimate_value_source` / `estimate_source_url` を UPDATE の SET 句に含めない**（列単位排他）。これらは kabulab-cf のローカルLLM（node-llama-cpp）+ 楽天市場API による推定で**再取得不能な資産**。`monthly.ts:83-89` が `isNotNull(estimatedValue)` でフィルタして `yutai_yield` → `otakara_stock_scores` を作っているため、消えると 002 の並びが変わる。
4. **`DELETE` を1回も発行しない**。現行 `fetch-yutai-full.ts` は「既存削除 → クリーンインポート」方式だが、これを踏襲すると LLM 資産が全滅する。`ON CONFLICT … DO UPDATE` のみ。
5. **保護ガード**: 「`estimated_value` が非 NULL の行数」を毎回 `jss_dataset_freshness` に記録し、**減ったら異常終了**する（事後に気づく手段が手動比較しかないため）。
6. `description` 列は残す（`license_tag='personal-only'`）。**公開面から外すのは kabulab-cf 側の責務**で、D1 のスキーマ設計の範囲外。

行数: 8,314。1行 **1,200 B**（`description` に掲載文全文が入るため）。

#### A-7. `otakara_stock_financials`（**VIEW にしない = 実テーブルのまま**）

旧設計は「`core_stock_financials` + `swing_stock_indicators` + `yutai_yield` の JOIN による VIEW に置換し、列名を同一にすれば 002 の SSR は無改修」としていた。**これを行わない。**

理由: `src/cron/monthly.ts:125` が `.insert(otakaraSchema.stockFinancials)`、同 `:170` が `.insert(otakaraSchema.stockScores)` を**同一ループ内で**実行している。VIEW には INSERT できないため Phase 2 全体が例外で落ち、`otakara_stock_scores`（1,645行・002 の並び順の正本）の再構築も止まる。旧設計は「002 の SSR クエリは無改修」しか確認しておらず、**writer の存在を確認していなかった**。

**確定仕様**: 実テーブルのまま残し、`monthly.ts` の Phase 2 の**入力**（`core_stock_financials` / `swing_stock_indicators` / `yutai_benefits`）が stockStock 由来になる、という形で正本を統一する。物理的な重複は 1,645行 / 約0.5 MB で、VIEW 化で得るものより `monthly.ts` を壊すリスクのほうが大きい。将来 VIEW 化するなら `monthly.ts` の Phase 2 書き換えを同一の変更に含めること。

---

### B. 新規テーブル（`jss_` 接頭辞）

#### B-1. `jss_raw_files` — ⑤原本の索引（R2 への唯一の入口）

```sql
CREATE TABLE jss_raw_files (
  sha256           TEXT PRIMARY KEY,      -- 64hex
  r2_bucket        TEXT NOT NULL,         -- 'jp-stock-raw'
  r2_key           TEXT NOT NULL,         -- raw/...
  derived_key      TEXT,                  -- derived/...（無ければ NULL）
  derived_ext      TEXT,                  -- 'parquet' | 'txt'
  source           TEXT NOT NULL,
  datatype         TEXT NOT NULL,
  scope            TEXT NOT NULL,
  doc_id           TEXT,                  -- R2 キーの doc_id セグメントと同値
  code             TEXT,
  data_date        TEXT,                  -- YYYY-MM-DD
  ext              TEXT NOT NULL,
  size_bytes       INTEGER NOT NULL,
  license_tag      TEXT NOT NULL,
  convert_status   TEXT NOT NULL,         -- 完了/失敗/対象外
  first_fetched_at INTEGER NOT NULL,      -- epoch秒
  last_fetched_at  INTEGER NOT NULL
);
CREATE INDEX idx_jss_raw_doc       ON jss_raw_files(doc_id);
CREATE INDEX idx_jss_raw_code_date ON jss_raw_files(code, data_date DESC);
CREATE INDEX idx_jss_raw_src_type  ON jss_raw_files(source, datatype, data_date DESC);
```

`url` と秒精度の `fetched_at` 履歴は R2 のカスタムメタデータに置き、D1 の1行を軽くする（行数が最も多いテーブルのため）。

**書込順序**: R2 PUT が成功してから D1 INSERT。逆にすると「D1 に行があるのに R2 にオブジェクトが無い」＝索引が嘘をつく状態を作る。R2 → D1 なら最悪でも「未索引オブジェクト」で、無害かつ突合で拾える。

**`doc_id` の充足が前提**: `doc_id` が NULL だと ④⇄⑤ の結合が成立しない。R2 キー設計の §3.2 を参照（`RawArtifact` への `doc_id` 追加が前提条件）。

**同日複数行**: `tdnet_list` は毎時取得で同一 `data_date` に最大11行並ぶ（r2_spec §3.3）。`idx_jss_raw_src_type` で最新1件を引ける。

行数: **年約6.3万** → 5年で約31.5万行。
**1行 650 B**（旧設計の 200 B は本文だけで破綻する。実キー例 `raw/edinet/pdf/2024/2024-07-29/8306/S100U1XX/xxxxxxxxxxxxxxxx.pdf` が 65 B、`derived_key` 69 B、`sha256` 64 B の3列だけで **198 B**。加えて PK の暗黙索引 + 3明示索引で約 200 B）。

保持: Paid は無期限 / **Free は直近24ヶ月**（それ以前は `derived/_index/raw_files_{yyyy}.parquet` へ退避し D1 から DELETE）。

#### B-2. `jss_financials` — ③財務サマリ

```sql
CREATE TABLE jss_financials (
  code                      TEXT NOT NULL,
  fiscal_period_end         TEXT NOT NULL,   -- YYYY-MM-DD
  disclosure_type           TEXT NOT NULL,   -- 本決算/1Q/2Q/3Q/修正/予想
  stock_id                  INTEGER,
  consolidated              TEXT,
  accounting_standard       TEXT,
  net_sales                 REAL,
  operating_income          REAL,
  ordinary_income           REAL,
  net_income                REAL,
  eps REAL, bps REAL, roe_pct REAL, roa_pct REAL, equity_ratio_pct REAL,
  cf_operating REAL, cf_investing REAL, cf_financing REAL,
  dps_actual REAL, dps_forecast REAL,
  forecast_net_sales REAL, forecast_operating_income REAL,
  forecast_ordinary_income REAL, forecast_net_income REAL, forecast_eps REAL,
  disclosed_at   INTEGER,
  doc_id         TEXT,
  raw_sha256     TEXT,
  source         TEXT NOT NULL,
  license_tag    TEXT NOT NULL,
  data_date      TEXT,
  fetched_at     INTEGER NOT NULL,
  quality        TEXT NOT NULL,
  PRIMARY KEY (code, fiscal_period_end, disclosure_type)
);
CREATE INDEX idx_jss_fin_stock     ON jss_financials(stock_id, fiscal_period_end DESC);
CREATE INDEX idx_jss_fin_disclosed ON jss_financials(disclosed_at DESC);
```

`local_store/schema.py` の `financials` と1対1。年次サマリは `disclosure_type='本決算'` で PK 先頭から索引で引ける（`core_stock_annual_financials` を太らせない根拠）。

行数: 年約2万 → 5年10万行。1行 **350 B**。保持: 無期限。

#### B-3. `jss_xbrl_documents` — ⑧ファクトの所在索引

```sql
CREATE TABLE jss_xbrl_documents (
  doc_id           TEXT PRIMARY KEY,
  code             TEXT,
  source           TEXT NOT NULL,        -- EDINET | TDnet
  doc_type_code    TEXT,                 -- 120/130/140/...
  period_start     TEXT,
  period_end       TEXT,
  fiscal_year      INTEGER,
  submitted_at     INTEGER,
  fact_count       INTEGER NOT NULL,
  text_block_count INTEGER NOT NULL,
  raw_sha256       TEXT NOT NULL,        -- jss_raw_files への結合
  parquet_key      TEXT NOT NULL,        -- derived/...parquet
  parquet_bytes    INTEGER NOT NULL,
  license_tag      TEXT NOT NULL
);
CREATE INDEX idx_jss_xbrl_code_period ON jss_xbrl_documents(code, period_end DESC);
CREATE INDEX idx_jss_xbrl_fy          ON jss_xbrl_documents(fiscal_year, source);
```

**ファクト本体は1行も D1 に入れない。** 行数: 年約8,300 → 5年41,500行。1行 285 B。

#### B-4. `jss_xbrl_elements` — 勘定科目の語彙表

```sql
CREATE TABLE jss_xbrl_elements (
  element       TEXT PRIMARY KEY,
  namespace     TEXT,
  doc_count     INTEGER NOT NULL,
  first_seen    TEXT,
  last_seen     TEXT,
  is_text_block INTEGER NOT NULL DEFAULT 0
);
```

MCP や分析が「どの勘定科目が存在するか」を引くための入口。`(element, doc_id)` の逆引き（年883万ペア）は**置かない**。

**行数: 7.5万〜10万行**（旧設計の「約2万行」は4〜5倍の過小）。

根拠（本タスクで実測）: 変換済み CSV **150 doc** をサンプルすると distinct element が **3,489**、総行数 136,600（平均 **910 行/doc**）。600 doc サンプルでは distinct **7,677**。Heaps' law で β = log(7677/3489) / log(4) ≈ **0.57**。33,257 doc へ外挿すると 3,489 × (33,257/150)^0.57 ≈ **75,300**。5年41,600 doc なら同程度。TDnet XBRL と提出者独自拡張を足すと10万に届く。1行 350 B。

#### B-5. `jss_supply_latest` — ⑧'需給の最新断面

```sql
CREATE TABLE jss_supply_latest (
  code        TEXT NOT NULL,
  data_type   TEXT NOT NULL,   -- 'jsf_zandaka' | 'jsf_shina' | 'jpx_margin'
  data_date   TEXT NOT NULL,
  isin        TEXT,
  loan_bal    INTEGER, loan_chg  INTEGER,
  stock_bal   INTEGER, stock_chg INTEGER,
  ratio       REAL,            -- 信用倍率（計算値）
  turn_days   REAL,
  r2_key      TEXT NOT NULL,   -- supply/{code}.json
  license_tag TEXT NOT NULL DEFAULT 'personal-only',
  fetched_at  INTEGER NOT NULL,
  quality     TEXT NOT NULL,
  PRIMARY KEY (code, data_type)
);
CREATE INDEX idx_jss_supply_date ON jss_supply_latest(data_type, data_date DESC);
```

**空欄・マスク値を 0 に潰さない**（`NULL` のまま）。履歴は R2 のみ。

行数: 4,400 × 3 = 約13,200（増えない）。1行 250 B。

#### B-6. `jss_index_symbols` — 指数・為替のシンボル対応表

```sql
CREATE TABLE jss_index_symbols (
  slug         TEXT PRIMARY KEY,   -- 'n225','topix','vix','gspc','usdjpy','niy_f','nkvi'
  yahoo_symbol TEXT NOT NULL,      -- '^N225' 等
  name_ja      TEXT,
  r2_key       TEXT NOT NULL,      -- index/{slug}.json
  license_tag  TEXT NOT NULL,
  last_date    TEXT,
  updated_at   INTEGER
);
```

**slug は7つ**（旧設計は6つで、`finmath_daily_ohlcv` の7シンボル目が欠けていた）。`nkvi`（日経VI）の Yahoo シンボルは**要確認**。行数: 7。

#### B-7. `jss_job_runs` — ⑦収集ジョブログ

```sql
CREATE TABLE jss_job_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  job_name      TEXT NOT NULL,
  status        TEXT NOT NULL,
  processed     INTEGER NOT NULL,
  failed        INTEGER NOT NULL,
  failed_codes  TEXT,
  run_url       TEXT,
  duration_secs REAL,
  finished_at   INTEGER NOT NULL
);
CREATE INDEX idx_jss_job ON jss_job_runs(job_name, finished_at DESC);
```

行数: 年約3,000。1行 400 B。保持: **直近18ヶ月**（DELETE で剪定）。

#### B-8. `jss_dataset_freshness` — データセット鮮度の断面

```sql
CREATE TABLE jss_dataset_freshness (
  dataset             TEXT PRIMARY KEY,  -- 'prices_daily','ir_disclosures','supply_jsf',...
  store               TEXT NOT NULL,     -- 'D1' | 'R2'
  location            TEXT NOT NULL,     -- テーブル名 or バケット/プレフィックス
  writer              TEXT NOT NULL,     -- 唯一の writer 名
  latest_data_date    TEXT,
  row_or_object_count INTEGER,
  bytes               INTEGER,
  license_tag         TEXT,
  updated_at          INTEGER NOT NULL  -- **データ自身の as_of**（記録時刻ではない）
);
```

行数: 約20。現行の `MAX(data_date) FROM core_stock_financials`（3,764行走査）と `MAX(pubdate) FROM ir_disclosures`（37,338行走査）を **1クエリ20行走査**に置換でき、D1 の走査行課金を桁で下げられる。

**`updated_at` の意味（間違えると監視が恒久的に緑になる）**: この列には「観測した実表の取得 epoch」＝ **データ自身の as_of** を入れる。行を書いた時刻（`now`）を入れてはいけない。`now` を入れると、実表が凍結していても記録のたびに値が進み、**翌日から永久に緑**になる（鮮度表があるのに何も検知しない状態で、writer 不在より悪い。空なら少なくとも「空だ」と分かる）。

測れなかった場合は `0` を入れる（列が NOT NULL なので NULL を書けない）。`0` は「不明」の約束で、`cloud_store/slo.py:age_hours` は `0` を `None` と同じく unknown に倒す。

**判定は `latest_data_date` を優先する。** `updated_at` は日付列を持たない表（`core_stocks` / `yutai_benefits`）のフォールバックにしか使わない。取得時刻で判定すると偽の緑が出る実例: `jss_supply_latest` は `data_date=2026-09-10` なのに `fetched_at` は `2026-09-11T14:58Z` で、`supply_daily` が `fetched_at = now_jst()` を全行に塗り直すため、日証金が同じスナップショットを返し続けても取得時刻基準では永遠に緑になる。

#### B-9. `jss_writer_claims` — 列単位の writer 排他

```sql
CREATE TABLE jss_writer_claims (
  dataset      TEXT NOT NULL,
  column_group TEXT NOT NULL,
  writer       TEXT NOT NULL,
  updated_at   INTEGER NOT NULL,
  PRIMARY KEY (dataset, column_group)
);
```

**新設の根拠**: `jss_dataset_freshness.writer` は「唯一の writer」を1つしか書けないが、`ir_disclosures` と `yutai_benefits` は列集合を分割して2 writer を許す必要がある（そうしないと `classify.ts` の20タグと LLM 推定値を残せない）。各ジョブの冒頭で自分の claim を照合し、想定外の writer 名なら**異常終了**する（黙って上書きしない）。

> **注**: R2 の「同一キー並行書込 1/秒の 429」は writer 二重稼働の検知手段として当てにできない（`daily/` は 4,445キー、`supply/` は 4,351キーに分散するため、2本の writer が別銘柄を処理していれば 429 は出ない）。**D1 も R2 も検知手段はこの claim 照合と payload の `writer` 突合だけ**であり、両者の防御強度に差はない。最も確実なのは writer ごとにトークンを分け、切替時に旧トークンを revoke すること。

行数: 約20。

#### B-10. `jss_column_license` — 列単位のライセンス地図

```sql
CREATE TABLE jss_column_license (
  table_name  TEXT NOT NULL,
  column_name TEXT NOT NULL,
  license_tag TEXT NOT NULL,   -- commercial-ok | factual-cite | personal-only
  PRIMARY KEY (table_name, column_name)
);
```

**新設の根拠**: `core_stocks` は「EDINETコードリスト由来=commercial-ok / JPX data_j.xls 由来（`market`/`sector33`/`sector17`/`instrument_type`）=personal-only」で**1行に混在**する。行の `license_tag` 1列では表現できないため、判定は列単位でなければならない。`licensing.py` から生成し、手書きの二重定義を作らない。

行数: 約300。

---

### C. D1 に置かないもの（明示）

| データ | 年間行数 | 置き場 | 理由 |
|---|---:|---|---|
| ②日足 OHLCV 全履歴 | 95.5万行 | R2 `daily/{code}.json` | 10GB上限が引き上げ不可。無限に伸びる |
| ②テクニカル履歴（SMA/RSI/MACD/BB/ATR） | 95.5万行 | **どこにも置かない** | 日足から決定的に再計算できる純粋な派生値 |
| ⑧XBRL全ファクト | **883万行** | R2 `derived/*.parquet` | 索引だけで10GBを超える |
| ⑧'需給の日次履歴 | 約210万行 | R2 `supply/{code}.json` | 同上 |
| ⑤原本のバイト列 | — | R2 `raw/` | D1 は1行2MB上限、KV 25MiB、DO 10GB |
| ⑥エクスポート | — | R2 `export/` | ファイル |
| ⑧XBRL の日本語全文索引 | — | 端末B PostgreSQL + PGroonga | 下記 |
| `(element, doc_id)` の逆引き | 年883万ペア | 置かない | `jss_xbrl_elements`（語彙）+ `jss_xbrl_documents`（所在）で代替 |

**⑧XBRL全ファクトの検索手段**

1. **ピンポイント取得（doc_id 指定）**: D1 `jss_xbrl_documents.doc_id` → `parquet_key` → R2 GET 1回。1ファイル平均 **359 KB**（実測）。D1 の走査は1行。
2. **銘柄横断（code + 期間）**: `idx_jss_xbrl_code_period` で該当 doc_id を索引レンジで引き、必要な Parquet だけ GET。
3. **勘定科目の探索**: `jss_xbrl_elements`（7.5万〜10万行）で語彙を引いてから 1 または 2 へ。
4. **日本語全文検索**: **Cloudflare 上には置かない。** 正本は端末B の PostgreSQL + PGroonga（`local_store/schema.py` の `FTS_STATEMENTS` として実装済み）。

D1 FTS5 を使わない根拠（推測ではなく数量）:
- D1 は FTS5 をサポートするが、**どのトークナイザが有効かは公式ドキュメントに記載が無い**（**未確認**）。
- 仮に `trigram` が使えても、対象テキストは **3,530万ファクト・tidy CSV 換算 9.021 GB**（実測）。FTS5 の trigram 索引は元テキストの数倍になるため、**索引だけで Paid の 10GB 上限を超える**。Free の 500MB では3桁足りない。
- `unicode61` は CJK を分かち書きできず、日本語 textBlock は事実上1トークンになる。
- 将来 CF 上で横断分析が必要になったら **R2 Data Catalog（Apache Iceberg）+ R2 SQL**（圧縮後スキャン量 $2.50/TB）だが、**有効化すると doc_id 単位 Parquet と Iceberg テーブルの二重保持**になるため現設計では有効化しない。移行パスだけ空けておく。

---

### D. 外部に出す述語 — 索引でカバーされるもののみ

D1 の課金軸は**走査行数**で `LIMIT` では下がらない。自由な WHERE を開放せず、**固定パラメータのエンドポイント**だけを出す。

| 用途 | 述語 | カバー索引 | 走査行数 |
|---|---|---|---:|
| 銘柄1件 | `core_stocks.code = ?` | `code` UNIQUE | 1 |
| 市場別一覧 | `is_active = 1 AND market = ?` | `idx_core_stocks_active_market` | ≤1,600 |
| 銘柄の開示 | `ir_disclosures.stock_id = ? AND pubdate >= ?` | 既存 `(stock_id, pubdate)` | ≤数百 |
| タグ別最新 | `primary_tag = ? ORDER BY pubdate DESC LIMIT ?` | `(primary_tag, pubdate DESC)` | LIMIT分 |
| 財務時系列 | `jss_financials.code = ? ORDER BY fiscal_period_end DESC` | PK先頭 | ≤30 |
| 原本の所在 | `jss_raw_files.doc_id = ?` | `idx_jss_raw_doc` | 1 |
| ファクトの所在 | `jss_xbrl_documents.code = ? ORDER BY period_end DESC` | `idx_jss_xbrl_code_period` | ≤30 |
| 鮮度 | `jss_dataset_freshness`（全件） | — | 約20 |

**禁止**: `LIKE '%…%'`（`tags LIKE` を含む。既存 `株ラボ-Youtube` の用法は内部の D1 直読として温存し、外部公開面には出さない） / 無索引列でのソート・絞込 / 深い `OFFSET`（cursor 方式に置換） / **D1 REST API のトークンを外部に配らない**。

---

### E. 容量見積り（索引込みの逐行再計算）

旧設計は「素データ 236 MB × 1.5 = 354 MB」としていたが、`jss_raw_files` の1行 200 B と `jss_xbrl_elements` の2万行が過小だった。逐行で積み直す。

| テーブル | 5年後の行数 | 1行 | 容量 |
|---|---:|---:|---:|
| `jss_raw_files` | 315,000 | 650 B | **205 MB** |
| `ir_disclosures` | 190,000 | 875 B | **166 MB** |
| `jss_financials` | 100,000 | 350 B | 35 MB |
| `jss_xbrl_elements` | 75,000–100,000 | 350 B | 26–35 MB |
| `yuho_*`（既存3本） | 59,700 | 300 B | 18 MB |
| `jss_xbrl_documents` | 41,500 | 285 B | 12 MB |
| `core_stock_annual_financials` | 44,450 | 250 B | 11 MB |
| `yutai_benefits` | 8,314 | 1,200 B | 10 MB |
| `jss_supply_latest` | 13,200 | 250 B | 3.3 MB |
| `swing_*` / `rsi_percentile` / `otakara_*` 残置分 | — | — | 3.6 MB |
| `core_stocks` | 4,445 | 500 B | 2.2 MB |
| `core_stock_financials` | 4,445 | 400 B | 1.8 MB |
| `jss_job_runs`（18ヶ月保持） | 4,500 | 400 B | 1.8 MB |
| `jss_index_symbols` / `jss_dataset_freshness` / `jss_writer_claims` / `jss_column_license` | 約350 | — | 2.0 MB |
| **合計** | | | **約500 MB** |

| プラン | 1DB上限 | 5年後の占有 | 判定 |
|---|---|---:|---|
| **Workers Paid** | 10 GB | **5.0%** | 余裕。ボトルネックは容量ではなく走査行数 |
| **Workers Free** | 500 MB | **約100%** | **5年もたない。縮退が必須条件** |

**Free の縮退（案ではなく必須条件）**: `ir_disclosures` と `jss_raw_files` を**直近24ヶ月保持**にし、超過分を R2 `derived/_index/*.parquet` へ退避する。
→ `ir_disclosures` 63 MB + `jss_raw_files` 78 MB となり、合計 **約270〜280 MB（54〜56%）**。

**Free で決定的なのは容量ではなく rows read**: 004 の EMH モメンタム画面が `swing_daily_ohlcv` を `stock_id, date` 昇順で**全走査**しており、1回で 336,185行。Free の 5,000,000 rows read/日に対し **1日15回の画面表示で枯渇する**。したがって Free では `swing_daily_ohlcv` の廃止が任意ではなく必須になる。

**初回一括投入の rows written**: `core_stocks` 4,445 + `core_stock_financials` 4,445 + `core_stock_annual` 44,450 + `jss_financials` 100,000 + `jss_raw_files` 70,463 + `jss_xbrl_documents` 33,257 + `jss_xbrl_elements` 7.5万〜10万 + `yutai_benefits` 8,314 + `ir_disclosures` の `doc_id` バックフィル UPDATE 37,338 = **38.3万〜42.3万行**。Free の 100,000/日 かつ日常ジョブ（日次約9,000 + `tdnet_hourly`）と同じ枠なので **5〜6日に分割**が必要（旧設計の「約22万行 → 3日」は `jss_xbrl_*` 11〜15万行と `core_stock_annual` 4.4万行を落としていた）。

**writer 自身の rows read（旧設計は未計上）**: `code → core_stocks.id` の解決マップ（`SELECT id, code FROM core_stocks` = 4,445行全走査）を各ジョブ冒頭で張るため、`prices_daily` 1 + `edinet_daily` 1 + `tdnet_hourly` 11 + `supply` 2 = 15回/日 × 4,445 = **約66,700 rows read/日**。Free 5M/日 の 1.3%。

> **未確定（U1・U2）**: Workers プランが Free か Paid かは未確定で、D1 `kabulab-cf` の**現使用量も未実測**。上表は新規分のみの見積りであり、現使用量を測らない限り Free / Paid の判断そのものが下せない（`wrangler d1 info` の `database_size` で取得可能）。

---

# Notion 雛形（サブ面）

## Notion 雛形 確定仕様（正本 = Cloudflare / Notion = サブ面）

対象: `/Users/satoki252595/projects/stockStock/src/jp_stock_pipeline/notion/`、`config.py` の `DB_REGISTRY`、`.github/workflows/*.yml`。
本書は「残すDB・そのプロパティ定義・廃止するもの・その理由」のみを確定する。R2/D1 のスキーマと配置表は再掲しない。

---

### 0. 前提と、1つだけ残す例外

| | 現行 | 本仕様 |
|---|---|---|
| 正本 | Notion 9DB | Cloudflare（D1 + R2） |
| Notion の役割 | 全データの格納先 | **人が見る／人が書く面のみ** |
| 書込契約 | Notion / ローカルPG の対称な双方向フェールセーフ（`runner.py:_persist` が「どちらか一方に残れば成功」） | **CF（正本）→ ローカルPG（ミラー）→ Notion（サブ・ベストエフォート）** の階層化 |
| 時系列 | ①配下の子DB | **Notion には一切置かない** |
| 原本の実体 | ⑤に添付 | **R2**。Notion は添付を持たない |

**例外（移行期のみ）: ④開示書類は第6波まで Notion をサブに降格しない。**
理由は、CF 側 `ir_disclosures` への writer 移管が第6波まで保留されるため（`tdnet_id` NOT NULL + UNIQUE、`tags` NOT NULL、`primary_tag` の語彙が `DOC_TYPES` と体系違い）。この期間、CF 側の ④ は旧 writer の行であり stockStock の正本ではない。したがって **第6波完了までは「④の構造化正本 = Notion ④ + ローカルPG `disclosures`」**とし、④の保持窓（36ヶ月剪定）は第6波完了後にのみ開始する。これを曖昧にすると「正本がどこにも無い期間」が数ヶ月生まれる。

### Notion に置いてよいかの判定基準（3条件すべてを満たすものだけ残す）

1. 行数が銘柄数で固定される、または保持窓を明示できる
2. 人が目で読む／人が編集する用途がある（機械が読むだけなら D1/R2/ローカルPG で足りる）
3. 1DB 250,000 行の 50% 以内に5年間収まる

---

### 1. 判定サマリ（DB_REGISTRY 9キー → 8キー）

| # | オブジェクト | 判定 | 5年後の行数 | 理由 |
|---|---|---|---|---|
| ① | 銘柄マスタ | **残す・拡張** | 4,445 | 人が開くハブ。逆relation で②③④⑧⑨を辿れる唯一の面。人間所有列（ウォッチ/タグ/メモ）の置き場 |
| ② | 株価テクニカル | **残す・列は1つも削らない** | 4,445 | 1行=1銘柄の断面で伸びない。Notionビュー（割安/売られすぎ/高ROE）が乗る面 |
| ③ | 財務サマリ | **残す・投入窓5年** | 約100,000（上限の40%） | 人が期別推移を読む。窓を切らないと10年で上限到達 |
| ④ | 開示書類 | **残す・保持窓36ヶ月（剪定開始は第6波後）** | 約90,000（36%） | 銘柄ページの開示タイムライン。年3万行 |
| ⑤ | 原本ファイル | **凍結（DBは削除しない）** | 増加を0にする | §4 |
| ⑥ | 時系列エクスポート | **「データセットカタログ」へ作り変え** | 約20 | 添付を捨て、所在＋鮮度＋取得手順のメタ行に降格 |
| ⑦ | 収集ジョブログ | **残す・異常時のみ記録** | 年数百 | 全実行ログの正本は D1 側。Notion は人が対応を書き込む面 |
| ⑧ | 需給 | **作り変え（1行=銘柄の最新断面）** | 4,445 | 現行未コミット設計（1行=銘柄×ISIN×週）は日次化後 年104万行で1年で上限突破。Notion 側0行の今なら無償 |
| ⑨ | 株主優待 | **残す・公開事故防止を追加** | 約8,300 | 8,314行で固定 |
| — | ①配下 子DB「株価テクニカル履歴」 | **全廃** | 0 | §3 |
| — | 📖 データカタログ | **残す・create-only を全置換へ修正** | — | §6 |

番号 ①〜⑨ は据え置き、**⑤を欠番にする**（再採番は `db_ids.json` の全参照を壊す）。

---

### 2. 履歴子DB（①配下）の全廃 — 確定

コミット `a753672` / `b8e0d4d` で入った `notion/price_history.py`（`HISTORY_SHARD_THRESHOLD = 8000`、1営業日=1行の日次追記）を廃止する。根拠4点。

**(a) 3番目のコピーになる。** 同じ日足とテクニカルは R2 `daily/{code}.json`（10年・4,444件・0.694GB）とローカルPG `prices` にある。テクニカルは日足から決定的に再計算できる純粋な派生値で、「決定的に再計算できる派生値は CF に置かない」と決めたもの。それを Notion にだけ残すのは規則の趣旨に反する。

**(b) 日次予算の 71% を占める。** 実コードから数えた `prices_daily` の Notion リクエスト（4,445銘柄）:

| 内訳 | req |
|---|---:|
| ①全走査（`query_database` は `page_size=100`）| 45 |
| ①状態マップ（`load_stock_master_state`）| 45 |
| ②page マップ（`load_price_page_map`）| 45 |
| ②upsert | 4,445 |
| 履歴 `_find_page`（基準日 title equals）| 4,445 |
| 履歴 create/update | 4,445 |
| 履歴 `write_history_pointer`（新規行ごとに①を部分更新）| 4,445 |
| 原本UL・⑦ | 13 |
| **合計** | **17,928** |

実効 1.04〜1.27 req/s で **3時間55分〜4時間47分**（3,900銘柄で計算すると 15,692 req = 3h26m〜4h11m となり、`prices_daily.yml` の `timeout-minutes: 300` の実測根拠と一致する）。履歴を止めると **4,548 req ≒ 60〜73分**。

**(c) 機能的に劣る。** 子DBは銘柄ごとに独立したDBなのでDB横断クエリができず、「全銘柄でRSI14が30以下だった日」に答えられない。同じ問いに R2 `daily/` とローカルPG `prices` は答えられる。

**(d) シャード機構がデッドコード。** 1銘柄の年間増加は245行。閾値8,000に到達するのは約32.6年後。`parse_history_db_title` / `should_roll_shard` / `_find_latest_history_child` とポインタ3列は、一度も発火しない分岐の維持コスト。

#### 「銘柄ページ配下に過去分を貯める」要求の代替

> **子DBは0個。「銘柄ページを開けば過去分に辿り着ける」は relation の逆向きプロパティで担保する。**

- `dual_property` relation により、①ページ上に「②株価テクニカル」「③財務サマリ」「④開示書類」「⑧需給」「⑨株主優待」の逆向きプロパティが自動生成され、その銘柄の行がリスト表示される。③は銘柄あたり最大25件（5年×5開示種別）、④は36ヶ月窓で平均60件程度。
- **満たせない部分は認める**: ソート済みテーブルビューを銘柄ページ本文に置くこと（linked view）は、公開 REST API がビュー作成に未対応のため自動化できない（`RECOMMENDED_VIEWS` が手動案内なのと同じ制約）。4,445ページ分を手で置くのは非現実的。
- 代替として、①ページ**本文**に外部リンクブロックをページ作成時に1回だけ生成する（以後触らない＝1銘柄1回のコスト）: ローカルPG API `GET /prices/{code}?from=&to=` / 007 `/vwap-analysis/api/daily?code={code}` / 006 `/ir-catalog/stocks/{code}`。

#### 実装
- `price_history.py` は**削除しない**。`prices_daily.py` からの呼び出しを外し、`--skip-history` を廃して **`--enable-history`（既定オフ）へ反転**する。workflow からは渡さない。
- 既に作成済みの子DBは削除しない（削除 req のほうが高く、失うものがある）。
- ①の `MASTER_PROP_HISTORY_DB_ID` / `MASTER_PROP_HISTORY_SHARD` / `MASTER_PROP_HISTORY_ROW_COUNT` は Notion 上に残置し、`stock_master_schema()` から外して「凍結」とカタログに記す（プロパティ削除APIが未確認のため §10-A1）。

---

### 3. 共通プロパティ v2

| プロパティ | 型 | 改廃 |
|---|---|---|
| ソース | select | **維持**。`SOURCE_OPTIONS` に `日証金` を追加（`models.Source.JSF` / `licensing.SOURCE_LICENSE` / `ATTRIBUTION` にも追加が必要） |
| ライセンスタグ | select | 維持（commercial-ok / factual-cite / personal-only） |
| データ基準日 / 取得日時 | date | 維持 |
| データ品質 | select | 維持（正常 / 要確認 / 欠損あり）。**選択肢を増やさない**（理由は §5-② 末尾） |
| ~~原本（relation→⑤）~~ | relation | **凍結**（⑤凍結に伴う。Notion 上は残置し、新規行では空） |
| **原本SHA256** | rich_text | **新設**。D1 `jss_raw_files.sha256` / ローカルPG `raw_files.sha256` への結合キー |
| **原本R2キー** | rich_text | **新設**。最長でも約70バイト |

「どの値も原本まで遡れる」は relation ではなく **sha256 + R2キーの文字列**で担保する。Notion 上でクリックして原本に飛べなくなるが、R2 は非公開バケットなので relation 先がメタ行であっても実効価値は同じ。機械（ローカルPG API / D1 / MCP）からは sha256 で完全に辿れる。

**「原本R2キー」には前提条件がある。** R2 の原本キーは `doc_id` セグメントを含むが、`models.RawArtifact` に `doc_id` フィールドが存在しない（`source/datatype/scope/data_date/fetched_at/url/local_path/sha256/size_bytes/license_tag/converted_paths/convert_status/notion_page_id` のみ）。`rawstore.raw_filename()` も `{source}_{datatype}_{scope}_{YYYYMMDD}.{ext}` で `doc_id` を持たない。実測で `data/raw` には `edinet_pdf_8306_20240729*` が **249件の別内容**として存在し（`edinet.py:181` の `scope=normalize_sec_code(code) or doc_id` が提出者コードを使うため）、`(scope, data_date)` では文書を一意に指せない。
→ **`RawArtifact.doc_id` の追加が済むまで「原本R2キー」列は作らない**（値を埋められないため）。「原本SHA256」列は先に作ってよい。

`provenance_properties()` の `include_raw_relation` 引数を廃し、`Provenance` に `raw_sha256` / `raw_r2_key` を追加する。`raw_page_id` は既存行互換のため当面残す。

---

### 4. ⑤ 原本ファイル — 凍結（DBは削除しない）

- `config.DB_REGISTRY` から `"raw_files"` キーを外す（9→8）。DB description に凍結の旨と移行先（R2 / D1 索引 / ローカルPG `raw_files`）を書く。
- backfill 済みの行と添付は失わないため**削除しない**。

**廃止根拠**
1. **1時間URL失効**。⑤の目的は「どの値も原本まで遡れる」ことだが、Notion ホストのファイルURLは外部サーバからの物理取得に使えない。人が UI でクリックする分には有効だが、機械（ローカルAPI / MCP / 再変換ジョブ）が取りに行けない時点で正本の要件を満たさない。
2. **拡張子 allow-list**。`NOTION_UPLOAD_EXTENSIONS` にない `.parquet` / `.xbrl` は `.zip` でラップしないと 400 になる（`file_upload.py` で実APIプローブ済み）。保存形式が Notion の都合で歪む。
3. **容量**。原本の年産はフル年実測で 4.4〜4.8 GB/年（2024年 4.822 GB / 2025年 4.428 GB）。TDnet PDF を入れると +3.4 GB/年。
4. **req**。1原本あたり create + send（+complete）+ 行作成 = 3〜4 req。現行8種の年産 1.9〜2.8万件で **年6〜11万 req**。

**切替順序の制約（重要）**: `DB_REGISTRY` から `raw_files` を外すと `settings.db_id("raw_files")` が `ConfigError` を上げる。`raw_files_schema()` / `file_upload.py` / `price_history._create_history_db` / 各ジョブの `ctx.upload_raw` / `supply_weekly._raw_exists` が参照している。**R2 `jp-stock-raw` への PUT が本番で成功していることを確認するまで外さない**（§9 第5段）。⑤を止めてから R2 が動かなければ、その期間の原本がどこにも残らない。

---

### 5. 残す各DBのプロパティ定義

#### ① 銘柄マスタ `stock_master` — 4,445行・月次

既存維持: 銘柄名(title) / 銘柄コード(rich_text・冪等キー) / 市場区分 / 33業種 / 17業種 / EDINETコード / 上場状態(checkbox) / 状態(select) / 上場日 / 上場廃止日 / 最終データ更新日。

| 追加 | 型 | 備考 |
|---|---|---|
| **銘柄種別** | select（内国普通株/ETF/ETN/REIT/PRO/外国株/優先株・出資証券） | D1 `core_stocks.instrument_type` と一致。母集団 3,818→4,445 拡張の事故（ETF 1321 の更新停止）を型で防ぐ |
| **D1 stock_id** | number | id 再採番禁止の人間可読な証跡 |
| **R2 日足キー** | rich_text | |
| **CF銘柄URL** | url | kabulab-cf 銘柄詳細への deep link |
| **ウォッチ** | checkbox | **人間所有** |
| **自分タグ** | multi_select | **人間所有** |
| **メモ** | rich_text | **人間所有** |

凍結（schema から外し Notion 上は残置）: 現行履歴DB ID / 履歴シャード番号 / 履歴行数 / 原本(relation)。

**人間所有列の保護（最重要の回帰リスク）**: `upsert.py:_set()` は「None も明示的な空値として送信する」完全置換セマンティクスで、`stock_master_properties()` はこれで全列を送る。人間所有3列を payload に含めた瞬間に全4,445銘柄ぶんが月次で消える。履歴ポインタ3列で既に同じ罠を回避しているので、同じ方式（**payload に含めない + 回帰テスト**）を適用する。

#### ② 株価テクニカル `prices` — 4,445行・毎営業日

**追加も削除もしない。** `PRICE_PROP_*` の全26列（始値/高値/安値/終値/前日比率%/出来高/売買代金/時価総額/52週高安/SMA5,25,75,200/SMA25乖離率%/RSI14/MACD3列/BB±2σ/ATR14/出来高25日平均比/PER/PBR/配当利回り%）をそのまま維持する。共通プロパティのみ v2 へ。

**② の列を維持するための入力条件（取込側への制約）**: `transform/technicals.py` は `sma75` に75本、`sma200` に200本、`week52_high/low` に `span_days >= 364`（暦日）を要求し、満たさなければ `None` を返す（推定禁止）。取込側で yfinance の `period` を短縮する場合、**compute_technicals の入力は短縮後の系列ではなく R2 マージ後の全系列**でなければならない。これを誤ると ②の SMA75 / SMA200 / 52週高安 が全銘柄で恒久的に空になり、推奨ビューが機能しなくなる。

**「データ品質」の選択肢を増やさない理由**: 母集団を 4,445 に拡張すると ETF/REIT/PRO には PER/PBR/ROE が存在しないが、`prices_daily._build_record` の品質判定は `close is None` / `sma25 is None` / `has_probable_split` の3条件だけで決まり、バリュエーションの欠損は `quality` に一切反映されない。したがって「対象外」という値を新設する必要はなく、該当行は `正常` のまま財務系プロパティが空になる。`DataQuality` enum を増やすとローカルPG と Notion select の両方に波及するので増やさない。

#### ③ 財務サマリ `financials` — 投入窓5年・約100,000行

既存プロパティ（タイトル/銘柄コード/決算期末/開示種別/連結単体/会計基準/売上高〜来期予想EPSの19 number 列/開示日/銘柄マスタ relation）をすべて維持。

| 追加 | 型 | 理由 |
|---|---|---|
| **書類管理番号** | rich_text | ④および D1 `jss_financials.doc_id` への結合キー。現状 ③から④へ辿る手段がない |

**投入窓**: `決算期末 >= 実行日 − 5年` の行のみ Notion へ書く。backfill も5年で止める。既存の古い行は消さない（剪定 req のほうが高い）。

#### ④ 開示書類 `disclosures` — 保持窓36ヶ月・約90,000行

既存プロパティすべて維持（開示タイトル/開示日時/書類種別/書類管理番号/銘柄コード/取得元URL/XBRL有無/分割比率/分割係数/効力発生日/銘柄マスタ relation）。共通プロパティのみ v2 へ。

**剪定**: `開示日時 < 実行日 − 36ヶ月` の行を月次で `PATCH /v1/pages/{id}` に `in_trash: true`。月あたり約2,500行 ≒ 33〜40分/月。
**剪定の開始条件**: §0 のとおり第6波（CF `ir_disclosures` への writer 移管 + `doc_id` バックフィル）完了後。それまで ④は正本なので窓を切らない。初回は保持行数が読めないため **1回あたり最大5,000行**の上限を設け、複数回に分ける。

#### ⑥ 時系列エクスポート →「⑥ データセットカタログ」（`exports` キーは維持）— 約20行

| プロパティ | 型 | 改廃 |
|---|---|---|
| データセット名 | title | 維持（冪等キー） |
| ~~ファイル~~ | files | **廃止**（`EXPORT_PROP_FILES`。現状 `.parquet.zip` + `.csv` を添付している） |
| **保管先** | select（R2 / D1 / ローカルPG） | 新設 |
| **場所** | rich_text | 新設 |
| **形式** | select（Parquet / CSV / JSON / 行） | 新設 |
| 対象期間 / 行数 / スキーマ説明 / 更新日 | — | 維持 |
| **サイズ(bytes)** | number | 新設 |
| **最新データ基準日** | date | 新設。D1 `jss_dataset_freshness.latest_data_date` のミラー |
| **唯一のwriter** | rich_text | 新設。二重書込を人が検知できるようにする |
| **取得手順** | rich_text | 新設 |

このDBが「Notion を開けば CF 全体の鮮度が一目でわかる」面になる。

#### ⑦ 収集ジョブログ `job_log` — 異常時のみ・年数百行

既存維持（ジョブ名/実行日時/ステータス/処理件数/失敗件数/失敗銘柄/GitHub Run URL/所要時間(秒)）。`JOB_STATUSES` の3値定義はそのまま残す。

| 追加 | 型 | |
|---|---|---|
| **失敗系統** | select（Cloudflare / Notion / ローカルPG / 収集元 / 不明） | 4系統書込になるのでどこで落ちたかを分ける |
| **対応状況** | select（未対応/対応中/対応済み/様子見） | **人間所有** |
| **対応メモ** | rich_text | **人間所有** |

**「⑦をD1に移すか」への回答: 両方置く。役割で分ける。**
- 全実行ログ（成功含む）の正本は D1 側（18ヶ月保持）。機械が鮮度と失敗率を出す面。
- Notion ⑦ は**異常のみ**。`write_job_log(status='成功')` は Notion の `create_page` を呼ばない。人が「何が落ちたか」を見て対応を書き込む面で、これは D1 にはできない。
- 「今日は全部通ったか」は ⑥の「最新データ基準日」で確認する。
- 副次効果: 失敗行だけが並ぶので見落としが起きない。

#### ⑧ 需給 `supply` — 作り変え: 1行=銘柄の最新断面・4,445行

現行の未コミット設計（1行=銘柄×ISIN×週、`SUPPLY_PROP_TITLE` が複合キー）は**採用しない**。JPX 日次化後は年104万行、日証金を足すと年210万行で**1年で 250,000 上限を突破する**。Notion 側が0行の今なら無償で作り直せる。

| プロパティ | 型 | 備考 |
|---|---|---|
| 銘柄コード | **title** | **冪等キー（複合キー title → 銘柄コード title へ変更）** |
| 銘柄名 / 代表ISIN | rich_text | 同一コードの複数証券は R2 per-code 側に全部入る |
| JPX基準日 | date | |
| 売残高 / 買残高 | number | |
| **売残前回比 / 買残前回比** | number | 「前週比」→「**前回比**」に改名（2026-09-28 の週次→日次化で意味が変わる） |
| 売残一般信用 / 売残制度信用 / 買残一般信用 / 買残制度信用（各前回比も） | number | 計8列 |
| 信用倍率 | number | 計算値（買残高÷売残高）。ソース=計算、タグは `inherit` |
| 日証金基準日 | date | 日証金は営業日更新で JPX と基準日が別 |
| 融資残高/融資新規/融資返済/貸株残高/貸株新規/貸株返済/融資残高(金額)/貸株残高(金額)/回転日数 | number | 日証金 `zandaka` |
| 逆日歩 | number | 日証金 `shina`。`*****`（マスク）は空欄にする（0に潰さない） |
| 貸借銘柄区分 | select | 日証金 `meigara`。値は生のコード（意味の解釈をしない） |
| 制限措置 | rich_text | 日証金 `seigenichiran`。**生表記のまま** |
| R2キー | rich_text | |
| 銘柄マスタ | relation→① | dual_property |
| 共通プロパティ | — | ソース=`JPX`（主）、ライセンスタグ=`personal-only` 固定 |

廃止（0行なので実害なし）: `SUPPLY_PROP_TITLE`（複合キー）/ `SUPPLY_PROP_DATA_TYPE` / `SUPPLY_PROP_FLAG`。

**来歴の非対称を明示する**: 1行に JPX と日証金の2ソースが混ざるため、共通プロパティの「ソース」select 1つでは表現しきれない。列名に「日証金」を冠し `日証金基準日` を別に持つことで来歴を列レベルで識別する。完全な来歴（申込日×取引所区分名ごとの行）は R2 per-code 側にある。この妥協は §10-B に記載。

コード変更: `upsert.supply_title()` / `supply_filter(title)` を廃し、`supply_filter(code)` を title equals = 銘柄コード にする。`load_supply_page_map()` の `data_date` スコープを廃し全件（4,445行）の `{code: page_id}` にする。`_SUPPLY_FIELD_TO_PROP` を全面差し替え。

#### ⑨ 株主優待 `yutai` — 約8,300行・月次

既存維持（タイトル(title・`{code} {優待品名}`)/銘柄コード/優待品名/ジャンル(select)/優待内容(rich_text・2,000字クリップ)/最低株数/権利確定月(multi_select)/取得元URL/銘柄マスタ relation）。

| 追加 | 型 | |
|---|---|---|
| **公開不可** | checkbox（**常に True** で送信） | 権限制御ではなく人間への警告表示 |

- ジャンルは D1 `yutai_genres.slug` 16種へのマッピング表をコード側に持つ（未知は `other`）。Notion 側は日本語のジャンル名のまま。
- 権利確定月は `multi_select`（`1月`…`12月`）のまま。D1 側は単数 integer なので**行展開は CF シンク側で行う**（Notion/ローカルPG は複数月のまま）。
- ライセンスタグ = `personal-only` **固定**。`licensing.SOURCE_LICENSE[Source.MINKABU] = PERSONAL_ONLY` は**変更しない**（規約の逐条確認の結果、緩める根拠が規約内に存在しない）。

**規約上の防止策（3段）**
1. 「公開不可」checkbox を全行 True（payload に固定値で含める。人間所有列ではない）。
2. ⑨DB の description に「本DBおよび親ページを Share to web / Publish to web にしてはならない（みんかぶ利用規約 第14条1項）」と明記。
3. データカタログの personal-only 節に同じ警告。

Notion ワークスペースが私的空間である限り S3（完全私的利用）の枠内だが、**"Publish to web" を1回押すと即座に S2（第14条1項に直撃）に転落する**。checkbox も description も権限制御ではない。

**prefetch の degrade（回帰リスク）**: 8,314行は 10,000件 silent truncation まで余裕が20%しかない。`load_yutai_page_map()` を新設し、**取得件数が 10,000 に達したら prefetch を諦めて per-record 検索へ degrade** する分岐を必ず入れる。既存規約どおり `page_resolved` は all-or-nothing（部分マップを True で渡すと重複行が毎月増える）。

---

### 6. データカタログページ — `ensure_catalog_page` → `sync_catalog_page`（全置換）

現行 `schema.ensure_catalog_page` は `setup.find_child_page(CATALOG_TITLE)` がヒットすると `logger.info("...内容は変更しない")` して `return existing` し、**本文を一切更新しない**。このため 2026-06-28 に J-Quants 廃止等の反映を Notion MCP で手作業する羽目になった。今回 ⑤凍結・履歴子DB廃止・⑧作り変え・正本のCF移管という大改訂が入るので、放置すると同じ手作業が再発する。

**修正仕様**
1. 既存ページを子ページのタイトル一致で発見（現行どおり）
2. `list_child_blocks(page_id)` で子ブロックを列挙し、**`DELETE /v1/blocks/{block_id}` で全件 in_trash 化**
3. `catalog_blocks()` の結果を `_BLOCK_CHUNK`(80) ずつ `append_block_children`
4. 先頭に生成マーカー: 「⚠ このページは `notion/schema.py` が毎回全置換で自動生成します。手動追記は子ページ『📝 運用メモ』へ。生成日時 / git SHA: …」
5. 避難先の子ページ「📝 運用メモ」を create-only で確保（こちらは絶対に触らない）

req: delete 約130 + append 2 = **約132 req/実行**。`master_sync`（月1）に載せる。

**全置換化の前に**、既存ページ本文を退避し、自動生成対象外の手動追記があれば「📝 運用メモ」へ移すこと。

**本文の改訂内容**（`_CATALOG_DICTIONARY` / `catalog_blocks`）
- 冒頭に「正本は Cloudflare（D1 / R2）。この Notion は人が見る・書くためのサブ面です」
- 「Notion が持つもの／持たないもの」の対照表（日足全履歴・XBRL全ファクト・需給日次履歴・原本の実体は Notion に無い、と所在つき）
- ⑤ と 履歴子DB を「凍結」節として記録（なぜ廃止したか・どこへ移ったか）
- personal-only の節に **Share to web 禁止**（②⑧⑨が該当）
- 利用者別クイックスタートを書き換え（開発者には Notion API を一次入口として案内しない）
- 出典表記に日証金を追加

ドリフト検知テスト `test_catalog_lists_all_schema_select_options` は維持し、対象定数に `INSTRUMENT_TYPES` / `FAILED_LAYERS` / `ACTION_STATUSES` / `EXPORT_STORES` / `EXPORT_FORMATS` / 新 `SOURCE_OPTIONS` を追加する。

`RECOMMENDED_VIEWS` は ⑤由来のビューを削除し、⑧（信用倍率降順）⑨（権利確定月グループ化）を追加する。

---

### 7. `client.py` に追加が必要なメソッド

現行 `NotionClient` には **archive / delete 系メソッドが1つも無い**（`query_database` / `get_page` / `retrieve_database` / `list_child_blocks` / `create_page` / `update_page` / `create_database` / `update_database` / `append_block_children` のみ）。

- **`delete_block(block_id)`** — `DELETE /v1/blocks/{block_id}`。カタログ全置換に必須。
- **`trash_page(page_id)`** — `PATCH /v1/pages/{page_id}` に `in_trash: true`。④の36ヶ月剪定に必須。`update_page()` は現在 `properties` のみを送るシグネチャなので、拡張するか別メソッドにする。

両方とも `dry_run` で `_record()` に落ちること、`_call(_retry_safe=...)` の扱い（DELETE は冪等なのでリトライ可）を既存規約に合わせる。

---

### 8. レート・行数・所要時間（レビュー再計算値）

実効スループット 1.04〜1.27 req/s を前提。**「CFのみ」と「CF + Notion」を必ず2列で示す**（Notion をサブに降格しても書き込みは続くため、片方だけの数字は成立しない）。

| ジョブ | 頻度 | Notion req | Notion 所要 | 現行 timeout | 推奨 timeout |
|---|---|---:|---|---:|---:|
| `prices_daily`（履歴廃止後・4,445銘柄） | 営業日 | 4,548 | **60〜73分** | 300 | **150** |
| `master_sync`（① 4,445 + カタログ132 + 原本UL） | 月1 | 約4,660 | **61〜75分** | 120 | 120 維持 |
| `supply_daily`（⑧ 4,445） | 営業日/週次 | 約4,556 | **60〜73分** | 300 | 120 |
| `yutai_monthly`（⑨ 8,314 + map 84） | 月1 | 約8,448 | **111〜135分**（+みんかぶ polite fetch 43分 = **154〜178分**） | 300 | **300 維持** |
| `edinet_daily`（⑤凍結後・繁忙日300書類×2req） | 営業日 | 約600 | 8〜10分（EDINET DL が律速） | 120 | 120 維持 |
| `tdnet_hourly`（⑤凍結後） | 平日毎時 | 約200 | 3〜4分 | 30 | 30 維持 |
| `export_weekly`（⑥ 約20行・添付なし） | 週1 | 約25 | <1分 | 120 | 60 |
| `reconcile_weekly` | 週1 | 0〜1 | — | 60 | 60 維持 |

`prices_daily` を 90分にしないこと。履歴廃止後の Notion だけで 60〜73分、CF側 17〜21分を足して 77〜94分になるため、90分では余裕がない。**150分**にする（実測が安定したら 120 へ）。
`yutai_monthly` を 180分にしないこと。154〜178分でマージンが 2〜26分しかない。**300 を維持**する。

**年間 Notion 行増**

| DB | 現行の年増 | 新の年増 | 5年後 |
|---|---:|---:|---:|
| ① / ② | 0 | 0 | 各 4,445 |
| ③ | 約20,000 | 約20,000（5年窓） | 約100,000（上限の40%） |
| ④ | 約30,000 | 約30,000（36ヶ月窓・剪定あり） | 約90,000（36%） |
| ⑤ | 1.9〜2.8万 | **0（凍結）** | 凍結時点の値 |
| ⑥ | 3〜5 | 0（約20固定） | 約20 |
| ⑦ | 約3,000 | 数百（異常のみ） | 数千 |
| ⑧ | 約1,040,000（日次化後・未実装） | **0（4,445固定）** | 4,445 |
| ⑨ | 0 | 0 | 約8,300 |
| ①配下 子DB | 1,089,025 | **0（全廃）** | 0 |

**母集団拡張の副作用**: 3,900 → 4,445 で ①②⑧ が各 +545 行、Notion 書込が約14%増える。上表はすべて 4,445 前提で計算済み。

**10,000件 silent truncation**: ③④は 10,000 行を超えるが、`_find_page` の**フィルタ付き**クエリ（`page_size=1, max_pages=1`）なので影響を受けない。①②⑧の全件マップは 4,445 行で下回る。**⑨だけ 8,314 で余裕が薄い**ので §5-⑨ の degrade 分岐を必ず入れる。
**⑥の生成元**: `export_weekly.export_notion_db_track_a()` は ③④ を Notion から全件クエリしている。③は5年で約10万行、④は約9万行。truncation されると**静かに1/10のエクスポートが出来上がる**。⑥の作り変えより**先に**生成元をローカルPG / D1 へ切り替えること。

---

### 9. 移行段階（CF 側の完成度に依存しない順）

| 段 | 内容 | CF依存 | 効果 |
|---|---|---|---|
| **第0段** | `prices_daily` の履歴子DB書込を停止（`--enable-history` へ反転）。`prices_daily.yml` の timeout を 300→150 | **なし。今日できる** | 日次 13,380 req 削減（75%） |
| 第1段 | `sync_catalog_page()` 化 + 本文改訂。⑤・履歴子DBの凍結を記録 | なし | 手作業カタログ更新の恒久解消 |
| 第2段 | ⑧を「1行=銘柄の最新断面」へ設計差し替え、⑨のジャンルマッピング・公開不可 checkbox・`load_yutai_page_map` の degrade（どれも未コミット・Notion 0行のうちに） | なし | 年104万行の上限突破を未然回避 |
| 第3段 | ⑦を異常時のみ記録へ。人間所有列を①⑦へ追加（回帰テスト同時） | なし | — |
| 第4段 | 共通プロパティに「原本SHA256」を追加（値は空のまま列だけ先に作る） | なし | 第5段の前提 |
| 第4.5段 | `RawArtifact.doc_id` 追加後に「原本R2キー」列を追加 | なし | §3 の前提条件 |
| 第5段 | R2 `jp-stock-raw` writer 稼働**後**、⑤の Notion 書込を停止・DB凍結・`DB_REGISTRY` 9→8 | **あり** | 年6〜11万 req 削減 |
| 第6段 | ⑥をデータセットカタログへ作り変え（生成元をローカルPG/D1 へ先に切替） | あり | 添付容量の解消 |
| 第7段 | ③の5年窓。**④の36ヶ月剪定は第6波（CF `ir_disclosures` 移管）完了後** | ④のみあり | 上限到達の回避 |

**第0段の費用対効果が最も高い**（CF 側が1行も存在しない今日の時点で、日次書込の75%が消える）。
**第5段だけは、R2 への PUT が本番で成功していることを確認してから**実行すること。

---

### 10. 未確定事項・意図的な妥協

#### A. 未確認（公式ドキュメント／実測で裏が取れていない）

| # | 事項 | 影響 |
|---|---|---|
| A1 | **Notion DB のプロパティ削除 API**。`update-a-database` の公式リファレンスを実取得したが削除方法の記載が**無い** | 設計を削除に依存させず、廃止プロパティ（履歴ポインタ3列・原本relation・⑧旧3列）は**凍結**にした。削除が必要なら Notion UI で人が手作業する |
| A2 | **プロパティのリネーム可否**（同ドキュメントに記載なし） | ⑧の「前週比」→「前回比」は Notion 側0行のうちに**作り直し**で対応する。既存行があるDBでのリネームは行わない |
| A3 | **日証金 CSV の実ヘッダ文字列と ⑧プロパティ名の1対1対応** | ⑧の日証金12列のプロパティ名が確定できない。実装時に cp932 ヘッダ原文と突合してから確定する。本書の「融資残高」「回転日数」等は暫定名 |
| A4 | **Notion ワークスペースの DB数・ブロック数の上限** | 既存の履歴子DB（最大3,900個）を残置する判断が上限を圧迫しないか未検証。圧迫するなら 3,900回の削除 ≒ 51〜62分 |
| A5 | **Notion 添付容量の上限と現使用量** | ⑤と⑥がどれだけ食っているか未実測。⑤凍結で増加は止まるが、既存分は削除しない限り解放されない |
| A6 | **③④の現在行数**（backfill 進捗次第） | 「③5年窓」「④36ヶ月剪定」の初回剪定量が読めない。1回5,000行の上限で分割する |
| A7 | **Notion データベースオートメーションを公開APIから設定できるか** | ⑦を異常時のみにしたことで「異常が出たら気づく」仕組みが必要になるが、API設定が不可なら GitHub Actions の失敗通知に依存する |
| A8 | **Workers プランが Free か Paid か** | Notion 側の設計には影響しないが、⑥に載せる D1 の保持期間（Paid=無期限 / Free=24ヶ月）の記述が変わる |

#### B. 引き受けるトレードオフ

| # | 妥協 | 代償 | 受け入れる理由 |
|---|---|---|---|
| B1 | ⑧で1行に JPX と日証金の2ソースを混ぜる | 「ソース」select 1つで来歴を表現できず、列名で識別する非対称な設計になる | 純粋に保つと1行=銘柄×データ種別で13,200行＝週次176分。4,445行なら60〜73分 |
| B2 | ⑤の relation を文字列キーに置換 | Notion 上でクリックして原本に飛べない | 添付URLは1時間で失効し、機械からはどのみち取得不能だった |
| B3 | ⑨にみんかぶ掲載文を残す | "Publish to web" を1回押すと S2 に転落する。checkbox も description も権限制御ではない | 掲載文なしの⑨は人間にとってほぼ無価値。ただし**これは規約準拠ではなく「最もリスクが低い形態」に留まる**（第7条(18) の取得禁止は残る） |
| B4 | ③④に窓を切る | 5年より古い決算、36ヶ月より古い開示が Notion から見えなくなる | 正本は D1 側。窓を切らないと④は10年で上限到達 |
| B5 | ⑦から成功ログを外す | 「ジョブが1回も走らなかった」ケースは⑦に何も出ない | ⑥の「最新データ基準日」で検知する。ただし**これはアラートではなく、人が能動的に見に行く必要がある** |
| B6 | 既存の履歴子DB・⑤の行を削除しない | 使われないデータが残り続ける（A4/A5 に繋がる） | 削除 req のコストと、誤削除で backfill 済みデータを失うリスクを避けた |

#### C. 本仕様で解決していないこと

1. **⑧⑨の未コミット実装が本仕様と乖離している。** `collectors/jpx_margin.py` / `minkabu_yutai.py` / `jobs/supply_weekly.py` / `yutai_monthly.py` と `tests/` は現行の⑧設計（1行=銘柄×ISIN×週、`supply_title()` の複合キー）を前提に書かれている。**コミット前に**書き直すべきで、先にコミットすると二重の書き換えが発生する。
2. **日証金コレクタが存在しない。** ⑧の日証金12列を埋める手段が現時点で無い。
3. **CF への書き込み層が1行も無い。** `.env` に `CLOUDFLARE_*` / `R2_*` が無い。本仕様は「CF writer が存在する」前提の上に Notion サブ面を設計したもので、その前提自体は取込仕様側の責務。
4. **stockStock は PUBLIC リポジトリ。** `collectors/minkabu_yutai.py` を公開リポジトリにコミットすることの是非（規約第7条(18)に該当すると結論が出ている取得行為の実装を公開すること）は、コミット前に一度判断が要る。**本書では判断しない。**
---

# 取込ジョブ

## 取込ジョブ 確定仕様（ジョブ別 書込先・cron・冪等性・所要時間）

対象: `/Users/satoki252595/projects/stockStock/src/jp_stock_pipeline/jobs/`、`collectors/`、`.github/workflows/`。
R2/D1 のスキーマと配置表は再掲しない。書込先はバケット名・プレフィックス名・テーブル名で参照する。

---

### 0. 着手前提条件（これが揃うまで実装に入らない）

| # | 前提 | 理由 | 状態 |
|---|---|---|---|
| P-1 | **Workers プランを Free / Paid で確定させる** | Free なら D1 1DB 500MB に対し新規分だけで5年 約507MB（§5）で超過し、`ir_disclosures` / `jss_raw_files` の24ヶ月保持縮退が「案」ではなく**必須条件**になる。また 004 の EMH 画面が `swing_daily_ohlcv` を全走査（336,185行/回）する問題が移行の全期間（4〜6ヶ月）続き、1日15回の画面表示で rows read 日次枠が枯渇する | **未確定**。`kabuMCP/wrangler.jsonc` が `limits.cpu_ms` / `send_email` / `ratelimits` を宣言して本番稼働している一方、`kabulab-cf/wrangler.toml` は「Paid を使わない」と明記しており矛盾している |
| P-2 | **`models.RawArtifact` に `doc_id` を追加する** | R2 の原本キーは `doc_id` セグメントを含み、`jss_raw_files.doc_id` が ④⇄⑤ の結合キーになる。現行 `RawArtifact` は `source/datatype/scope/data_date/...` のみで `doc_id` を持たない。`rawstore.raw_filename()` も `{source}_{datatype}_{scope}_{YYYYMMDD}.{ext}` で、実測 `data/raw` に `edinet_pdf_8306_20240729*` が **249件の別内容**として存在する（`edinet.py:181` の `scope=normalize_sec_code(code) or doc_id` が提出者コードを使うため。8306 は大量保有報告書の提出者）。`(scope, data_date)` では文書を一意に指せない。`doc_id` を `_` にフォールバックすると全EDINET原本で索引が NULL になり、原本まで遡る経路が原理的に作れない | **未実装** |
| P-3 | **D1 `kabulab-cf` と R2 の現使用量を実測する**（`wrangler d1 info`） | Free 枠はアカウント共有。kabuMCP・domain の使用分を知らずに P-1 の判断はできない | **未実測** |

補足: `rawstore.save_raw` の「2スロット」（`primary` と `{stem}_{sha8}.{ext}`）は衝突ではない。`alternate` が内容アドレスなので、別内容は各々が自分の `_{sha8}` 名を取る。`ValueError("SHA256 付き既存原本と内容が異なる")` は sha8 の短縮衝突時のみ発火する。ローカル保存は破綻していない。**破綻しているのは「ファイル名から文書を特定できない」ことだけ**であり、P-2 はそれを直す。

---

### 1. 書き込みレイヤ（4系統・階層化）

現行 `jobs/runner.py:_persist()` は Notion / ローカルPG の**対称な**双方向フェールセーフで、「どちらか一方にでも残れば成功」と判定している。正本が CF に移ると対称性は維持できない（正本が欠けたら再取得が必要だが、サブが欠けても次回実行で追いつく）。

| 系統 | 役割 | 失敗時 | カウンタ |
|---|---|---|---|
| **R2** | 正本（時系列・原本・派生） | **その取得単位を `ctx.failed`** | `cf_failed` |
| **D1** | 正本（断面・索引・イベント行） | 同上 | `cf_failed` |
| ローカルPG | ミラー（全文検索・分析） | warning のみ、収集は継続 | `mirror_failed`（既存） |
| Notion | サブ（人が見る面） | warning のみ、収集は継続 | `notion_failed`（既存） |

**⑤原本だけは例外。** 従来どおり「R2 / ローカル / Notion のいずれか1箇所に残れば構造化書込を続行」を維持する（`upload_raw` の `RawUploadError` セマンティクス）。ただし **R2 に置けなかった原本は `jss_raw_files` に行を作らない**（所在索引が嘘をつく状態を作らない）。

#### 1.1 段階的必須化（コード変更なしで締める）

- `CF_WRITE_MODE` = `off` | `shadow` | `live`（既定 `off`）
- `CF_REQUIRED_DATASETS`（カンマ区切り・既定 空）… ここに載っているデータセットだけ CF 失敗を `ctx.failed` に数える。載っていないものは warning（`cf_failed` には計上するが成否に影響させない）

#### 1.2 `JobContext.persist` の新契約

```
persist(record, notion_write, *, dataset, label)
  cloud_ok  = CloudSink.write(record, dataset)   # R2 と D1 の両方
  notion_ok = notion_write()                     # 例外を握って notion_failed
  local_ok  = self._mirror(...)                  # 既存
  判定:
    dataset in CF_REQUIRED → cloud_ok が False なら False
    それ以外               → (cloud_ok or notion_ok or local_ok)   # 従来互換
```

`CloudSink.write` は **R2 と D1 のどちらか片方だけ成功した場合 `False`** を返す（部分成功を成功と呼ばない）。「R2 に書けて D1 に書けなかった」は未索引オブジェクトを残すだけで破壊的ではないので、`reconcile_weekly` が突合して埋める。

#### 1.3 writer 二重稼働の検知 — R2 の 429 には依存しない

「同一キーへの並行書込 1/秒で 429 が出るから R2 側は気付ける」は**成立しない**。`daily/` は 4,445キー、`supply/` は 4,351キーに分散するので、2本の writer が別々の銘柄を処理していれば同一キーが同時に叩かれる確率はほぼゼロで 429 は出ない。R2 と D1 の防御強度に差はない。

したがって検知は以下の3段に統一する。

1. **トークン分離（唯一の物理的な保証）**: stockStock 専用の D1 / R2 トークンを新規発行する。旧 writer を止めると決めたら**トークンを revoke して物理的に書けなくする**（cron を止め忘れても安全）。R2 トークンはバケット単位でしかスコープできないため、`jp-stock-supply`（日証金 personal-only）は必ず別トークンにする。
2. **claim 照合**: `jss_writer_claims(dataset, column_group, writer, updated_at)` を各ジョブ冒頭で照合し、想定外の writer 名なら**異常終了**（黙殺しない）。**kabulab-cf 側にも同じ照合を入れる**（片側だけの規律にしない）。
3. **payload の writer フィールド**: mutable な R2 JSON は PUT 直前に GET 済み payload の `writer` が自分かを検証する。

#### 1.4 同一テーブルの複数 writer は「列集合が互いに素」のときだけ許す

| テーブル | 行の作成 + 基本列 | enrich 列（既存行の UPDATE のみ・INSERT/DELETE 禁止） |
|---|---|---|
| `ir_disclosures` | stockStock（第6波以降） | kabulab-cf: `tags` / `primary_tag` / `pdf_sentiment*` |
| `yutai_benefits` | stockStock | kabulab-cf: `estimated_value` / `short_summary` / `estimate_value_source` / `estimate_source_url` |

stockStock 側の UPSERT は `SET` 句を**ホワイトリストで列挙**する。`SELECT *` 起点の動的 UPSERT を禁止する（列が増えた瞬間に他 writer の列を潰す）。

---

### 2. ジョブ別 書込先・cron マトリクス

凡例: **正**=正本 / サブ=サブ系統 / ―=書かない

#### 2.1 `master_sync` — 月1・毎月1日 06:00 JST（`cron: "0 21 1 * *"` 現行維持）

| 出力 | 場所 | 区分 |
|---|---|---|
| EDINETコードリスト原本・変換版 | R2 `jp-stock-raw` の `raw/` / `derived/` | 正 |
| **JPX `data_j.xls` 原本**（新規） | 同上 | 正 |
| ①銘柄マスタ | D1 `core_stocks`（`ON CONFLICT(code) DO UPDATE`、**`id` を SET 句に入れない**） | 正 |
| 原本索引 | D1 `jss_raw_files` | 正 |
| ① | Notion ① / ローカルPG `stock_master` | サブ |
| 鮮度・ジョブログ | D1 `jss_dataset_freshness` / `jss_job_runs` | 正 |

**新規要件: 母集団拡張。** 現行は EDINET コードリストのみで、ETF/ETN/REIT/PRO/出資証券/外国株を含まない。R2 `daily/` の母集団 4,445 を供給するには JPX `data_j.xls` が必須。新コレクタ `collectors/jpx_universe.py` を追加し、「市場・商品区分」→ `instrument_type` へ写像する。**未知の区分は `None` にしてログに出す**（`equity` に倒さない）。`sector33` / `sector17` は data_j.xls 由来、`edinet_code` は EDINET 由来で、code でマージする。片方にしか無い銘柄は欠けた列を `None` にする（推定禁止）。

**ガード（`src/cron/universe.ts:82-120` の `assertUniverseCoverage` から移植。`cloud_store/universe_guards.py`）**: (a) JPX raw 行数 < 4,000 で中止、(b) 内国株式 < 3,000 で中止、(c) 既存 active の被覆率 < 98% で中止、(d) 1 run の対象外化が既存 active の 2% 超で中止。**(c)(d) の分母は `core_stocks` の total ではなく is_active=1 の件数**（実測 3,715）。

> なお `master_sync._detect_delistings` の `MIN_CODELIST_COVERAGE = 0.5` は **EDINET コードリストによる上場廃止検知**の閾値で、kabulab-cf の 0.98（`universe.ts:59`）とは別物。混同しないこと。0.5 を 0.98 へ引き上げるかは ① の writer 移管時に判断する。

**母集団拡張は2段に割る（重要）。** `core_stocks` を 3,818→4,543 に拡張した直後から、kabulab-cf の `daily.ts` が `is_active=true` の全件を処理対象にする。つまり切替が済んでいない状態で **725件**が**旧 writer**の処理対象に入り、yfinance 取得と D1 rows written が約1.19倍、PER/PBR/ROE を持たない 725行が `core_stock_financials` と `swing_stock_indicators` に NULL で積まれる。

→ **第1段: `instrument_type` 列の追加のみ**（既存3,818行に `equity` を埋める）。**第2段: +725行の INSERT を、R2 `daily/` の writer 交代の直前に行う。** あるいは拡張と同時に kabulab-cf の `daily.ts` の対象を `is_active=true AND instrument_type='equity'` に絞る改修を入れる。どちらにせよ承認項目に「旧 writer の処理対象が725件増えることの受諾」を含める。

#### 2.2 `prices_daily` — 毎営業日 19:30 JST（`cron: "30 10 * * 1-5"` 現行維持）

| 出力 | 場所 | 区分 |
|---|---|---|
| 日足10年 | R2 `vwap-data` の `daily/{code}.json` | **正** |
| 指数・為替・先物 | R2 `vwap-data` の `index/{slug}.json`（新設） | **正** |
| ②断面 | D1 `core_stock_financials` | **正** |
| yfinance 日足バッチ CSV 原本 | **R2 に置かない**（ローカル `data/raw` のみ） | ― |
| バリュエーション原本 JSON | R2 `jp-stock-raw` | 正 |
| ②断面 | Notion ② / ローカルPG `prices` | サブ |
| **②履歴（Notion 履歴子DB）** | **廃止**（`--enable-history` でオプトイン） | ― |

**確定処理順（これが仕様の核心）**

```
1. 対象コード解決（core_stocks.is_active=1 かつ instrument_type 非NULL）
   ① の Notion クエリは1回だけ行い、コード解決と relation マップに再利用する
2. yfinance 日足取得（period は 3 の経路が動くまで "2y" 据え置き、動いたら "3mo"）
3. R2 daily/{code}.json を GET（並列16）
4. date キーの Map でマージ（既存 bars を保持。後退禁止ガードを通す）
5. マージ後の全系列（最大10年）を compute_technicals の入力にする   ← ここが要
6. has_probable_split もマージ後系列の直近260営業日で判定
7. 分割検出銘柄のみ Ticker.splits を追加取得して splits を更新
8. R2 daily/ PUT + index/ PUT
9. D1 core_stock_financials upsert
10. ローカルPG prices upsert
11. Notion ② upsert（ベストエフォート）
```

**なぜ `period` を単純に `3mo` へ落としてはいけないか（実コード根拠）**: `transform/technicals.py` は `sma75` に75本、`sma200` に200本、`week52_high/low` に `span_days >= _WEEK52_CALENDAR_DAYS(364)`（暦日）を要求し、満たさなければ `None` を返す。`3mo` ≒ 62営業日 / 92暦日なので、**D1 に足す `sma200` / `week52_high` / `week52_low` と既存 `sma75` が全4,445銘柄で恒久的に NULL になる**。さらに `has_probable_split` の既定 `lookback=_WEEK52_TRADING_DAYS(260)` の窓が62本に縮み、3ヶ月より古い分割を永久に検出できなくなるため、「分割検出時のみ `Ticker.splits` を叩く」設計そのものが機能せず `splits` 配列が固定化する。転送量が 1/8 になるのは事実だが、②断面の半分と分割検出を代償にする。

**マージ後系列を入力にすれば `period` 短縮の代償はゼロになる。** `_ohlcv()` が `compute_technicals` に渡すのは `date/open/high/low/close/volume` のみで、`adj` は R2 用であってテクニカルの入力にしない（現行と同じ未調整終値ベースを保つ）。

**`prices` テーブルを投入経路にしてはならない**: `prices_daily.py` は `compute_technicals` の返す最新1本のスナップショットを1行だけ書くので実行日以降しか行が増えず、`local_store/schema.py` の `prices` に `adj` 列が無い。`collectors/yfinance_prices.py` には splits 取得が1行も無い（`yf.download` のみ）。

**母集団の罠**: R2 `daily/` の 4,444ファイルの母集団は `core_stocks`(3,818) ではなく `kabulab-cf/public/vwap-analysis/data/stocks.json`(4,445・ETF/ETN 466 + PRO 181 + REIT等63 + 外国株5 + 出資証券2 を含む)。**書く前に「対象コード集合 ⊇ R2 に既に存在する `daily/` のキー集合」を検査し、満たさなければ1件も書かずに異常終了する。** 満たさないまま書くと ETF 1321 が更新停止し、`株ラボ-新高値ブレイク検証`（日経平均代理に 1321 を使う）が直撃する。

**`daily/{code}.json` に `instrument` を入れない。** 007 の `/api/daily` は R2 JSON の素通し（`passthrough(o.body, 3600)`）なので、入れた値はそのまま無認証の公開 API に現れる。`instrument` は JPX `data_j.xls` 由来＝personal-only に分類される列であり、公開面に出すべきではない。母集団事故の防止は上記の writer 側の事前検査で担保する。追加してよいのは `schema` / `source` / `license` / `first_date` / `last_date` / `writer` / `bars[].adj_source` まで。

**`adj_source`** は `"yfinance-adjclose"` / `"close-fallback"` の2値で明示する（既存実装は `adjclose` が無いとき `c` と同値を `adj` に書いており、真の調整済みか代用かが区別できない。推定しない）。

**バリュエーション並列化は既定4から**: `fetch_valuation` は `VALUATION_SLEEP = 0.5` の逐次で、4,445銘柄で約81.5分。`--valuation-workers`（既定4、`1` で従来の逐次）を追加する。429 を食うかは未検証なので8から始めない。429 検知時は全ワーカーを 60 秒停止する（`RATE_LIMIT_WAIT` と同じ扱い）。

#### 2.3 `tdnet_hourly` — 平日 9:00–19:00 毎時（`cron: "0 0-10 * * 1-5"` 現行維持）

| 出力 | 場所 | 区分 |
|---|---|---|
| 一覧 JSON / HTML 原本 | R2 `jp-stock-raw` | 正 |
| **TDnet PDF 原本**（新規） | R2 `jp-stock-raw` | 正 |
| 短信 XBRL zip 原本・tidy 派生 | R2 `jp-stock-raw` | 正 |
| ③財務サマリ | D1 `jss_financials` | 正 |
| ⑧ファクト所在・語彙 | D1 `jss_xbrl_documents` / `jss_xbrl_elements` | 正 |
| 原本索引 | D1 `jss_raw_files` | 正 |
| ④開示メタ | D1 `ir_disclosures` | **第6波まで書かない**（§2.3.2） |
| ③④ | Notion / ローカルPG | サブ |

**TDnet PDF 原本の取得を追加する。** TDnet PDF は約31日で purge され、過ぎた開示の原本は二度と手に入らない。現行 `tdnet_hourly` は PDF を1件も取っておらず（`data/raw` 実測でも 0件）、収集開始が1日遅れるごとに1日ぶん永久に失われる。`--pdf-scope {all,high-signal,none}`（既定 `all`）。

容量: `ir_disclosures` の実測（37,338行 / 2023-06-05〜2026-09-10 = 1,193日 = 3.27年）から**年11,420件**。EDINET PDF の実測単価 339 KB（12.617GB / 37,205件。2024年に限れば 279 KB）を使い **年 3.4 GB**。

##### 2.3.2 `ir_disclosures` の writer 移管は第6波まで保留 — ただし④の正本を宙吊りにしない

`ir_disclosures` は `stock_id`(FK, notNull) / `tdnet_id`(notNull, **unique**) / `company_code` / `company_name` / `title` / `pubdate` / `document_url` / `tags`(JSON, notNull) がすべて NOT NULL。stockStock は `tdnet_id`（yanoshin 生 id）を保持しておらず、`DOC_TYPES` の12語彙は `classify.ts` の20タグと体系が違う。かつ `株ラボ-Youtube` が `tags LIKE '%"タグ名"%'` で検索しているので格納形式を変えられない。

**保留にすると「④の正本がどこにも無い期間」が数ヶ月生まれる**（D1 には旧 writer の行があるが stockStock の正本ではなく、Notion もサブに降格済み、という状態）。これを避けるため:

> **第6波完了までは「④開示メタの構造化正本 = Notion ④ + ローカルPG `disclosures`」と明記する。** Notion ④ のサブ降格と36ヶ月剪定の開始は第6波完了後。

加えて、`CloudSink` が `DisclosureRecord` を no-op で `True` にする実装のままだと「書けていないのに成功」と誤認するので、**no-op 時は `jss_dataset_freshness` に ④ の行を作らない**（鮮度が空のままであることで検知可能にする）。`CF_REQUIRED_DATASETS` に `ir_disclosures` を早まって入れない。

移管の前提条件3つ: (1) `collectors/tdnet_yanoshin.py` に `DisclosureRecord.yanoshin_id` を保持させる（現状 `doc_id` 導出後に捨てている）、(2) `classify.ts` の20タグ決定論分類を `transform/ir_tags.py` へ移植（**タグ文字列は1文字も変えない**。kuromoji ベースの `pdf_sentiment` は移植せず SET 句に入れない）、(3) 既存37,338行への `doc_id` バックフィル（`doc_id_from_document_url()` は純粋な文字列操作で HTTP 不要。導出できない行は `NULL` のまま残す）。

#### 2.4 `edinet_daily` — 毎営業日 21:00 JST（`cron: "0 12 * * 1-5"` 現行維持）

| 出力 | 場所 | 区分 |
|---|---|---|
| 書類一覧 JSON / XBRL zip / CSV zip / PDF 原本 | R2 `jp-stock-raw` の `raw/` | 正 |
| tidy 派生 Parquet / PDFテキスト txt | R2 `jp-stock-raw` の `derived/` | 正 |
| ③財務サマリ / ③年次断面 | D1 `jss_financials` / `core_stock_annual_financials` | 正 |
| ⑧ファクト所在・語彙 / 原本索引 | D1 | 正 |
| ④開示メタ | D1 | **第6波まで書かない** |
| ③④⑤ | Notion / ローカルPG | サブ |

kabuMCP への type5 ZIP ハードリンク連携（`--kabumcp-cache-dir`）はそのまま維持する（ローカルFS経由・CF とは独立）。

**未収集書類種別の扱い**: `--doc-types` オプションを追加し、**既定は現行の8種（120/130/140/150/160/170/350/360）のまま**にする。`extended` で 180/190（臨時報告書）・220/230（自己株券買付状況報告書）・240〜310（TOB）の④メタ + PDF 原本を追加する（XBRL の数値抽出はしない）。

理由: 拡張分は年 11,000〜15,000件 / 約4.1 GB（推定・未実測）で、これを常時入れると原本の年産が 12.1 GB/年になり、保守側の計画値 10.36 GB/年を超える。既定オフなら 8.0 GB/年で収まる。導入前に1日だけ `--dry-run` で実件数を測る。

TOB は `secCode` が取れないものが多く（公開買付者が非上場・外国法人・SPC）、`ir_disclosures.stock_id` は NOT NULL + FK なので構造化行を作れない。→ `secCode` が取れない TOB は **R2 原本 + `jss_raw_files` のみ**に保存し、構造化行を作らない。提出者名や書類名から対象会社を推定することは禁止。

#### 2.5 `supply_jsf`（新規）— 平日 11:30 JST（`cron: "30 2 * * 1-5"`）

日証金の4 CSV（`zandaka` / `shina` / `meigara` / `seigenichiran`）。すべて `https://www.taisyaku.jp/data/{name}.csv`、cp932、URL 固定、`Last-Modified` あり。

| ファイル | 行数 | プリアンブル行数 | `Last-Modified`（実測） |
|---|---:|---|---|
| `zandaka.csv` | 4,756（ヘッダ1 + 4,755） | **0**（1行目がヘッダ） | 10:50 JST |
| `shina.csv` | 1,035 | **3** | 10:30 JST |
| `meigara.csv` | 4,335 | **1** | 前日19:00 JST |
| `seigenichiran.csv` | 453 | **4** | 前日17:01 JST |

cron 根拠: `zandaka` の確報が 10:50、`shina` が 10:30。40分の余裕を取って 11:30 の1回で4本とも最新版が取れる（`meigara` / `seigen` は前日更新）。夕方の追加 run は不要。

**パース規約（実測で確定した罠）**
- **銘柄コードは4桁だが英数字混在**（`meigara.csv` に `130A` が実在）。`^[0-9A-Za-z]{4}$` で検証する。4桁数字を仮定しない（007 の `/api/margin` も同じ正規表現なので整合する）。
- `zandaka.csv` に **`速報／確報` 列がある**（実測は全4,755行が `確報`）。**`速報` 行は取り込まない**（確報で上書きされるため）。
- `zandaka.csv` の `制度信用・買残高株数` / `制度信用・売残高株数` は実測で**全行空**。空を 0 に潰さない。
- `shina.csv` の `当日品貸料率` / `当日品貸日数` / `前日品貸料率` に **`*****` のマスク値が実在**する。`int()` 失敗を 0 にフォールバックすると「品貸料率ゼロ」という嘘のデータが生まれる。**`None` にする。**
- `meigara.csv` の5列目のヘッダは `－`（全角ハイフン）という異常な列。
- ヘッダ行の文字列を実測値と完全一致で検証し、不一致なら `FetchError`（様式変更を無音で通さない）。
- `HEAD` で `Last-Modified` を見て前回取得時と同じならダウンロードごとスキップする。

書込先: 原本4本 → R2 `jp-stock-raw`、索引 → D1 `jss_raw_files`、断面 → D1 `jss_supply_latest`、Notion ⑧ はサブ。**`supply/{code}.json` はこのジョブでは触らない**（§2.6 で1回にまとめる）。**`vwap-data` には一切書かない**（源泉が別・ライセンスが別・公開Worker がバインドしている）。

#### 2.6 `supply_jpx`（`supply_weekly` を置換）— 平日 17:00 JST（`cron: "0 8 * * 1-5"`）

**現行の cron は誤りで、移行前に直す必要がある。** `.github/workflows/supply_weekly.yml` は `cron: "30 22 * * 5"` = **土曜 07:30 JST** だが、JPX の週末残高は一覧ページ記載のとおり「毎週第2営業日（火曜日）16:30」公表。最大4日遅れで、`--weeks 2` に救われているだけ。

**2026-09-28 の日次化が強制イベント。** 旧様式で取れる残りは **9/11申込分（9/15 火曜公表）** と **9/18申込分（9/25 金曜16:00 に例外公表）** の2回だけで、9/25申込分の週末残高は存在しない。一覧には直近約5週しか残らないため、取り逃すと永久欠測になる（2026-06-12〜07-31 が既に再取得不能なのと同じ状況が繰り返される）。

→ **`supply_daily` の実装が 9/25 に間に合わないなら、少なくとも現行 `supply_weekly.yml` の cron を火曜17:00 + 金曜17:00 に直して旧様式の残り2週を確保する。**

最終形は `cron: "0 8 * * 1-5"`（毎営業日 17:00 JST）の1行。火曜・金曜はその部分集合。一覧に新しいものが無ければ冪等スキップで即終了する。

**新旧両様式対応が必須。** 「9/28以降だけでよい」は不可。(a) 告知②（`t13vrt000001ixeg.pdf`、2026-07-06）の2ページ目に**「変更が延期となった場合」の代替スケジュールが公式に併記されている**（＝延期は現実的に起こりうる。可否は 9/27 20時ごろ JPX サイトで告知）、(b) 旧様式の残り2週を取り逃すと永久欠測、(c) 既存 `margin/` 10週は旧様式パーサの産物で再パースの可能性を残す必要がある。

**日付分岐を一切書かない。** 一覧の `.pdf` リンクを全部拾い、`syumatsu(\d{8})\d{2}\.pdf` に一致するものは `kind="weekly"`、一致しないものは**1ページ目のテキストを抽出して帳票名と「申込み現在 YYYY/M/D」で判定**する（新様式のファイル名は公式資料に記載がない）。判定できない PDF は**推測でパースせず `ctx.add_failure` で記録**する。

**パーサは数値トークン数で分岐する（最も危険な無音破壊への対策）。** 現行 `supply_weekly._build_records` は `**{name: getattr(row, name) for name in _SUPPLY_FIELD_NAMES}` で 12 フィールドへ順に割り当てる。新様式は上場比×2 + 取組比率×1 が増えて **15数値**になるため、そのまま流すと**例外も出さずに全ての値がずれる**。15→新様式 / 12→旧様式 / それ以外→その行を失敗として記録。

旧様式との構造差分: 行頭フラグが複数トークン（`B 規`, `B 規 株`）/ 銘柄名とコードの間に市場（プライム・スタンダード・グロース）と銘柄種別（制・貸・他）が挿入 / 数値 12→15 / 「前週比」→「**前日比**」/ ETF・ETN の上場比は `*`（`None` にする。0 に潰さない）。

`SupplyRecord` に追加: `section`（市場）/ `issue_kind`（制・貸・他）/ `unit_symbol` / `attrs`（規・日・監・株・喚・○ の生値リスト）/ `sell_listed_ratio` / `buy_listed_ratio` / `credit_ratio`。`data_type` は旧 `"信用残週末残"` / 新 `"信用残日次残"` で区別し、`sell_chg` の意味（前週比 / 前日比）が `data_type` から一意に決まるようにする。

**書込先を1回にまとめる**: `supply_jsf`(11:30) が R2 `raw/` と D1 断面だけを書き、**`supply_jpx`(17:00) が当日の JSF 原本を R2 から読み戻して JPX 分と合わせ、per-code の `supply/{code}.json` を1回で read-merge-write する。** これで R2 Class A が年213万→107万に半減する。JPX が失敗した日は `--part jsf-only-flush` で JSF 分だけ更新する。

##### `vwap-data/margin/` 互換シムの確定仕様

**rows の並び順規約を導入しない。PDF 出現順を維持する。**

理由: 007 の `/api/margin` は `const row = (snap.rows||[]).find(r => r.code===code); return row ? { week: w, ...row } : null;` で**最初の1行を返し、行の中身を丸ごとスプレッドする**。`ORDER BY code ASC, primary DESC, isin ASC` に並べ替えても、現行の出現順と一致する保証がない限り重複コード（実測 6コード: 2593 / 5076 / 7550 / 9201 / 9202 / 9434。9434 は3行でうち2行が buy/sell とも 0）で返る行が変わり、画面の信用残がゼロ表示になりうる。かつ `primary` の判定規則が設計文書間で矛盾している（「判定できないので全行 false」vs「PDF表記名が『普通株式』を含むかで判定」）。全行 false なら実質 code+isin 順で、PDF 出現順とは別物になる。

並び順を変えるなら、切替前に既存の直近週で「旧パーサ出力の `find()` 結果 == 新パーサ出力の `find()` 結果」を全コードで検証し、重複6コードが完全一致した場合にのみ採用する。本仕様では**採用しない**。

**rows の payload を1バイトも増やさない。** `rows[]` は `{code, sell, sell_chg, buy, buy_chg}` の5キーのみ。`isin` / `name` / `flag` / 制度・一般の内訳8列は**入れない**（スプレッドされて無認証の公開 API に出る。銘柄名と ISIN は personal-only な JPX PDF 由来。内訳を入れると payload が 5→17 フィールドで約3.4倍になり、`n=260` 指定時に効く）。これらは per-code 側（`jp-stock-supply`）にのみ持つ。

追加してよいのは**トップレベルのみ**: `schema` / `kind`（`weekly` | `daily`）/ `source` / `license` / `row_count` / `writer` / `order`（`"pdf"` = 並び順の来歴）。`week` は不変（文字列・トップレベル）で、日次化後は基準日を入れる。`weeks.json` は**ソート済み文字列の素の配列**のまま。意味の変化（週→基準日、前週比→前日比）は新設の `margin/index.json` に記録する。

**既存10週（2026-06-12〜）は読みも書きも削除もしない。** `cloud_store` に `delete_object` を実装せず、`vwap-data/margin/` に expiration ルールを設定しない。**バックアップコピーを CF 上に作らない**（同一内容が CF 上に2箇所になり、「同じデータを Cloudflare 上に二重に置かない」に直接違反する）。バックアップが要るなら CF 外（ローカル `data/raw` か端末B）に置く。

#### 2.7 `yutai_monthly` — 月1・毎月1日 08:30 JST（`cron: "30 23 1 * *"` 現行維持）

**頻度は上げない。** みんかぶ利用規約 第7条(18)(19)(44) に照らし、取得頻度を上げる正当化はできない。1.5秒間隔の polite delay も維持する。Phase 1（検索ページ全走査）+ Phase 2（1,670銘柄の個別ページ）で約1,700リクエスト × 1.5s = **約43分**。これは規約への配慮による意図的なコストなので短縮しない。

| 出力 | 場所 | 区分 |
|---|---|---|
| 全件まとめ JSON 原本 | R2 **`jp-stock-raw`**（`vwap-data` には置かない） | 正 |
| 原本索引 | D1 `jss_raw_files`（`license_tag='personal-only'`） | 正 |
| ⑨構造化行 | D1 `yutai_benefits` | 正 |
| ⑨ | Notion ⑨ / ローカルPG `yutai` | サブ |

**書込規約**
1. `record_months: list[int]` を単数 `record_month` へ**行展開**する（既存粒度を維持し 002 の権利月絞込を壊さない）。キーは `(stock_id, item_name, record_month)`。
2. ジャンル名 → `yutai_genres.slug`（16種固定）のマッピング表をコード側に持つ。未知は `other`。**`yutai_genres` テーブル自体は触らない。**
3. **`estimated_value` / `short_summary` / `estimate_value_source` / `estimate_source_url` を UPDATE の SET 句に含めない。** ローカルLLM（node-llama-cpp）+ 楽天市場API による**再取得不能な資産**で、`monthly.ts:83-89` が `isNotNull(estimatedValue)` でフィルタして `yutai_yield` → `otakara_stock_scores` を作っている。上書きすると 002 の並びが変わる。切替前に全件を JSON で R2 へ退避し、**`estimated_value` 非 NULL の行数を `jss_dataset_freshness` に毎回記録して、減ったら異常終了する**ガードを入れる（事後に気づく仕組みが手動比較しかないため）。
4. **`DELETE` を1回も発行しない。** 現行 kabulab-cf の `fetch-yutai-full.ts` は「既存削除 → クリーンインポート」だが踏襲しない。取得に出てこなくなった行は `data_date` が古いまま残す。優待廃止の断定は一次開示（TDnet「株主優待制度の廃止」）が所有する。差分（新規/更新/stale 件数）を `jss_job_runs` に記録する。
5. `core_stocks.is_yutai` の再導出は kabulab-cf の `monthly.ts` に残す（責務を1つに絞って二重 writer を作らない）。
6. `description`（掲載文）は D1 に書くが `license_tag='personal-only'` を必ず付ける。**公開面から外すのは kabulab-cf 側 `app.ts:680` の是正**であり stockStock の責務ではないが、**writer 切替と同じ工程でやらないと規約違反（S2）を新基盤へ引き継ぐ**ので依存タスクとして管理する。
7. `licensing.SOURCE_LICENSE[Source.MINKABU] = PERSONAL_ONLY` は**変更しない**。

#### 2.8 `export_weekly` — 週1・日 09:00 JST（`cron: "0 0 * * 0"` 現行維持）

| 出力 | 場所 | 区分 |
|---|---|---|
| トラックA 財務 / 開示 Parquet | R2 `jp-stock-raw` の `export/{date}/` | 正 |
| 最新ポインタ `export/latest.json` | 同上 | 正 |
| **トラックB 株価** | **R2 に置かない** | ― |
| ⑥ | Notion ⑥（メタ行のみ。添付は廃止） | サブ |

**トラックBを R2 に置かない。** `export_prices_track_b` の中身は `fetch_daily_batch(period="5y")` の long 形式で、R2 `daily/` と**同一粒度・同一期間**。5年×4,445銘柄 = 5.45M行、Parquet 0.11〜0.15 GB/週、180日保持で26世代 = **2.9〜3.9 GB** の純粋な重複になる（配置表の容量表にこの行が存在しない未計上分でもある）。
→ `export/latest.json` に `daily/` プレフィックスのポインタ（キー一覧と各 `last_date`）を書くだけにする。

**入力経路を変える。** 現行は `fetch_daily_batch(period="5y")` を毎週再取得している（39チャンク・数百MB）。→ **R2 `daily/{code}.json` を並列 GET して Parquet 化する**。yfinance への週次フル再取得が丸ごと消える。
トラックAは Notion 全走査（`_track_a_filter()` → `query_database`）をやめ、**D1 から `license_tag` で絞ってページング**する。Notion のままだと ③が5年で約10万行、④が約9万行になり、10,000件 silent truncation で**静かに1/10のエクスポートが出来上がる**。

CSV は R2 に置かない（CSV 9.02 GB vs Parquet 2.68 GB）。Notion ⑥ 添付用にローカル一時ファイルとしてのみ生成する。

#### 2.9 `reconcile_weekly` — 週1・土 09:00 JST（`cron: "0 0 * * 6"` 現行維持）— 役割変更

stooq は PoW を返し実質死亡（`stooq_enabled` 既定 `False`）で、現状このジョブは即 return する。→ **「CF 正本の整合チェック」へ役割を変更する。**

1. **R2↔D1 の所在整合**: `raw/` を `ListObjectsV2` で列挙し、`jss_raw_files` に無いキー（未索引オブジェクト）を INSERT で埋める。逆に D1 にあって R2 に無い行（索引の嘘）は `quality=要確認` を立てる。
2. **鮮度の突合**: R2 `daily/{code}.json` の `last_date` と D1 `core_stock_financials.data_date` の一致率。不一致率が 1% を超えたら `ctx.add_failure`。
3. **writer の二重稼働検知**: R2 各 JSON の `writer` と D1 `jss_dataset_freshness.writer` / `jss_writer_claims` を突合し、想定外の writer 名を見たら**異常終了**。切替期に kabulab-cf の旧 writer が止まっていないことを検出する手段。
4. **母集団の突合**: R2 `daily/` のオブジェクト数と D1 `core_stocks` の active 行数。
5. 結果を D1 `jss_dataset_freshness` / `jss_job_runs` に書く。

`STOOQ_ENABLED=true` になったら従来の②終値突合も併せて実行する（コードは残す）。

---

### 3. 冪等性

#### 3.1 R2 — immutable キー（`raw/` / `derived/`）

キーに SHA256 先頭16hex が入るので物理的に上書きが起こらない。

1. ローカルで `rawstore.sha256_bytes(content)` を計算
2. D1 に `SELECT sha256 FROM jss_raw_files WHERE sha256 IN (?, …)` で**まとめて**照会（bind 100/文 → 100件/文）
3. ヒットしなければ R2 `PutObject`。メタデータに `x-amz-meta-fetched-at` / `-url` / `-license` / `-writer`
4. **R2 PUT が成功してから** D1 `INSERT ... ON CONFLICT(sha256) DO UPDATE SET last_fetched_at = ?`
5. ヒットした場合は PUT をスキップし `last_fetched_at` だけ UPDATE

**順序の理由**: D1→R2 の順にすると「D1 に行があるのに R2 にオブジェクトが無い」＝索引が嘘をつく状態を作る。R2→D1 なら最悪でも「未索引オブジェクト」で、無害かつ `reconcile_weekly` が拾える。

条件付き PUT（`If-None-Match`）には依存しない（未確認）。同一キーへの同一内容 PUT は冪等なので競合しても実害がない。

**`fetched_at`（秒精度）をキーに入れない。** 同一内容を別日に再取得するたびに別オブジェクトが増え、ストレージ課金が実データの数倍になる。取得イベントは D1 の `first_fetched_at` / `last_fetched_at` に記録し、オブジェクトは内容1つにつき1つにする。

**未解決の重複経路（明記）**: TDnet 一覧 JSON の毎時取得（平日11回/日）は、同一 `data_date` について増分だけ異なるスナップショットを最大11オブジェクト生成する（sha256 が違えば別キー）。実測でもローカルに `tdnet_tdnet_list_*` の sha8 付きが23件、`edinet_documents_list_ALL.json` の変種が **780件**発生している。上記の設計は「同一内容の別日再取得」しか解いておらず、**「増分だけ異なる同日再取得」を解いていない**。一覧系（`documents_list` / `tdnet_list`）については「その日の最終版だけを残す」か「全スナップショットを残す」かを別途決める必要がある。本仕様では**決めない**。

#### 3.2 R2 — mutable キー（`daily/` / `index/` / `supply/` / `margin/` / `weeks.json`）

read-merge-write（既存 JSON を GET → `date`/`ts`/`d` をキーにした Map にマージ → 昇順で全置換 PUT）。同じ入力を2回流せば同じ結果になるので冪等。

**後退禁止ガード（全ての merge PUT に必須）** — 1つでも満たさなければ **PUT せず失敗として記録**する:

- 既存オブジェクトの GET が **`NoSuchKey`/404 以外**のエラーで失敗した → PUT しない。「無かったこと」にして空から書き直すと 10年履歴が1ヶ月に化け、0.694 GB のデータと外部2リポジトリの分析が消える
- `len(new.bars) < len(old.bars)` → PUT しない
- `new.first_date > old.first_date` → PUT しない
- `old.writer` が自分以外 → PUT しない
- 契約必須キーが1つでも欠けた → PUT しない
- **配列型の全置換 PUT は要素数が減ったら拒否する（汎用ルール）**。`weeks.json` / `margin/index.json` / `supply/{code}.json` の各 `series` に適用する。`weeks.json` は要素数が少ない（11→将来250程度）ので、PUT 前に**既存全要素が新配列に含まれることを集合として検証**する。007 の `/api/margin` は `weeks.json` を唯一の入口にしており（無ければ即 `weeks:[]` を返す）、これを空にすると R2 のオブジェクトが残っていても画面上はデータが消えたのと同じになる

**同時実行**: (a) GitHub Actions の `concurrency: group`（現行 workflow で設定済み・維持）、(b) writer 宣言の照合、(c) GET 時の ETag を保持し PUT 直前に `HeadObject` で再確認して変わっていたら再読込リトライ（最大3回。`If-Match` 付き PUT の可否は未確認なのでベストエフォート）。

#### 3.3 D1 — upsert

全テーブル `INSERT ... ON CONFLICT(...) DO UPDATE`。

- `core_stocks` → `ON CONFLICT(code)`。**`id` を SET 句に絶対に入れない**。`TRUNCATE` / `DELETE` / `DROP` を発行しない（**14個**の子テーブル、うち cascade 11 本が消える）
- `core_stock_financials` → `ON CONFLICT(stock_id)`
- `jss_financials` → `ON CONFLICT(code, fiscal_period_end, disclosure_type)`
- `jss_raw_files` → `ON CONFLICT(sha256)`
- `jss_supply_latest` → `ON CONFLICT(code, data_type)`
- `yutai_benefits` → `ON CONFLICT(stock_id, item_name, record_month)`。保護4列を SET 句から除外
- `ir_disclosures`（第6波以降）→ **`doc_id` と `tdnet_id` の両キーで冪等化**。SQLite の UNIQUE は NULL を重複許容するので `ON CONFLICT(doc_id)` だけでは既存の `doc_id IS NULL` 行に当たらない。実装は「対象日の行を索引レンジで一括 SELECT → メモリで突合 → INSERT / UPDATE を振り分け」にする（per-row SELECT は走査行課金と往復の両方で不利）

**バッチング（公式 limits）**: bind 100/文 → `core_stock_financials`（約32列）なら 3行/文。SQL 文長 100,000 bytes。REST `/query` は複数文のバッチ実行を受けるので 1 HTTP に 50文 = 150行。**数値・検証済みコード・ISO日付だけの行はリテラル埋め込みでバルク INSERT**、テキスト列（銘柄名・タイトル・優待掲載文）を含む行は**必ず bind パラメータ**。この使い分けを `cloud_store/d1.py` の1箇所に閉じ込め、リテラル経路がテキスト列を受けたら `ValueError` を上げる。D1 は 1DB が単一スレッドで逐次処理するので HTTP 並列度は 2〜4 に留める。`batch` が原子的かは未確認なので、原子性に依存しない設計（全部 upsert・部分適用されても次回 run で収束）にする。

#### 3.4 degrade の具体

| 失敗箇所 | 挙動 |
|---|---|
| R2 PUT 失敗（原本） | ローカル/Notion に原本が残れば構造化は続行。**`jss_raw_files` には書かない** |
| R2 PUT 失敗（正本の時系列） | `dataset` が `CF_REQUIRED_DATASETS` にあれば `ctx.add_failure`、無ければ warning |
| R2 GET 失敗（merge の読み側） | **書かない**（§3.2）。`NoSuchKey` のみ空から開始 |
| D1 書込失敗 | 同上。R2 は書けているので次回 run か `reconcile_weekly` が索引を埋める |
| writer 名が想定外 | **異常終了**（黙殺しない） |
| Notion / ローカルPG 失敗 | `notion_failed` / `mirror_failed` に計上して warning。収集は継続（現行どおり） |
| CF 資格情報が未設定 | `CF_WRITE_MODE=off` と同じ扱い。Notion + ローカルPG のみで完走（現行と同じ挙動） |

既存の「双方向フェールセーフ思想」の核（片系統の障害で収集を止めない・失敗を隠さず計上する・ダミーで埋めない）はそのまま引き継ぐ。

---

### 4. 所要時間試算

**必ず「CFのみ」と「CF + Notion」の2列で示す。** Notion はサブに降格しても書込は続くため、片方だけの数字は成立しない。Notion の実効スループットは 1.04〜1.27 req/s。

#### 4.1 現行 `prices_daily` の内訳（コードから導出・4,445銘柄）

| 項目 | Notion req |
|---|---:|
| ①全走査（`page_size=100`）+ ①状態マップ + ②page マップ | 135 |
| ②upsert | 4,445 |
| 履歴 `_find_page` / create / `write_history_pointer`（各4,445） | 13,335 |
| 原本UL・⑦ | 13 |
| **合計** | **17,928** |

→ **3時間55分〜4時間47分**。3,900銘柄で計算すると 15,692 req = 3h26m〜4h11m となり、`prices_daily.yml` の `timeout-minutes: 300` の根拠と一致する。**現行の所要時間はほぼ100% Notion のレート制限で決まっている。**

#### 4.2 CF 化後の `prices_daily`（4,445銘柄）

| 工程 | 所要 | 根拠 |
|---|---:|---|
| yfinance 日足 `period=3mo`（45チャンク） | 7.5分 | 45 × (download ~6s + `CHUNK_SLEEP_RANGE` 平均3.5s)。download 単価は未実測 |
| バリュエーション（並列4） | 10.2分 | 4,445 × 0.55s ÷ 4 |
| バリュエーション（逐次・従来） | 81.5分 | `VALUATION_SLEEP=0.5` |
| R2 `daily/` の GET+PUT（並列16） | **2〜4分** | 1ファイル 156 KB（0.694 GB / 4,444）。GET 0.694 GB + PUT 約0.72 GB = **1.41 GB/日**。100 Mbps で 113s、50 Mbps で 226s |
| R2 `index/` 6 slug | 数秒 | |
| D1 `core_stock_financials` 4,445行 | 12秒 | 150行/req → 30 req |
| ローカルPG 4,445行 | 10秒 | `execute_batch` |
| Notion ② upsert + マップ + 原本UL + ⑦ | **60〜73分** | 4,548 req（①クエリを1回に統合した場合） |

| 構成 | 所要 | 現行比 |
|---|---:|---|
| 現行（Notion 正本 + 履歴子DB） | 3h55m〜4h47m | — |
| **(a) 履歴子DB廃止 + Notion ②断面は毎日書く（本番想定）** | **77〜94分** | **▲67%** |
| (b) Notion を書かない（CF + ローカルPG のみ） | **20〜25分** | ▲91% |
| (c) (b) だがバリュエーション逐次 | 1h32m | ▲67% |

**履歴子DB を廃止するだけで Notion リクエストが 17,928 → 4,548（75%減）になる**のが最大の効き目。

注: 設計文書にある「R2 RMW 2.9 GB/日」は GET/PUT を二重計上している。実データ量は 1.41 GB/日。所要をレイテンシ（`ops × 0.2s ÷ 16`）ではなく帯域から出すべきという指摘は正しく、帯域ベースでも 2〜4分に収まる。

#### 4.3 全ジョブ

| ジョブ | CFのみ | CF + Notion（本番） | 律速 | 現行 timeout | 推奨 timeout |
|---|---:|---:|---|---:|---:|
| `master_sync` | 3〜5分 | **61〜75分** | Notion ①4,445 + カタログ132 | 120 | 120 維持 |
| `prices_daily` | 20〜25分 | **77〜94分** | Notion ②4,445 | 300 | **150** |
| `tdnet_hourly`（⑤凍結後） | 2〜4分 | 5〜8分 | 短信XBRL/PDF の DL | 30 | 30 維持 |
| `edinet_daily`（⑤凍結後） | 60〜80分 | 68〜90分 | **EDINET API の DL**（type5+type2 の2DL/書類）。CF化では減らない | 120 | 120 維持 |
| `supply_jsf`（新規） | 2分 | 2分（Notion 書かない） | CSV 4本 DL（計 1.17 MB） | — | 30 |
| `supply_jpx`（新規） | 5〜8分 | **65〜80分** | Notion ⑧4,445。PDF 86ページのパース ~30s | 300 | 120 |
| `yutai_monthly` | 45〜50分 | **154〜178分** | **みんかぶ polite delay 43分**（規約上短縮しない）+ Notion ⑨8,448 req = 111〜135分 | 300 | **300 維持** |
| `export_weekly` | 4〜6分 | 5〜7分 | R2 `daily/` 4,445 GET + Parquet 化 | 120 | 60 |
| `reconcile_weekly` | 3分 | 3分 | R2 `ListObjectsV2` + D1 突合 | 60 | 60 維持 |

**`prices_daily` を 90分にしない**（77〜94分で余裕がない）。実測が安定するまで 150分。
**`yutai_monthly` を 180分にしない**（154〜178分でマージンが 2〜26分）。現行の 300 を維持する。下げるなら Notion ⑨の書込をジョブ本体から切り離して別 workflow にするのが先。

#### 4.4 D1 の初回一括投入

| 対象 | 行数 |
|---|---:|
| `core_stocks` / `core_stock_financials` | 8,890 |
| `core_stock_annual_financials` | 44,450 |
| `jss_financials` | 100,000 |
| `jss_raw_files` | 70,463 |
| `jss_xbrl_documents` | 33,257 |
| `jss_xbrl_elements` | 80,000〜120,000 |
| `yutai_benefits` | 8,314 |
| `ir_disclosures` の `doc_id` バックフィル UPDATE | 37,338 |
| **合計** | **383,000〜423,000** |

Free（100,000 rows written/日、日常ジョブ約9,000 + `tdnet_hourly` と同じ枠）で **5〜6日に分割**が必須。Paid なら1回。
`jss_xbrl_elements` を「約2万行」と見積もると4〜6倍の過小になる。converted CSV 600件サンプルで distinct element が 7,677、Heaps' law の β = log(7677/2114)/log(10) = 0.560 で 33,257doc へ外挿すると約72,700、5年41,600doc で約82,000。TDnet XBRL と提出者独自拡張を足すと10万超。

**writer 自身の D1 rows read（未計上だった分）**: `code → core_stocks.id` の解決マップ（`SELECT id, code FROM core_stocks` = 4,445行の全走査）を各ジョブ冒頭で張るので、`prices_daily` 1 + `edinet_daily` 1 + `tdnet_hourly` 11 + `supply` 2 = 15回/日 × 4,445 = **約66,700 rows read/日**。Free 5M/日の 1.3%。

---

### 5. 容量・コスト（レビュー再計算）

すべて 2026-09-11 に `data/raw` を `stat -f %z` で実測、または実測からの外挿。

#### 5.1 実測ベースライン

原本: zip 32,451 / 1.854 GB、pdf 37,205 / 11.750 GB、json 807 / 0.236 GB。
変換版: csv 33,258 / 9.021 GB、parquet 33,257 / 2.677 GB、txt 37,205 / 4.575 GB。合計 30 GB。

**年別の原本のみ**（`_converted` を除外）: 2022年 2,847件 / 0.245 GB、2023年 11,416 / 1.156、2024年 27,786 / **4.822**、2025年 19,022 / **4.428**、2026年(8.5ヶ月) 9,392 / 3.191（年換算 13,260 / 4.505）。

→ **現行8種のフル年実績は 1.9〜2.8万件 / 4.4〜4.8 GB/年。** 設計にある「3.46 GB/年」はバックフィルの薄い2022–2023を含む4年平均でフル年より25〜30%低く、「年7万件」は 70,463（4年総数）を年産として使った誤りである。

#### 5.2 原本の年産（確定）

| 内訳 | 年産 |
|---|---:|
| 現行8種 | 4.4〜4.8 GB |
| TDnet PDF（年11,420件 × 0.30 MB） | 3.4 GB |
| **小計（既定構成）** | **約8.0 GB/年** |
| EDINET 拡張種別（`--doc-types extended`・推定） | +4.1 GB |
| 合計（拡張あり） | 12.1 GB/年 |

保守側の計画値 10.36 GB/年 に対し、**拡張ありは超過する**。→ `--doc-types` の既定をオフにする根拠。

#### 5.3 R2 5年容量

| 項目 | 5年 |
|---|---:|
| `raw/`（既存13.8 + 8.0×5） | 53.8 GB（拡張ありなら 74.3 GB） |
| `derived/` parquet（2.677 + 0.67×1.4×5） | 7.4 GB |
| `derived/` txt（4.575 + 1.14×1.35×5） | 12.3 GB |
| `daily/` | 1.04 GB |
| `intra/`（**未実測・仮**） | 2.0 GB |
| `margin/` 互換シム | 1.2 GB |
| `supply/` per-code | 1.09 GB |
| `export/`（トラックA のみ） | 0.3 GB |
| **合計（既定構成）** | **約79 GB** |
| 合計（EDINET 拡張あり） | 約99.6 GB |

トラックBを R2 に置かない判断で **2.9〜3.9 GB** 減る。

#### 5.4 R2 コスト

| ケース | 既定構成（79 GB） | 拡張あり（99.6 GB） |
|---|---:|---:|
| 全 Standard | (79−10)×$0.015 = **$1.04/月**（$12.4/年） | **$1.34/月**（$16.1/年） |
| `raw/`・`derived/` を90日後 IA | 73.5×$0.01 + 残 5.5 GB は Free 枠内 = **$0.74/月** | **$0.94/月** |

IA を使う場合、`daily/` `intra/` `supply/` には適用しない（日次上書きされるため最低保存期間30日と取得課金で不利）。IA 遷移そのものが Class A としてカウントされるか、および IA からの配信時の retrieval $0.01/GB は**未計上**。

#### 5.5 R2 操作回数

**Class A（書込）**

| 経路 | 年間 |
|---|---:|
| 原本 PUT + derived PUT | 約10〜13万 |
| `daily/`（4,445 × 245営業日） | 1,089,025 |
| `index/` | 1,470 |
| `supply/`（**JSF と JPX を1回にまとめて半減後**） | 1,066,000 |
| `margin/` / `export/` / `reconcile` の List | 約3,800 |
| **合計** | **約228万/年 = 月19万** |

Free 枠 1,000,000/月 の **19%**。**Class A は $0**。
注: 設計の容量表にある「supply 2,131,990」は §2.6 の半減決定が反映されていない二重計上。

**Class B（読出）** — 設計は「未計上・要監視」としているが、writer 自身が確定的に発生させる量は計算できる:
`daily/` の merge GET 1,089,025 + `supply/` の merge GET 1,066,000 + `export_weekly` の `daily/` GET 231,140 + `reconcile` の GET 231,140 + margin 245 = **年262万 = 月21.8万**。Free 10M/月の 2.2%。課金は $0 だが、公開Worker 分がこれに加算される。

#### 5.6 D1 容量

索引とページ充填率を逐行で積み直した5年後の**新規分**:

| テーブル | 行数 | 1行 | 素データ |
|---|---:|---:|---:|
| `jss_raw_files` | 315,000 | 650 B | **205 MB** |
| `ir_disclosures` | 190,000 | 875 B | **166 MB** |
| `jss_financials` | 100,000 | 350 B | 35 MB |
| `jss_xbrl_elements` | 80,000〜120,000 | 350 B | 28〜42 MB |
| `yuho_*`（既存3本） | 59,700 | 300 B | 18 MB |
| `jss_xbrl_documents` | 41,500 | 285 B | 12 MB |
| `core_stock_annual_financials` | 44,450 | 250 B | 11 MB |
| `yutai_benefits`（`description` 掲載文全文） | 8,314 | 1,200 B | 10 MB |
| `jss_supply_latest` / `core_stocks` / `core_stock_financials` / `jss_job_runs` / swing・rsi・otakara 残置 / その他 | — | — | 約15 MB |
| **合計** | | | **約507 MB** |

`jss_raw_files` の「1行200 B」は3倍の過小。`r2_key`（実キー例 `raw/edinet/pdf/2024/2024-07-29/8306/S100U1XX/xxxxxxxxxxxxxxxx.pdf` = 65 B）+ `derived_key` 69 B + `sha256` 64 B の3列だけで 198 B あり、加えて PRIMARY KEY の暗黙索引 + 3明示索引で約200 B。

| プラン | 1DB 上限 | 5年後 | 判定 |
|---|---|---:|---|
| Workers Paid | 10 GB | 507 MB = **5.1%** | 余裕。ボトルネックは容量ではなく走査行数 |
| Workers Free | 500 MB | 507 MB = **101%** | **超過する** |

Free の縮退（`ir_disclosures` と `jss_raw_files` を直近24ヶ月保持にし、超過分を R2 の索引 Parquet へ退避）で ir 63 MB + raw 78 MB → **約280 MB（56%）**。つまり Free では縮退は「案」ではなく**必須条件**。加えて D1 `kabulab-cf` の現使用量が未実測なので、この 507 MB は新規分の話であり、**現使用量を測らない限り Free / Paid の判断そのものができない**（前提条件 P-3）。

#### 5.7 XBRL ファクト総数

converted CSV を600件サンプルした平均は 1,061行/doc・359 KB/doc。33,257doc × 1,061 = **3,528万ファクト**、年産 **883万**（設計の 3,190万 / 792万 に対し約11%の過小）。D1 に置かない判断は変わらない。

---

### 6. 実装順序

| # | 作業 | 検証 |
|---|---|---|
| 0 | 前提条件 P-1〜P-3 を潰す | — |
| 1 | `cloud_store` 層（d1 / r2 / keys / sink / contracts / guard）+ 単体テスト | モックで冪等性・bind分割・後退禁止ガード・保護列の SET 句除外を検証 |
| 2 | Secrets / Variables 登録。`CF_WRITE_MODE=off` のまま全 workflow に env 追加（**`ci.yml` には1つも足さない**。PUBLIC リポ + `pull_request` トリガー。先頭に禁止コメントを置く） | 既存挙動が1ミリも変わらないこと |
| 3 | `_schema/{prefix}.json` を R2 へ投入。**現行の実オブジェクト**（`daily/7203.json` / `margin/2026-09-05.json` 等）を検証器にかけ、契約が現実と食い違っていないことを先に確認 | — |
| 4 | `jss_*` の DDL 適用 + 既存テーブルへの `ALTER TABLE ADD COLUMN`。事前に全テーブルの `PRAGMA table_info` を取る | D1 で SELECT 確認 |
| 5 | **`supply_jsf`（日証金4CSV）を新規作成** | Notion ⑧ / PG と R2 / D1 の行数一致 |
| 6 | **`supply_jpx`（新旧両様式）** — 9/25 に間に合わないなら現行 cron を火曜+金曜へ先に直す | 数値トークン数分岐の単体テスト。旧様式の既存週で `find()` 等価性 |
| 7 | `yutai_monthly` に D1 書込を追加（保護列のテストを先に書く） | 保護4列が変化しないことを SELECT で確認 |
| 8 | `master_sync` に data_j.xlsx + `instrument_type`（列追加のみ）+ **4条件ガード** | dry-run で `instrument_type` 別件数を `stocks.json` と突合。ガードの発火テスト |
| 9 | `prices_daily`: 履歴子DB廃止 + R2 merge 経路 + マージ後系列でのテクニカル計算 + D1 断面 | **既存 `daily/{code}.json` を1件も壊さない**（GET→書き戻しの往復テスト）。sma75/sma200/week52 が NULL にならないこと |
| 10 | `master_sync` の母集団 **+725件** INSERT（**9 の直前**） | 旧 writer の処理対象増を承認済みであること |
| 11 | `export_weekly` の入力を R2 `daily/` へ、トラックAを D1 へ | 生成 Parquet の行数・期間が従来と一致 |
| 12 | `edinet_daily` / `tdnet_hourly` に R2 原本 + `jss_*`。TDnet PDF 取得を追加 | `jss_raw_files` と R2 の突合 |
| 13 | `reconcile_weekly` を整合チェックへ改修 | 意図的に不整合を作って検出できるか |
| 14 | `DisclosureRecord.yanoshin_id` + `classify.ts` の Python 移植 + `doc_id` バックフィル | 既存37,338行の `doc_id` 充足率 |
| 15 | `CF_WRITE_MODE=live` → `CF_REQUIRED_DATASETS` を段階的に追加 | `jss_dataset_freshness` の鮮度 |

**5〜7 を先頭に置く理由**: ⑧需給と⑨優待は Notion 側も実測0行で、stockStock は Cloudflare リソースを1つも持たない＝**まだ何も二重になっていない**。最初から正しい配置で作れるので後付けの重複解消が不要で、撤退コストがゼロ。ただし⑧には期限がある（§2.6）。

---

### 7. 未確定事項

#### 7.1 実装前に潰す必要があるもの

| # | 事項 | 潰し方 |
|---|---|---|
| U-1 | **Workers プラン（Free / Paid）** | 前提条件 P-1。Free なら D1 縮退が必須で、`swing_daily_ohlcv` 廃止の前倒しが要るが、それは他の波の完了が前提という循環になる。実際には「Free なら Paid に上げる」が唯一の現実解（$5/月の判断のために設計を2通り維持するコストのほうが高い） |
| U-2 | **`get_info()` に `operatingMargins` / `trailingEps` / `bookValue` / `returnOnEquity` / `returnOnAssets` が含まれるか** | `_VALUATION_KEYS` は現在 `market_cap` / `trailingPE` / `priceToBook` / `dividendYield` の4つのみで、`get_info()` の返り値をそのまま引く仕組みなので**キー追加だけで済む見込み**だが未検証。含まれなければ `jss_financials` からの join に倒れ、既存値（Yahoo TTM）と**定義が変わって 001/002 の画面数値が動く**。`core_stock_financials` の writer 移管に着手する前に実レスポンスで確認する |
| U-3 | **2026-09-28 の JPX 新様式の実フォーマット** | 実物が存在するのは当日以降。ファイル名パターンは公式資料に記載がなく、新様式が金額を持つかも告知の変更点欄（「金額も公表対象」）と公表イメージ（`(Unit:1Share)` で株数15列のみ）が食い違っている。数値トークン数で分岐する設計なので15列でも23列でも壊れないが、**初回に実物で確認する**。9/27 20時ごろの JPX 告知（移行可否）を人間が確認する運用を残す |
| U-4 | **日証金 CSV の列名と per-code 側キーの1対1対応** | ヘッダ全列は実取得済みだが、格納側のキー名への写像は実装時に cp932 原文と突合してから確定する |
| U-5 | **`data_j.xls` の「市場・商品区分」の網羅的な実値** | 代表値のコメントしか根拠がない。未知の区分は `None` にしてログに出す（`equity` に倒さない）。dry-run で `instrument_type` 別件数を `stocks.json`（4,445件の内訳: プライム1,558/スタンダード1,575/グロース595/ETF・ETN 466/PRO 181/REIT等63/外国株5/出資証券2）と突合する |
| U-6 | **`core_stocks` / `core_stock_financials` / `yutai_benefits` の NOT NULL 制約** | `ir_disclosures` のみ実読確認済み。`ALTER TABLE` の前に `PRAGMA table_info` を取る |
| U-7 | **EDINET 拡張種別の年間件数** | すべて推定。`--doc-types extended` を1日だけ dry-run で回して実件数を測ってから本番投入する |
| U-8 | **バリュエーション並列化で 429 を食うか** | `--valuation-workers 1` で従来の逐次に戻せるようにし、4から始める |
| U-9 | **R2 の PUT/GET レイテンシと実効帯域** | 所要時間の 2〜4分は 50〜100 Mbps の仮定。実測なし |
| U-10 | **yfinance の 1チャンク download 単価** | 6s/チャンク（100銘柄・`period=3mo`）は仮定 |

#### 7.2 本仕様で決めないこと

1. **一覧系原本（`documents_list` / `tdnet_list`）の保持方針**（§3.1 末尾）。「増分だけ異なる同日再取得」を「その日の最終版だけ残す」か「全スナップショット残す」か。実測でローカルに `edinet_documents_list_ALL.json` の変種が780件ある。
2. **みんかぶ経路の公認照会を待つか。** `personal-only` 維持を前提にしているが、仮に公認や法務確認でタグが変われば、公開面の是正が不要になり `description` を戻す逆方向の作業が発生する。照会窓口は規約にも関連法規ページにも明記がない。
3. **`collectors/minkabu_yutai.py` を PUBLIC リポジトリにコミットすることの是非。**
4. **R2 Data Catalog + R2 SQL の有効化**（`derived/*.parquet` との二重保持になるため現設計では有効化しない。移行パスのみ確保）。

#### 7.3 スコープ外だが依存している作業（kabulab-cf 側）

1. **みんかぶ掲載文の公開面からの除去**（`services/otakara-yutai/app.ts:680` の `descHtml`）。stockStock が `yutai_benefits` の writer になる工程と**同じタイミング**で是正しないと、規約違反をそのまま新基盤へ引き継ぐ。stockStock 側は `license_tag='personal-only'` を付けるところまでしかできない。
2. **`classify.ts` の20タグ移植。** 済むまで第6波に進めない。
3. **`daily.ts` の分割改修。** `rsi_percentile` / `swing_*` を書くために `stock-sync.yml` の cron を動かし続ける必要があるので、「stockStock が書く部分を skip する」フラグが入らないと `core_stock_financials` のカットオーバーができない。
4. **`vwap-ingest.yml` の intra 切り出し。** `"0 8 * * 1,3,5"` は daily と intra を同じステップで回しているので、daily を止めるつもりが intra も止まる。
5. **`public/vwap-analysis/data/stocks.json` のビルド時生成物への降格**（銘柄マスタが CF 上に複数ある状態の解消）。
6. **`vwap` リポジトリの `.github/workflows/build-stocks.yml` の停止。** 週1で `docs/data/stocks.json` を再生成・commit しており今も生きている。止める前に `株ラボ-新高値ブレイク検証/scripts/fetch_universe.py` の参照を切る。`kabulab-cf/.env` は絶対に消さない（同リポの `_bootstrap.py` が R2/D1/EDINET の資格情報をここから読み、無いと `FileNotFoundError` で即死する）。
---

# API / MCP 公開

> 本書は公開層（REST API / 物理ファイル配信 / MCP）の確定仕様のみを扱う。R2 キー設計・D1 テーブル定義・Notion プロパティ定義は別書（ストレージ配置）にあり、ここでは再掲しない。
> 行番号付きの引用は 2026-09-11 に実読したもの。未確認事項は「**未確認**」と明記する。

# 1. この層が守る3つの分離

| 分離軸 | 何を防ぐか | 実装場所 |
|---|---|---|
| 読取 / 書込 | 読取利用者に書込権限が渡る | Worker は読取専用。データ本体の書込は今までどおり GitHub Actions → D1 REST / R2 S3互換。Worker に汎用の書込面を作らない |
| 公開 / 内部（ライセンス） | personal-only が公開面に出る | デプロイ・データ・**列**・出口・CI の5層（§6） |
| 行 / ファイル | D1 の走査行課金を外部に握らせる | 行APIは索引カバー述語のみ。バルクは必ず R2 のファイル |

---

# 2. デプロイ単位

## 2.1 決定: 新規 Worker 2本（kabulab-cf にも kabuMCP にも相乗りしない）

| Worker | 用途 | bind | 認証 |
|---|---|---|---|
| `jss-api-public` | 公開 REST。`commercial-ok` と `factual-cite`（メタのみ） | `DB`(D1 kabulab-cf) / `RAW`(R2 jp-stock-raw) | 無認証（レート制限のみ） |
| `jss-api-private` | 内部 REST + **MCP**。personal-only を含む全量 | 上記 + `TS`(R2 vwap-data) + `SUPPLY`(R2 jp-stock-supply) | APIキー必須 |

- **`kabulab-cf` に相乗りしない**: 7サービスの SSR 配信 Worker であり、切替第2〜6波で画面を壊すリスクと外部 API の互換保証が同一デプロイ単位に乗る。さらに既存 `/api/*` は現時点で personal-only を無認証配布しており（§6.5）、是正前の面と是正後の面が同一ホストに混在する。
- **`kabuMCP` に統合しない**: Stripe webhook / Cookie 認可 / PBKDF2 ログインを抱える課金 Worker の攻撃面にデータ面を乗せない。かつ kabuMCP のデータは Static Assets（Worker を経由しない＝リクエスト課金ゼロ）で、D1/R2 読みに替えるとその利点を捨てて走査行課金を買うことになる。
- **D1 は1個のまま**。同一 D1 を2つの Worker が bind するのはコードの分割であってデータの複製ではない。両方とも読取専用なので writer 二重稼働も起きない。
- `jss-api-public` は `TS` と `SUPPLY` を **bind しない**。コードのバグでも personal-only の R2 に物理的に到達できない（§6 L0）。

## 2.2 ドメイン

`webmado.com` はこのアカウントの Cloudflare ゾーン（kabuMCP が `kabumcp.webmado.com` を本番稼働）。新 Worker は**カスタムドメインに載せる**（仮: `jpstock.webmado.com` / `jpstock-x.webmado.com`。ホスト名は未確定）。

理由はキャッシュ。`workers.dev` ではエッジキャッシュが効かないという既知の挙動があり、効かないと D1 走査行と R2 Class B が素通しになる。**この挙動は本タスクで公式ドキュメント未確認**だが、効かない場合カスタムドメインは任意ではなく前提になるので、最初からカスタムドメインで立てる。

## 2.3 kabuMCP との関係

| 観点 | 判断 |
|---|---|
| Worker | 分離 |
| ツール名前空間 | 分離（kabuMCP は `edinet_*` / 新規は `jp_*`）。将来 kabuMCP のツール実装を新 API へ載せ替えてもツール名は変えない |
| 認証基盤 | 共有（kabuMCP の `kmcp_` トークン検証を再利用。フェーズ3） |
| **検索索引** | **kabuMCP の既存 Static Assets（entities 27シャード）を再利用する。新規に検索索引を作らない**（§2.4） |
| データ本体 | 正本の統一のみ。物理コピーの解消はフェーズ4 |
| MCP エンドポイント | 2本並存。クライアントは両方登録できるので統合の必要がない |

## 2.4 銘柄名検索を D1 で引かない／新規索引も作らない

名前検索は `LIKE '%...%'` になり全走査＋索引不使用になるため、D1 では引かない。当初案は `core_stocks` からシャード索引をビルドする設計だったが、**kabuMCP が機能的に同一の entities 27シャードを既に持っている**。同じ機能の索引をもう1本作ると、解消するはずの「CF 上の銘柄マスタ多重化」を1本増やすことになる。

→ **`jp_search_stocks` / `GET /v1/stocks/search` は kabuMCP の Static Assets を参照する**（service binding もしくは同一生成物の共有）。生成元を `core_stocks` に一本化する作業は、kabuMCP dataset のビルド入力差し替え（フェーズ4）と同じ工程で行う。

---

# 3. REST API の確定仕様

## 3.1 共通規約

- ベース: `https://<public-host>/v1/...` / `https://<private-host>/v1/...`。パスは同形、**ホストでライセンス面を分ける**。
- バージョン `/v1` 固定。破壊的変更は `/v2` を並走。
- レスポンス封筒（全エンドポイント共通）:

```json
{
  "data": [],
  "next_cursor": "eyJrIjoiNzIwMyJ9",
  "meta": {
    "as_of": "2026-09-10",
    "license": "commercial-ok",
    "source": "EDINET",
    "attribution": "出典: EDINET（金融庁）。本データは EDINET 公表情報を編集・加工して作成。",
    "disclaimer": "情報提供のみを目的とし、投資助言ではありません。",
    "filtered": 0,
    "rows_read": 12
  }
}
```
  - `attribution` は `licensing.ATTRIBUTION` と**同一文字列**。公開面に出典表記が無い状態を作らない。
  - `filtered` は出口フィルタが落とした件数。黙って落とさない。
  - `rows_read` は D1 の実走査行数。**D1 の結果 `meta` に走査行数が入るかは未確認**。入らない場合はこのキーを省略し、推定値で埋めない。
- エラー: `{"error":{"code":"...","message":"...","hint":"..."}}`。`400 bad_request` / `401 unauthorized` / `403 license_denied` / `404 not_found` / `429 rate_limited` / `503 upstream_unavailable`。**「存在するが公開できない」は 403 で返し、404 で隠さない。**
- ページング: **keyset cursor のみ**。`OFFSET` を受けない。`limit` 既定50・上限500。
- 条件付きリクエスト: 全 GET が `ETag` を返し `If-None-Match` で 304。
- **1リクエストあたりの D1 走査行上限は 2,000**。超える述語はそもそも受けない。

## 3.2 公開面（無認証）

出せるのは `commercial-ok` 全量と `factual-cite` のメタデータ＋原典リンクのみ。

| パス | パラメータ | カバー索引 | 最大走査行 | キャッシュ |
|---|---|---|---|---|
| `GET /v1/meta/freshness` | — | 全件 | 約20 | 60s |
| `GET /v1/meta/licenses` | — | 静的 | 0 | 1d |
| `GET /v1/meta/jobs` | `job_name` `limit` `cursor` | job索引 | ≤500 | 5m |
| `GET /v1/meta/openapi.json` | — | — | 0 | 1d |
| `GET /v1/stocks` | `listing` `industry` `limit` `cursor` | code / edinet索引 | ≤500 | 1d |
| `GET /v1/stocks/{code}` | — | code UNIQUE | 1 | 1d |
| `GET /v1/stocks/search` | `q` `limit` | **D1 を引かない**（kabuMCP entities） | 0 | 1d |
| `GET /v1/stocks/{code}/financials` | `disclosure_type` `limit` `cursor` | PK先頭 | ≤30 | 1h |
| `GET /v1/stocks/{code}/financials/annual` | `limit` | (stock_id, fiscal_year) | ≤10 | 1h |
| `GET /v1/stocks/{code}/disclosures` | `from` `primary_tag` `limit` `cursor` | (stock_id, pubdate) | ≤数百 | 1h |
| `GET /v1/disclosures/latest` | `primary_tag` `source` `limit` `cursor` | (primary_tag, pubdate DESC) | LIMIT分 | 5m |
| `GET /v1/disclosures/{doc_id}` | — | doc_id索引 + raw索引 | 2 | 1h |
| `GET /v1/xbrl/documents` | `code` `fiscal_year` `limit` `cursor` | (code, period_end) | ≤30 | 1h |
| `GET /v1/xbrl/documents/{doc_id}` | — | PK | 1 | 1h |
| `GET /v1/xbrl/elements` | `prefix` `limit` `cursor` | PK前方一致 | ≤500 | 1d |
| `GET /v1/files/{sha256}` | — | PK | 1 | 1y |
| `GET /v1/files/{sha256}/content` | `derived=1` | — | 1 | 1y immutable |
| `GET /v1/bulk/exports` | — | — | 0 | 1h |
| `GET /v1/bulk/exports/{date}/{name}` | — | — | 0 | 1y |

**`/v1/stocks` の列が薄いことの明示**: ①銘柄マスタは列単位でライセンスが混在する（EDINET コードリスト由来＝commercial-ok / JPX data_j.xls 由来＝personal-only）。公開面に出せるのは EDINET 由来（`code` / 名称3種 / `edinet_code` / 上場区分 / 提出者業種 / 決算日 / 連結の有無）に限られ、**市場区分でも33業種でも絞り込めない**。「東証プライムの高ROE」のような最も自然なクエリは公開面で成立しない。これは設計上受け入れる制約であり、緩和は §7 の判断事項。

**`/v1/files/{sha256}` は commercial-ok の行のみ返す**（§6.2）。原本索引にはメタだけでも日証金 CSV・みんかぶ JSON・JPX PDF の所在が入るため、「メタは全行公開可」としない。

## 3.3 内部面（APIキー必須）

公開面の全エンドポイントに加えて:

| パス | 裏側 |
|---|---|
| `GET /v1/stocks/{code}/snapshot` | ②断面（D1・1行） |
| `GET /v1/prices/{code}/daily` `?from&to` | R2 日足 |
| `GET /v1/prices/{code}/intraday` | R2 5分足 |
| `GET /v1/index/{slug}/daily` `?from&to` | R2 指数 |
| `GET /v1/supply/{code}` `?series&from&to` | R2 需給 per-code |
| `GET /v1/supply/latest` `?data_type&limit&cursor` | D1 需給断面 |
| `GET /v1/yutai/{code}` | D1 優待（**掲載文の列を SELECT しない**） |
| `GET /v1/files/{sha256}/content` | R2 全バケット |
| `POST /v1/admin/freshness` | D1 鮮度 upsert |
| `POST /v1/admin/cache/purge` | Cache API |

`/v1/admin/*` が**この層で唯一の書込面**で、writer 自身が鮮度断面を書くためだけに存在する。

## 3.4 差分取得・バルク取得

- 差分取得は**索引がある列でだけ**受ける: 開示は `from`（pubdate）、財務は `disclosed_since`（disclosed_at）。原本の `fetched_since` は**索引が無いので受けない**（`(source, datatype, data_date DESC)` で代用）。断面テーブル（①②⑨）は全件が最新なので差分パラメータ自体が無い。
- 索引のない `updated_since` を受けると全走査になる。代わりに `GET /v1/meta/freshness`（約20行）で「どのデータセットがいつ更新されたか」を返し、更新されたものだけ取りに来させる。これが差分取得の正道。
- バルクは必ず R2 のファイル。トラックA（commercial-ok / factual-cite）とトラックB（personal-only）は別ファイルで、公開ホストはトラックAのファイルしか解決しない。**D1 に対するバルク（全銘柄の財務を1回で等）は提供しない。**
- **トラックB（株価全履歴）は R2 に置かない**。R2 の per-code 日足と同一粒度・同一期間の重複であり、容量試算にも未計上だった（2.9〜3.9GB）。`GET /v1/bulk/exports` は日足プレフィックスを指すポインタ（キー一覧と各銘柄の最終日）を返す。

## 3.5 受けない述語（明示）

任意 `WHERE` / 任意 `ORDER BY` / 生 SQL / `LIKE '%...%'`（既存の外部読者が D1 直読で使っている用法は温存するが、**公開面には出さない**）/ `OFFSET` / 無索引列での絞込・ソート。**D1 REST API のトークンを外部に配らない。**

---

# 4. 物理ファイルの取得導線

## 4.1 決定: Worker 経由ストリームを既定とする

| 軸 | (1) Worker ストリーム | (2) presigned URL | (3) R2 カスタムドメイン公開 |
|---|---|---|---|
| 認可の粒度 | **オブジェクト単位** | 発行時に判定 | **バケット単位のみ** |
| personal-only の混在 | 安全 | 発行を絞れば安全 | **一発で規約違反** |
| 鍵の露出 | 無し（binding のみ） | S3 キーを Worker Secret に置く | 無し |
| 実装コスト | 小 | SigV4 自前実装（**R2 binding に presign API があるかは未確認**） | 最小 |
| 監査 | Worker ログに残る | 発行時のみ | 残らない |

**(3) は採用しない。** 原本バケットには EDINET(commercial-ok)・TDnet(factual-cite)・JPX/日証金/みんかぶ(personal-only) が同居し、R2 の公開アクセスはバケット単位でしかスコープできない（プレフィックス単位の公開はできない）。公開ドメインに載せた瞬間に日証金（「第三者の利用に供することを固く禁じます」）が無認証配布になる。
**(2) は 50MB 超のバルク export のみの例外オプション**として設計だけ残す。v1 では使わない。

## 4.2 実装

```
GET /v1/files/{sha256}/content?derived=1
```
1. 原本索引を `sha256` 主キーで1行引く（走査1行）
2. `license_tag` で配布可否を判定
3. 不可なら **403 `license_denied`**、可なら R2 binding で `get()`
4. ストリームをそのまま返す（**本文をメモリに読まない**。CPU ほぼ 0）

返すヘッダ: `Content-Type` / `Content-Disposition` / `ETag`（= sha256） / `Cache-Control: public, max-age=31536000, immutable` / `X-JSS-License` / `X-JSS-Source` / `X-JSS-Attribution` / `X-JSS-Doc-Id` / `Accept-Ranges: bytes`。
`immutable` を付けられるのは原本キーが sha256 入りで上書き経路が無いことの直接の帰結。

`doc_id` からの導線も1本用意する: `GET /v1/disclosures/{doc_id}` のレスポンスに `links.raw` / `links.derived` を入れる。

> **前提条件（ingest 層への依存）**: この導線は「原本1件 = doc_id で一意に指せる」ことに依存する。現行 `RawArtifact` は `doc_id` フィールドを持たず、ローカル原本の命名は (scope, data_date) 単位で、実測で `edinet_pdf_8306_20240729` に 250個の別内容原本が存在する。**`doc_id` を原本メタに持たせるまで、`GET /v1/disclosures/{doc_id}` → `links.raw` は成立しない。** これは移行 T-RAW の着手前提として管理する（migration §3）。

## 4.3 ライセンス別の配布可否

| タグ | メタ（`/v1/files/{sha256}`） | 本体（`/content`） | 根拠 |
|---|---|---|---|
| `commercial-ok` | 公開可 | **公開可** | `is_publishable() == True` |
| `factual-cite` | 公開可（原典URL併記） | **公開しない**（内部面のみ） | `is_metadata_publishable()` の定義が「メタ+リンクのみ」 |
| `personal-only` | **内部面のみ** | 内部面のみ | 両方 False |

→ `licensing.py` に **`is_file_distributable(tag)`（commercial-ok のみ True）** を追加し、Worker 側の判定表をこの関数から生成する。判定を2言語に手書きしない。

---

# 5. MCP サーバの確定仕様

## 5.1 トランスポート

`jss-api-private` の `POST /mcp` に同居させる（別 Worker にしない。バインディングとライセンス判定を REST と共有するため）。実装構造は kabuMCP の現行実装を踏襲する。

- Streamable HTTP・ステートレス（Durable Objects 不要）
- `server/discover`（新世代）と `initialize`（旧世代）の**両方**を受ける
- `tools/list` は**認可状態によらず常に全ツールを列挙**し、保護対象は `tools/call` 時点で判定
- 未認可の `tools/call` は **401 + `WWW-Authenticate: Bearer realm="jss"`** と JSON-RPC エラーを同時に返す
- 全ツールに `annotations: { readOnlyHint: true, destructiveHint: false }`
- **プロトコル版の仕様そのものは未確認**（kabuMCP の実装とコメントからの引用）。仕様と食い違っても kabuMCP と同じ挙動にはなる。

## 5.2 ツール一覧（`jp_*` 名前空間）

| ツール | 引数 | 返り値の骨子 | 面 |
|---|---|---|---|
| `jp_search_stocks` | `query` `limit≤20` | code / 名称 / edinet_code / 上場区分 / 業種 / ambiguous | 公開 |
| `jp_get_stock` | `code` | マスタ1件 + 最新決算期 + 開示件数 + links | 公開 |
| `jp_get_financials` | `code` `disclosure_type?` `limit≤30` | 決算期別の主要勘定 + doc_id + raw_sha256 + source_url | 公開 |
| `jp_list_disclosures` | `code?` `primary_tag?` `from?` `limit≤50` | 開示メタ（本文は返さない） | 公開 |
| `jp_get_disclosure` | `doc_id` | 上記1件 + files:{raw, derived} | 公開 |
| `jp_list_xbrl_documents` | `code` `limit≤30` | doc_id / 期間 / ファクト件数 / Parquet の所在とサイズ | 公開 |
| `jp_list_xbrl_elements` | `prefix?` `limit≤100` | 勘定科目の語彙 | 公開 |
| `jp_get_file_link` | `sha256?` `doc_id?` `derived?` | url / bytes / content_type / license_tag / attribution。**本文は返さない** | タグ依存 |
| `jp_get_freshness` | — | データセット別の鮮度と writer | 公開 |
| `jp_get_prices_daily` | `code` `from?` `to?` `limit≤500` | 日足 | **内部のみ** |
| `jp_get_supply` | `code` `series` `from?` `to?` | 需給時系列 | **内部のみ** |
| `jp_get_yutai` | `code` | 優待の構造化項目（**掲載文を含まない**） | **内部のみ** |

**`jp_get_xbrl_facts`（ファクト本体の抽出）は v1 に入れない。**
理由は2つ。(a) Worker 上で Parquet を解くコストが読めない（pure JS 実装は存在するが**本タスクで未検証**）。(b) doc 単位の tidy JSON を新設すると Parquet と二重になる。
→ 代替は `jp_list_xbrl_documents` → `jp_get_file_link` で Parquet の URL を返し、クライアント側で解かせる。**MCP の役割は「所在と語彙の解決」に限定する。** ピンポイントの財務値は ③財務サマリが主要勘定を持つので LLM 用途の大半はそちらで足りる。

## 5.3 返り値の規約

```json
{
  "content": [
    {"type":"text","text":"（人間可読の1行要約）"},
    {"type":"text","text":"（structuredContent と同内容の JSON 文字列）"}
  ],
  "structuredContent": {},
  "isError": false,
  "_meta": {
    "jss/license":    {"tag":"commercial-ok","attribution":"出典: EDINET（金融庁）…"},
    "jss/provenance": {"source":"EDINET","data_date":"2026-06-20","doc_id":"…","raw_sha256":"…"},
    "jss/cost":       {"rows_read":3,"r2_gets":0}
  }
}
```

- `content[1]` に JSON 文字列を重ねるのは、OpenAI 系クライアントが `structuredContent` と同内容の文字列を要求するため（kabuMCP の実運用知見）。
- **全ての数値に来歴（source / data_date / doc_id / raw_sha256）を必ず付ける。** 不変条件「来歴」を公開層まで貫通させ、LLM が値だけを持ち出して出典を失う事故を構造的に防ぐ。
- サーバの `instructions` に「personal-only のデータは回答に含めても再配布しないこと」を明記する。

---

# 6. 認証

## 6.1 単一 CRON_SECRET では足りない

kabulab-cf の現行 `verifyCronSecret()` は (a) 1本の秘密を6つの writer 経路が共有（読取利用者に配ると書込も渡す）、(b) 比較が非定数時間、(c) ローテーションできない（1本止めると6経路が同時に死ぬ）。公開面の入口には使わない。

## 6.2 3面の鍵

| 面 | 鍵 | 保存 | 検証 |
|---|---|---|---|
| 公開読取 | 無し | — | レート制限のみ |
| 内部読取（REST / MCP） | `jss_live_<32hex>` を `Authorization: Bearer` | D1 に **SHA-256 のみ**。平文は発行時に一度だけ返す | 定数時間比較。先頭13文字の prefix で失効管理 |
| 管理書込（`/v1/admin/*`） | `ADMIN_KEY`（CRON_SECRET とは別 secret） | Worker Secret | 定数時間比較 |

APIキー表と使用量計上表は新規テーブル（数十〜数千行）。`10GB` 上限への影響は無視できるが、確定済みの D1 配置表に無いため**追加提案**として扱う。

## 6.3 ライフサイクル

- フェーズ0–1: キー表を作らず、Worker Secret の単一 `SELF_KEY` だけで内部面を開ける（利用者は自分1人）。
- フェーズ2: キー表を導入し**用途別に発行**（`youtube-analysis` / `breakout-backtest` / `claude-code` など）。どの経路が走査行を食っているかが分かる。
- フェーズ3: kabuMCP の `kmcp_` トークンも受け付ける。ただし **personal-only スコープは自分の operator_id にのみ付与する**（他人に配った瞬間に「私的利用」の前提が崩れる）。

---

# 7. ライセンスフィルタ（必須要件）

## 7.1 5層防御

| 層 | 実装 | 保証 |
|---|---|---|
| **L0 デプロイ** | 公開 Worker は personal-only の R2 バケットを bind しない | コードのバグでも物理的に到達不能 |
| **L1 データ** | 全行に `license_tag`、全 R2 JSON に `license` | 判定材料が常に手元にある |
| **L2 クエリ** | 公開 Worker の SQL は**定数SQLのホワイトリスト**（1ファイル＝1エンドポイント）。動的 SQL 組立を禁止 | D1 に列レベル権限が無いので SQL の形そのもので守る |
| **L3 出口** | レスポンス組立の唯一の関数 `emit(rows, {audience, dataset})` を通す | 黙って漏らさない・黙って落とさない |
| **L4 CI** | ①公開 Worker の全 SQL の列を列ライセンス表と突合、②公開面の全エンドポイントを叩いて personal-only が0件であることを検査 | 将来列を足したときに気付ける |

## 7.2 `emit()` の契約は**列レベル**（行レベルでは不十分）

当初案の「行の `license_tag` が personal-only なら除去」は**①銘柄マスタに対して機能しない**。①は1行の中に EDINET 由来列（commercial-ok）と JPX 由来列（personal-only）が混在し、行に1つの `license_tag` を持たせても表現できない。

→ 契約を次のとおり確定する。

```ts
type Audience = "public" | "private";
function emit<T>(rows: T[], opts: {audience: Audience; dataset: string}): Envelope<T>
```
1. **列ライセンス表（`(table, column) → license_tag`）を唯一の判定源**とし、`audience="public"` では**列ホワイトリストで SELECT 句を組み立てる**。行フィルタは補助。
2. ホワイトリストに無い列がレスポンスに含まれていたら**例外**（開発時に落ちる）。
3. 行ごとの `license_tag` が personal-only の行は除去し、`meta.filtered` に件数を入れる。
4. `meta.license` / `attribution` / `disclaimer` を必ず埋める（空のまま返せない型にする）。
5. **全ての公開レスポンスはこの関数だけが生成する**（`c.json()` の直接呼び出しを lint で禁止）。

列ライセンス表は **Worker に焼き込む**（D1 に置くと運用中に公開範囲が変わってしまう）。生成元は `licensing.py` の判定関数。

## 7.3 メタ系エンドポイントにも同じフィルタを掛ける

見落としやすい3経路を明示する。

- `GET /v1/meta/jobs` … ジョブ名・失敗銘柄に personal-only データセットの情報が出る → 該当データセットの行を出口で落とす。
- `GET /v1/meta/freshness` … 保管先にバケット名が出る → 同上。
- `GET /v1/files/{sha256}` … 原本索引のキー文字列にソース名が出る → **commercial-ok の行のみ返す**（§3.2）。

## 7.4 前提: 既存公開面は既に personal-only を無認証で配っている

実読で確定した事実（本設計が作ったものではない）。

| 現行 URL | 配っているもの | タグ |
|---|---|---|
| `/vwap-analysis/api/daily?code=` | yfinance 日足10年（`app.ts:62-69` は `passthrough(o.body, 3600)` で R2 の本文を素通し） | personal-only |
| `/vwap-analysis/api/intra?code=` | yfinance 5分足（同 :53-60・同じく素通し） | personal-only |
| `/vwap-analysis/api/margin?code=` | JPX 信用残（同 :72-99） | personal-only |
| `/otakara-yutai/api/screening` ほか | みんかぶ由来の優待データ | personal-only |

新 API の公開ホストを立てることは、この状態を**追認も拡大もしない**。むしろ「公開してよい面はこれだけ」という基準線を作る。既存面の是正は移行の各波に組み込む（migration A24）。

## 7.5 **公開面を拡大しないための、ストレージ層への拘束（新規・重要）**

実読で確定した2点により、「R2 オブジェクトへの追加キーは既存読者が無視するから安全」という前提は**成立しない**。

1. `/api/daily` と `/api/intra` は R2 の本文を**バイト単位で素通し**する（`passthrough(o.body, …)`）。したがって R2 側に足したキーは**そのまま無認証で公開される**。
2. `/api/margin` は `return row ? { week: w, ...row } : null;`（`app.ts:94-95`）で行オブジェクトを**丸ごとスプレッド**する。したがって行に足したキーも**そのまま公開される**。ペイロードは5フィールド→17フィールドで約3.4倍になり、`n=260` 指定時に効く。

→ 公開層として次を契約化する。

- **既存公開 Worker が素通し／スプレッドしている R2 オブジェクトに、personal-only 由来の属性を足さない。** 具体的には (a) 日足オブジェクトに JPX data_j.xls 由来の銘柄種別を入れない、(b) 信用残の行に銘柄表記名と ISIN を入れない（ISIN は主キーとして必要なので per-code 側にだけ持たせる。銘柄名は①マスタから引ける）。
- writer 側の母集団事故防止（ETF を更新停止させない）は、**PUT 前に対象コードが①マスタに存在し銘柄種別が非 NULL であることを検査するコード側のガード**で足りる。公開 JSON に分類を焼き込む必要はない。
- ライセンス・writer・スキーマ版などの運用メタも同じ理由で公開オブジェクトには入れない（入れるなら `/api/daily` を素通しから許可キーのホワイトリスト再構成に変える改修が先。CPU とコードが増えるので入れないほうが安い）。
- **この判断はユーザー確認事項**（migration 判断事項 #4）。「追加キーを公開してよい」と判断するなら、007 の改修とセットにする。

### 7.5.1 外部読者側の失敗モード（追加キーの副作用）

`株ラボ-Youtube` の信用残ローダは `pl.DataFrame(rows)` で**行辞書の全キーから DataFrame を構築してから** `.select()` で5列に絞る（`margin.py:117-127`、実読確認）。polars のスキーマ推論（既定100行）以内が全て null の数値列が混じると `Null` dtype に推論され、後続行の整数で `ComputeError` になる。`.select()` は構築の後なので防げない。
→ 「既存キーを消さなければ後方互換」という分析はこの失敗モードを拾えていない。追加キーを入れる場合は、外部読者の実データでの読み取りテストを切替前ゲートに含める。

---

# 8. キャッシュとレート制限

## 8.1 キャッシュ

| 面 | 方式 | TTL |
|---|---|---|
| 公開 REST | `Cache-Control: public` + Cache API | 銘柄1d / 財務・開示1h / 最新開示5m / 鮮度60s |
| 原本ファイル | `public, max-age=31536000, immutable` | 1年 |
| 内部 REST | 既定 `private, no-store`。R2 の per-code JSON のみ**認可判定の後に**プライベート名前空間へ | 1h |
| MCP | `no-store`（`tools/list` のみ TTL ヒントを返す） | — |

- キャッシュキーは**正規化した URL**（クエリの順序固定・未知パラメータの除去）。正規化しないと `?foo=1` を付けるだけでキャッシュを迂回して D1 を叩ける（安価な DoS 経路）。
- Cache API は Worker のオリジンに紐づき外部から到達できないので、**認可判定の後に引けば**内部データを入れても漏れない。判定の前に引いてはいけない。

## 8.2 レート制限

| 面 | 単位 | 上限（設計値） |
|---|---|---|
| 公開 REST | IP | 60 req / 60s |
| 公開 ファイル配信 | IP | 20 req / 60s |
| 内部 REST / MCP | APIキー | 300 req / 60s |
| 全体 | — | 1リクエストあたり走査行 2,000 |

- Workers の Rate Limiting binding は同一アカウントで本番稼働実績がある（`period` は 10 か 60 のみ）。**プラン要件は未確認**。Free で使えない場合は Cache API + D1 カウンタの自前実装に縮退する（精度は落ちる）。
- `429` には `Retry-After` を必ず付ける。

## 8.3 この層自身が発生させる R2 GET

公開層のトラフィックとは別に、writer 側だけで年 約262万回の R2 GET（Class B）が確定的に発生する（日足の read-merge-write 109万 + 需給 107万 + 週次エクスポート 23万 + 整合チェック 23万）。Free 枠 10M/月 の 2.2% で課金は $0 だが、「未実測なので要監視」ではなく自分で確定的に発生させる量として計上する。公開層の GET はこれに加算される。

---

# 9. 導入順（ストレージ切替の波との対応）

| フェーズ | 内容 | 前提の波 | 公開面 |
|---|---|---|---|
| **S0** | `jss-api-private` のみデプロイ。`SELF_KEY` 1本。自分の Claude Code から MCP で叩ける | P1（需給）と並行可 | 出さない |
| **S1** | `jss-api-public` を立てる。①(EDINET列) / ③ / ④メタ / ⑤原本メタ+commercial-ok本体 / 鮮度 | **P5 完了後**、かつ ④の正本の宙吊り（migration 判断事項 #2）が解決した後 | commercial-ok + factual-cite(メタ) |
| **S2** | MCP を内部面で正式運用。APIキー表を導入し用途別キーへ | P6 完了後 | 変わらず |
| **S3** | kabuMCP の `kmcp_` トークンを受け付ける。公開 MCP の是非を判断 | — | 要判断 |
| **S4** | kabuMCP の dataset ビルド入力を stockStock 正本へ差し替え（正本の統一）。検索索引の生成元も同時に一本化 | ③④が安定後 | — |
| **S5** | 007 の `/api/margin` を per-code へ切替 → 信用残の互換シムの新規書込停止 | **P6 と同じ PR**（migration §4） | — |

**S1 より前に公開面を立てない。** ③④が D1 上で stockStock 由来になっていない段階で公開 API を出すと、外部への契約が既存スキーマに固定され、切替の自由度が消える。

---

# 10. この層が満たす不変条件（チェックリスト）

1. 同じデータが CF 上に二重に増えない — **新規の検索索引を作らない**（kabuMCP entities を再利用）、バルクの株価 Parquet を作らない
2. D1 の走査行を外部に握らせない — 固定パラメータ・索引カバー・cursor・走査行上限・D1 REST トークン非配布
3. personal-only が公開面に出ない — 5層防御 + **列レベルの出口契約** + メタ系3経路の明示
4. 公開面を無自覚に拡大しない — 素通し／スプレッドする既存 API の存在をストレージ層への拘束として契約化（§7.5）
5. 原本まで遡れる — 全レスポンスに `doc_id` と `raw_sha256` と `links.raw`（原本メタの `doc_id` 保持が前提）
6. 出典表記と免責が全公開レスポンスに入る — `emit()` の型で強制
7. writer が二重にならない — 公開層は読取専用。書込面は鮮度更新のみ
8. 公開中の7サービスを壊さない — 別 Worker・別ホスト・既存スキーマ非変更
---

# 移行フェーズ

> 本書は移行フェーズの確定仕様のみを扱う。R2 キー設計・D1 テーブル定義・Notion プロパティ定義は別書にあり再掲しない。
> 数値はレビュー側の再計算を採用する。未確認事項は「**未確認**」と明記する。

# 1. 移行期にだけ適用する規則（M1–M4）

確定済みの重複解消規則 R1〜R5 に加え、並行期間にのみ必要な規則を4つ置く。

## M1: 並行期間はキー空間を物理的に分ける
新 writer は切替が済むまで **shadow キー / shadow テーブル**にしか書かない。「同時に書くが後勝ちで揃えばよい」は採らない — D1 は 429 が出ないため二重書込が静かに成立し、どちらの値が入ったか事後に判別できない。

- shadow は R5 の**期限付き例外**として登録し、廃止条件（＝当該データセットのカットオーバー完了＋観測窓終了）と観測窓終了日を鮮度テーブルに書く。過ぎたら次回ジョブが自動で消す。
- 日足の shadow は全4,445件ではなく**層化サンプル200件**（全銘柄種別を網羅）。全件 shadow は純粋な重複で R1 の趣旨に反し、Class A を倍にする。

## M2: 同一テーブルの複数 writer は「列集合が互いに素」かつ「行の作成主体が1つ」のときだけ許す

| テーブル | 行の作成 + 基本列 | enrich 列（既存行の UPDATE のみ・INSERT 禁止） |
|---|---|---|
| ④開示メタ | stockStock | kabulab-cf: タグ2列 + 感情スコア3列 |
| ⑨優待 | stockStock | kabulab-cf: LLM 推定4列 |
| ②断面 | stockStock | （供給できない場合のみ）kabulab-cf: 営業利益率 |

担保: (a) stockStock の UPSERT は `SET` 句を**ホワイトリストで列挙**し、`SELECT *` 起点の動的 UPSERT を禁止。(b) kabulab-cf の enrich は `UPDATE ... WHERE id = ?` のみ。(c) `jss_writer_claims(dataset, column_group, writer, updated_at)` を新設し、各ジョブ冒頭で claim を照合、想定外の writer 名なら**異常終了**。**claim 照合は kabulab-cf 側にも入れる**（片側だけの規律にしない）。

## M3: 後退禁止ガード（データ量が減る書込を物理的に拒否する）
R2 のマージ書込は「読んで・足して・全置換」なので、読みに失敗した瞬間に長期履歴が消える。全ての merge PUT は以下を PUT 前に検証し、1つでも満たさなければ **PUT せず失敗として記録**する。

- 既存オブジェクトの GET が 404 以外のエラーで失敗した → PUT しない（「無かったこと」にしない）
- **配列型の全置換 PUT は要素数が減ったら拒否**（日足の bars に限らず、週次インデックス・需給の series・margin index の全てに適用する汎用ルール）
- 期間の始端が後退した → PUT しない
- 既存の `writer` が自分以外 → PUT しない
- 契約必須キーが1つでも欠けた → PUT しない
- 要素数が少ない配列（週次インデックス等）は追加で**既存全要素が新配列に含まれることを集合として検証**する

## M4: writer 二重稼働の検知を R2 の 429 に依存しない
「同一キーへの並行書込 1/秒 → 429 で気付ける」は成立しない。日足は 4,445キー、需給は 4,351キーに分散するので、2本の writer が別々の銘柄を処理していれば同一キーが同時に叩かれる確率はほぼゼロで 429 は出ない。

→ 全ての mutable JSON の PUT 直前に (a) GET した payload の `writer` が自分か (b) claim が自分か、を**両方**チェックする。最も確実なのは **writer ごとに R2 トークンを分け、切替時に旧トークンを revoke して物理的に書けなくする**こと。日足・信用残の切替でも同じ手を使う。

---

# 2. フェーズ全体像

| | フェーズ | 触る対象 | 既存への書込 | 承認 | 実装(人日・見積) | 観測窓 |
|---|---|---|---|---|---|---|
| **P0** | 準備（cloud 層・新規リソース・契約・**プラン確定**） | 新規のみ | なし（SELECT のみ） | 要 | 5–8 | 3日 |
| **P1** | ⑧'需給を新規領域に作る | 新規バケット・新規表 | なし | P0 に含む | 6–9 | 7日 |
| **T-RAW** | ⑤原本バックフィル（P1 以降並行可） | 新規バケット・新規表 | なし | 不要 | 5–7 | 14日（投入期間） |
| **P2** | 信用残 互換シムの writer 移管（第0波） | vwap-data | **あり** | 要 | 4–6 | 14日 |
| **P3** | ⑨優待（第1波） | D1 既存表 | **あり** | 要 | 3–5 | 14日 |
| **P4a** | ①銘柄マスタ **列追加のみ**（第2波前半） | D1 既存表 | **あり** | 要 | 2–3 | 14日 |
| **P5** | ③断面 + 年次 + 財務サマリ（第3波） | D1 既存表 | **あり** | 要 | 6–9 | 21日 |
| **P4b** | ①**母集団拡張 +725行**（第2波後半） | D1 既存表 | **あり** | 要 | 1–2 | 14日 |
| **P6** | ②日足 writer 移管 + 指数新設（第4波） | vwap-data・D1 | **あり** | 要 | 8–12 | 28日 |
| **P7** | swing 系の縮小（第5波・不可逆 DROP を含む） | D1 既存表 | **あり** | 要 | 6–9（大半は kabulab-cf 側） | 28日 |
| **P8** | ④開示メタ（第6波） | D1 既存表・37,338行 | **あり** | 要 | 7–10 | 28日 |
| **PX** | vwap リポジトリのアーカイブ完了 | GitHub のみ | なし | 要 | 1 | 即日 |

合計 実装 **54〜82 人日**（見積り。実測ではない）。観測窓を直列で通すと **5〜7ヶ月**。P1 と T-RAW は他と並行可、P7 は kabulab-cf 側の作業が主。

**保留（当面 kabulab-cf のまま。本計画で触らない）**: 有報 iXBRL 系3表、5分足、マクロ判定、RSI パーセンタイル、swing のシグナル・スクリーニング・セクター集計、優待スコア。

## 2.1 P4 を2分割した理由（レビュー F5）

kabulab-cf の日次 cron は「①マスタの active 全件」を処理対象にする。①を 3,818 → 4,543 に拡張すると、**切替が済んでいない状態で旧 writer が拡張母集団を掴む**。yfinance 取得と D1 rows written が約1.19倍になり、PER/PBR/ROE を持たない 725件が断面テーブルに NULL で積まれる。

→ **P4a（列追加 + 既存3,818行への銘柄種別の充填）と P4b（+725行の INSERT）に割り、P4b を P6 の直前に置く。** あるいは P4b と同時に kabulab-cf 側の処理対象を「銘柄種別 = 内国普通株」に絞る改修を入れる。どちらを採るかは実装時に決めてよいが、**P4b を P5 より前に置いてはならない**。

---

# 3. 各フェーズの確定仕様

## P0 — 準備（既存に1バイトも書かない）

**着手条件（P0 完了条件ではない）**
- **Workers プランを Free / Paid のいずれか確定させる。** kabuMCP は Paid 専用機能を宣言して本番稼働している一方、kabulab-cf の設定コメントは「Paid を使わない」と明記しており、両者の記述は矛盾している。プランはアカウント単位なので両立しない。
- **D1 の現使用量を実測する**（`wrangler d1 info`）。Free 枠はアカウント共有で、他 Worker の使用分が先に食っている可能性がある。**現使用量を測らない限り Free/Paid の判断そのものができない。**

**成果物**
1. cloud 層（D1 REST クライアント / R2 S3互換クライアント / キー生成 / 契約検証 / M1–M4 ガード）。`CF_WRITE_MODE=off|shadow|live` の3値、既定 `off`。
2. R2 バケット2本の新規作成とライフサイクル設定。
3. API トークンを**最小権限で分割発行**（新規バケット用 / vwap-data 用は P2 まで発行しない / D1 は対象1DBのみにスコープ）。
4. D1 に**新規テーブルのみ**作成（鮮度・ジョブログ・writer claim）。既存表への `ALTER` は P3 以降。
5. プレフィックス契約ファイル（削除・改名禁止キーの機械可読一覧）を配置。
6. **原本メタへの `doc_id` 追加**（レビュー F2）。現行の原本メタは `doc_id` フィールドを持たず、ローカル命名は (scope, data_date) 単位。実測で `edinet_pdf_8306_20240729` に 250個、`…_20231016` に 105個の別内容原本が存在し、**(scope, data_date) では文書を一意に指せない**。EDINET は docID、TDnet は PDF ファイル名を埋める。これは T-RAW と公開 API の `links.raw` の前提条件。

**検証**
- `CF_WRITE_MODE=off` で既存テストと全ジョブの dry-run が現行どおり通る（回帰ゼロ）。
- D1 トークンで新規表の作成が通る。**既存表に対して発行した SQL が SELECT のみ**であることを、クライアントが出す SQL 全文ログで目視確認する。
- 契約検証器に**現在 R2 にある実オブジェクト**をかけ、既存データが自分の契約を満たすことを先に確認する。

**ロールバック**: 新バケット2本を削除、新規表を DROP、トークンを失効。既存に触っていないので影響ゼロ。

---

## P1 — ⑧'需給を新規領域に作る（撤退コストがゼロの窓を使う）

未コミットの ⑧'需給は Notion 側も 0 行、stockStock は CF リソースを1つも持たない。**まだ何も二重になっていない**ので最初から確定配置で作る。

**なぜ P2 より前か**: 日証金は毎営業日取得でき**過去分が残らない**ため、開始が1日遅れるごとに1日ぶん永久に失われる。かつ JPX が 2026-09-28 に様式変更する局面で、独立した第2の需給ソースを先に確保しておくと P2 が失敗しても需給データが途切れない。

**成果物**: 日証金4CSV のコレクタ、毎営業日 12:00 JST の需給ジョブ、per-code 時系列と断面表への書込。

**検証**
- 3営業日連続で全4,351銘柄が書けること。欠測日ゼロ。
- **日証金 CSV の実ヘッダ文字列と列名の1対1対応を原文突合で確認する**（本設計時点で未検証）。
- 主キー（申込日・銘柄コード・取引所区分名）の重複がゼロ。複数証券を持つコードで ISIN 別に行が保持されていること。
- 既存 vwap-data へは**1リクエストも発行していない**ことをクライアントログで確認。

**規約**: 日証金は「第三者の利用に供することを固く禁じます」と明文がある。バケットを分離し、公開 Worker にバインドせず、ライセンスタグを personal-only 固定にすることを**バケット分離とコードの両方**で担保する。

---

## T-RAW — ⑤原本バックフィル（P0 の `doc_id` 追加が前提）

ローカルの原本 70,463件 13.8GB を R2 へ、索引を D1 へ投入する。

- 派生は Parquet と txt のみ。**tidy CSV（9.0GB）は R2 に置かない。**
- **yfinance 日足バッチ CSV は投入しない**（Yahoo から再取得可能・per-code 日足と同一内容）。除外は datatype ホワイトリスト方式で担保する（ブラックリストにしない）。
- 投入は S3互換 API。1日 10,000件ずつ7日に分割。

**検証**: 索引の行数 = R2 のオブジェクト数 = ローカルのファイル数（除外分を引いた数）が3つとも一致。無作為30件の SHA256 を R2 から GET して再計算し一致を確認。

---

## P2 — 信用残 互換シムの writer 移管（2026-09-28 が強制イベント）

JPX は 2026-09-28 から週次→毎営業日16:00へ変更し、様式も変える（金額行の追加・上場比の追加・銘柄コード順化）。kabulab-cf と stockStock の**どちらのパーサも旧様式前提**なので、どのみち 09-28 前に改修が要る。ならば改修を1箇所（stockStock）で行い同時にカットオーバーする。

**カットオーバーが構造的に無衝突になる**
```
2026-09-26(土) 18:00 JST  kabulab-cf が最後の週次分を書く
2026-09-26(土) 19:00 JST  kabulab-cf の該当 cron 行をコメントアウトして push
2026-09-28(月) 17:00 JST  stockStock が初の日次分を書く
```
旧 writer の最後と新 writer の最初の間が **48時間空く**。

**成果物**
1. パーサの**新旧両様式対応**。様式判定は行の数値トークン個数とヘッダ文字列で行い、判定不能な行は**捨てずに品質=要確認で記録**する（推定禁止）。
2. `--kind weekly|daily` 対応。
3. 後方互換拡張は既存5キーを不変に保つ。**銘柄表記名と ISIN は行に足さない**（serving §7.5。`/api/margin` が行を丸ごとスプレッドするため公開面の拡大になる）。
4. **並び順の規約は導入しない（既定）。** PDF 出現順をそのまま保存し、`row_index` を足して順序の来歴を明示する。理由: `/api/margin` は最初に一致した1行を返すので、順序を規約化しても現行と一致する保証が無く、重複6コード（2593/5076/7550/9201/9202/9434、9434 は3行でうち2行は buy/sell とも0）で画面の数値が動きうる。導入する場合は下のゲートを全通過した場合のみ。

**カットオーバー前ゲート**
- **G-margin-1（find() 等価性）**: 既存の直近週を入力に、旧パーサ出力と新パーサ出力それぞれで全コードの「最初に一致した行」を突き合わせ、**4キーが完全一致しないコードがゼロ**であること。並び順規約を入れるならこのゲートの全通過が必須条件。
- **G-margin-2（契約キー）**: 契約ファイルによる機械検証。
- **G-margin-3（読み口）**: 全コードの `/api/margin` レスポンスを切替前に保存し、切替後の差分が**「新規追加された1日ぶん」だけ**であること。
- **G-margin-4（既存週の不可侵）**: 既存10週の ETag / size が切替前後で1バイトも変わっていないこと。
- **G-margin-5（外部読者）**: 外部読者の信用残ローダは行辞書の全キーから DataFrame を構築してから列を絞る実装（`margin.py:117-127`）。**追加キーを入れる場合は実データでの読み取りを切替前に必ず通す**（推論で Null dtype になった列が後続行の整数で落ちる失敗モードがある）。

**既存10週の保全（再取得不能: 2026-06-12〜07-31。07-03・07-10 は恒久欠測）**
- **CF 上にバックアップコピーを作らない**（ユーザーの一次制約への直接違反になる）。守るべきは「削除しない」ことだけであり、それは (a) クライアントに削除メソッドを実装しない、(b) 当該プレフィックスに expiration ルールを置かない、の2点で足りる。
- バックアップが必要なら **CF 外**（ローカルまたは端末B）に置く。

**ロールバック**: 新様式パースに失敗したらその日は書かない（欠測として記録）。既存10週は無傷。旧 writer に戻しても同じ様式変更で失敗するため、ロールバック先は「旧 writer」ではなく「欠測」。

---

## P3 — ⑨優待（壊れる画面が無い）

優待テーブルへ書く自動 writer は**存在しない**（取得スクリプトはどのワークフローからも呼ばれておらず、データは 2026-06-22 で停止）。shadow すら不要で、**旧 writer の停止操作自体が無い**。切替の中で唯一「止めるものが無い」フェーズ。

**再取得不能資産の保全（最優先）**

> 2026-09-12 実測で修正: 当初「LLM 推定4列」と書いていたが、**本番 D1 に存在するのは
> `estimated_value` と `short_summary` の2列だけ**。`estimate_value_source` /
> `estimate_source_url` は `drizzle/d1/0005_sweet_rhodey.sql` が追加する列だが
> **未適用**（`d1_migrations` テーブルも存在しない）。SQLite は解決できない
> 二重引用符付き識別子を文字列リテラルとして返すため、kabulab-cf の relational
> query はエラーにならず、両列に列名そのものの文字列が入っていた。

保全対象の2列はローカル LLM + 楽天市場API の産物で再現不能。かつ kabulab-cf の月次再構築が「金銭価値が非 NULL」でフィルタして優待利回り→スコアを作っているため、失うと 002 の並びが変わる。

1. 切替前に全件 JSON で吸い出し R2 へ退避 → `jobs/yutai_backup.py`（`workflow_dispatch` のみ）。
   - 置き場所は **`jp-stock-supply/backup/yutai/{sha16}.json`**。personal-only なので
     公開 Worker が bind していないバケットへ隔離する（第0層の防御）。
     本文 §原本キー の `raw/minkabu/yutai_monthly/…` は**みんかぶ月次取得の原本**用で、
     その取得自体を採用しないため使わない。
   - キーは**内容の SHA256 だけ**で決まる。同じ中身を何度流しても R2 上の
     オブジェクトは増えない（重複禁止の原則を満たす）。取得時刻は本文に入れず
     索引 `backup/yutai/index.json` 側のエントリに持つ。
   - 退避対象の列は `EXPORT_COLUMNS` に固定し、**`description` は選択列に入れない**
     （出典掲載文そのもののため）。テストで固定。
2. UPSERT の `SET` 句ホワイトリストに保全2列を**含めない**。
3. **`DELETE` を1回も発行しない**（現行の「既存削除→クリーンインポート」方式を踏襲しない）。
4. 「掲載されなくなった優待」は行を消さず、基準日が更新されないことで表現する（取りこぼしと廃止を区別できないため推定で消さない）。
5. **「金銭価値が非 NULL の行数」を毎回ガードで突き合わせ、減ったら退避せず失敗させる**
   （`cloud_store/yutai.check_floors`）。基準は 2026-09-12 実測の
   8,314行 / 推定額あり 5,333 / 要約あり 8,314。優待廃止で自然に減ることはあるため
   絶対下限は基準の 0.9 倍とし、加えて**前回退避より減っていたら止める**
   （意図した減少は `--allow-shrink` で明示する）。

**規約上の是正（同じ工程で必ず行う）**
みんかぶ掲載文を公開ページに全文レンダリングしている状態は、逐条確認で S2 相当（利用規約 第14条1項に直撃）と判定済み。**writer 切替と同じ PR で是正する。** 実読の結果、影響箇所は当初把握の1箇所ではなく **6箇所**（`otakara-yutai/app.ts:147,156 / 509,535,680 / 849,856 / 986,995 / 1218`）で、銘柄詳細だけでなく**一覧の説明文結合と API 出力**も含む。列自体は内部用に残す。

→ kabulab-cf PR #10 で是正済み（マージは未）。あわせて次も直した。
- `/stocks/:code` の relational query に `columns:` を付け、`description` を D1 から引かない
- `/screening` のクライアント描画（`innerHTML` 連結）に `esc()` を追加。`description` には
  山括弧が 0 件だったが `short_summary` には実在するため、切替で初めて踏む描画欠落を塞いだ
- `fetch-yutai-full.ts` の「全削除→再 INSERT」が `short_summary` / `estimated_value` を
  落としていた。掲載文という代替表示が無くなったので、削除前に
  `(銘柄コード, description)` の内容アドレスで退避して復元するようにした
- 死にコード（`src/routes/` `src/views/`）に残っていた `?? b.description` も塞いだ

**未決（ユーザ判断待ち）**
- `services/otakara-yutai/data-scripts/data/benefit-descriptions.jsonl`（掲載文 5,694 行・3.2MB）が
  **public リポジトリの `origin/main` に commit 済み**。公開面から外しても、ここが残る限り
  「掲載内容の公開」は解消しない。除去には履歴書き換え + force push が必要。
- `drizzle/d1/0005` の適用可否。未適用のままだと `apply-benefit-interpretations.ts`
  （`short_summary` を書き戻せる唯一の経路）が `no such column` で動かない。

みんかぶのライセンスタグを personal-only 固定から緩める根拠は規約内に存在しないため、**タグは維持する**。

**検証**
- LLM 推定の金銭価値が非 NULL の行数が切替前後で**完全一致**（1行でも減ったら即ロールバック）。
- 要約列の全文チェックサムが切替前後で一致。
- 権利月の値集合が展開前の集合と一致。
- 002 の代表20 URL のレスポンス差分が意図した行追加のみ（掲載文の除去は意図した差分として扱う）。

---

## P4a — ①銘柄マスタ 列追加のみ

**絶対規則**
1. `ON CONFLICT(code) DO UPDATE` のみ。**サロゲートキーを SET 句に含めない。**
2. `TRUNCATE` / `DELETE` / `DROP` を発行しない（**14子表**、うち多くが cascade で消える）。
3. 対象外化は非アクティブ化の UPDATE のみ。
4. **4条件ガードを移植**（JPX 行数下限 4,000 / 内国株式 3,000 / 既存 active 比 98% / 1 run の対象外化が既存 active の 2% 上限）。1つでも破れたら書込ゼロで異常終了。**(c)(d) の分母は total ではなく is_active=1 の件数**。なお「現行の被覆率下限 0.5」は stockStock 側 `master_sync.MIN_CODELIST_COVERAGE`（EDINET コードリストによる上場廃止検知）の値で、移植元 kabulab-cf は既に 0.98。別物なので混同しないこと。

**検証（shadow で3回連続）**
- **G-core-1（id 不変）**: 書き込み SQL を**実行せずダンプ**し、サロゲートキー列が SET 句に一度も現れないことを静的に検査。
- **G-core-2（キー集合）**: 旧にあって新に無い code がゼロ。
- **G-core-3（値一致）**: 名称・市場・業種の完全一致率。不一致は1件ずつ原因を特定する（JPX と EDINET の表記ゆれ）。**不一致を自動で片方に寄せない。**
- **G-core-4（ガード発火）**: JPX 行数を意図的に下限未満に細工した入力で、書込ゼロで異常終了することを確認。
- **G-core-5（子表の孤児ゼロ）**: 切替後に**全14子表 + `jss_financials` の soft 参照 = 15表**で孤児が0件。

**ロールバック**: 切替前に取得する全件スナップショットから active フラグだけを戻す UPDATE。追加列は使わなければ無害なので DROP しない。

### 実施記録（2026-09-12）

`jobs/core_stocks_migrate.py --apply` で 14 文（`ALTER TABLE ... ADD COLUMN` 12 +
`CREATE INDEX` 2）を発行。**値の充填は行っていない**（後述の理由で P4a の範囲外）。

適用前後の実測:

| | 適用前 | 適用後 |
|---|---|---|
| 列数 | 9 | 21 |
| 索引 | `core_stocks_code_unique` のみ | + `idx_core_stocks_active_market` / `idx_core_stocks_edinet` |
| total / active / sector NULL | 3,818 / 3,715 / 62 | 3,818 / 3,715 / 62（不変） |
| `sqlite_sequence.seq` | 14,455 | 14,455（不変） |
| 15表の孤児 | 0 | 0 |
| 追加12列が非 NULL の行 | — | 0 |

全 3,818 行のスナップショット（`id/code/name/market/sector/is_active/is_yutai`）を
適用前後で取得し、**完全一致**を確認（G-core-2 / G-core-3）。
kabulab-cf は `pnpm typecheck` 通過、本番 11 経路がすべて 200。

**先に実測で確かめたこと**（本番と同一 DDL のローカル複製）:
- SQLite の `ALTER TABLE ADD COLUMN` は既定値の無い `NOT NULL` も `UNIQUE` も
  付けられない。したがって**追加12列は全て nullable 必須**
- `core_stocks.name` / `market` が NOT NULL で既定値が無いため、
  「code と新列だけを渡す部分 upsert」は**既存行が相手でも失敗する**
  (`NOT NULL constraint failed`)。よって**充填は `UPDATE` 一択**で、
  `D1Store.upsert()` は使えない（`cloud_store/core_stocks.build_column_update`）
- 列追加後も kabulab-cf の `universe.ts` と等価の upsert（新規 INSERT / 既存 UPDATE）
  は通る

**D1 固有の制約（本番実測）**: compound SELECT（`UNION ALL` 等）の項数上限は
**5**。6 項目から `too many terms in compound SELECT: SQLITE_ERROR` を返す
（素の SQLite の既定は 500）。15 表の孤児検査を 1 文にまとめると必ず失敗するので
3 文に分割している（`cloud_store/d1.MAX_COMPOUND_SELECT_TERMS`）。

**P4a の範囲外にしたもの**: `instrument_type` / `sector33` / `sector17` の値の充填。

着手時点では供給源の JPX data_j が旧 URL (`.../data_j.xls`) で **HTTP 404** を返し、
一次データを正規に取得できなかった（`core_stocks.MAX(updated_at)` は 2026-08-10 で、
9 月の月次 universe sync はこの 404 で失敗していた）。

> **その後、同日に解消済み**: JPX は同じパスで `.xls` → **`.xlsx`** へ差し替えていた。
> 新 URL は `https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx`
> で HTTP 200（kabulab-cf `fix/jpx-listing-xlsx` で追従済み。基準日 2026-08-31 /
> raw 4,441 行 / isListedEquity 3,700 / 4条件ガードすべて通過を実測）。
> したがって充填の前提条件そのものは解消しており、残る判断は
> **stockStock 側に data_j の取得経路を持つかどうか**（`collectors/jpx_universe.py` の新設）。

充填を別フェーズに切ったまま据え置く理由:
- 列追加（DDL・1回きり）と値の充填（UPDATE・繰り返し）で、承認とロールバックの
  単位が違う
- `sector33` の正本ソースが未決（本番 `core_stocks.sector` は JPX 33業種区分と
  **不一致 0 件**で、stockStock の `edinet_codelist.py` は EDINET 提出者業種を
  入れる実装。どちらに寄せるかで `jss_column_license` のタグも変わる）
- `instrument_type` の語彙が data_j の「市場・商品区分」から 1:1 に導けない
  （`ETF・ETN` は 1 区分で etf/etn を分離不能、`REIT・ベンチャーファンド・
  カントリーファンド・インフラファンド` も同様、`出資証券` は 7 語彙のどれにも
  当たらない）。**A-1 の語彙表を先に直す必要がある**

---

## P5 — ③断面 + 年次 + 財務サマリ

**前提条件（これが片付くまで着手しない）**
断面テーブルの営業利益率・EPS・BPS・ROE・ROA の供給経路を確定する。
- 現行のバリュエーション取得は 時価総額 / PER / PBR / 配当利回り の4キーのみだが、実装は情報オブジェクトの返り値をそのまま引く形なので、**キーを足すだけで供給できる見込み**。
- **ただし実レスポンスに当該キーが存在するかは未検証**。P5 の最初のタスクは実レスポンスでの存在確認。
- 存在しなければ財務サマリの最新決算期から join して埋める案に倒すが、その場合 kabulab-cf の既存値（TTM 生値）と**定義が変わり 001/002 の画面数値が動く**。

**年次断面の扱い（重複解消）**
年次断面は財務サマリの本決算部分集合であり、財務サマリの主キー先頭で直接引ける。既存15,943行との互換だけが残す理由なので、**列を11本足して太らせるのをやめ「現状維持」とする。** 新規の消費者は財務サマリを引く。既存消費者を洗い出して向け終えたら DROP する、を別チケットにする。

**検証（3回連続）**
- **G-fin-1（値一致）**: 数値列の相対誤差 ≤ 1e-6、NULL は NULL と一致。**両者が別時刻に Yahoo を叩くため、基準日が一致する行だけを比較し、一致しない行は「比較不能」として別カウントする。**
- **G-fin-2（カバレッジ）**: 新 writer が書けた銘柄数 ≥ 旧の 99.5%。
- **G-fin-3（読み口）**: 001 のスクリーニング（3値×3ソート）、002 の一覧（6ソート）、005 のスクリーニングの SSR HTML が切替前後で一致。
- **G-fin-4（外部読者）**: 外部読者の財務取得が返すデータフレームが切替前後で一致。

**前提となる kabulab-cf 側の改修（P5 のブロッカー）**
kabulab-cf の日次 cron は断面テーブルと同時に RSI パーセンタイル・swing 系8表も書く。**全部は止められない。** → `daily.ts` を「stockStock が書く部分を skip する」ようにフラグで分割する改修が必要。**これが入らないと P5 のカットオーバーができない。**

---

## P4b — ①母集団拡張（+725行。設計当初の「+627」は誤り）

> 2026-09-12 実測で訂正: 「+627」は `4,445 − 3,818` の単純引き算で、集合差では
> ない。**現行配布の `data_j.xlsx`（基準日 2026-08-31、raw 4,441 行 /
> 4文字コード 4,434）と本番 `core_stocks`（3,818 / active 3,715）の集合差**は:
>
> | | 件数 |
> |---|---|
> | data_j にあり core に無い（= INSERT 対象） | **725** |
> | core にあり data_j 4文字集合に無い（= 余剰） | **109**（うち active 7） |
> | P4b 後の総行数 | **4,543** |
>
> INSERT 725 件の内訳: ETF・ETN 477 / PRO Market 187 / REIT等 54 / 外国株 5 /
> グロース（内国株式）1 / 出資証券 1。
>
> **どの基準日の data_j で測ったかを必ず併記すること。** 基準日が変われば
> 数字も変わる（2026-05-31 版では 707 / 80 / 4,525 だった）。余剰 109 行の扱い
> （残す / `is_active=0` にする / 最新 data_j で自然解消を待つ）は P4b の設計時に決める。

P5 完了後、P6 の直前に実施する（§2.1）。

- 銘柄種別（内国普通株 / ETF / ETN / REIT / PRO / 外国株 / 出資証券）を埋め、母集団を 4,445 にする。
- これは P6 で「stockStock の母集団で日足を上書きして ETF を更新停止させる」事故を**型で防ぐ**ための前提。
- **副作用の明示**: 001/003/004/005 の一覧件数が +725行になる。ETF/REIT/PRO に PER/PBR/ROE は存在しないので、「欠損」ではなく「対象外」として扱う値が品質区分に無い（要検討）。002 は優待フラグで絞っているので影響を受けない。
- kabulab-cf の日次 cron が拡張母集団を掴む副作用（取得 1.16倍・NULL 行の積み上がり）を受け入れるか、処理対象を内国普通株に絞る改修を同時に入れるかを選ぶ。

---

## P6 — ②日足 writer 移管 + 指数新設

**前提: 取得期間を短縮してはならない（レビュー F1）**
テクニカル計算の実装を実読すると、SMA75 は 75本、SMA200 は 200本、52週高安は**暦日 364日以上**が無ければ None を返す。取得期間を 3ヶ月（≒62営業日 / 92暦日）に縮めると、断面テーブルに載せる SMA200・52週高安・既存 SMA75 が**全4,445銘柄で恒久的に NULL** になる。さらに分割検出の窓（260営業日）が 62本に縮み、3ヶ月より古い分割を永久に検出できなくなる。

→ **処理順を仕様として固定する。**
```
(1) yfinance を 3mo で取得（欠測追いつき用の差分取得）
(2) R2 の per-code 日足を GET
(3) (1) を (2) にマージ
(4) マージ後の長期系列（10年）でテクニカルを計算する   ← ここが必須
(5) R2 へ PUT + D1 断面へ UPSERT
```
分割検出も (4) のマージ後系列で 260営業日を見る。「転送量 1/8」の利得はこの順序でも維持される。

**成果物**
1. 分割情報の取得を追加（現行のコレクタには分割取得の実装が1行も無い）。
2. per-code 日足の read-merge-write（**M3 の後退禁止ガード必須**）。
3. 指数・為替・先物の新設。**スロットに日経VI を含める**（レビュー F9）。含めないと、マクロ判定の入力5系統のうち日経VI が欠ける。
4. 調整値の由来を2値で明示する（推定しない）。
5. **銘柄種別を公開オブジェクトに入れない**（serving §7.5。母集団事故の防止は PUT 前のコード側ガードで行う）。

**母集団の罠**: per-code 日足の母集団は①マスタ（従来3,818）ではなく静的ファイル（4,445件・ETF/ETN 466 + PRO 181 + REIT等63 + 外国株5 + 出資証券2 を含む）。stockStock の母集団で置き換えると ETF 1321 が更新停止し、外部読者（日経平均代理に 1321 を使う検証リポジトリ）が直撃する。
→ P4b 完了を前提に、**書く前に「対象コード集合 ⊇ R2 に既に存在する日足キー集合」を検査し、満たさなければ1件も書かずに異常終了**する。

**検証（shadow 200件・全銘柄種別を網羅）**
- G-daily-1（後退なし）: 全件でバー数が減らず、期間の始端が後退しない。
- G-daily-2（値一致）: 直近20営業日の OHLCV が完全一致。**調整値は分割で遡及改訂されるため、不一致があれば分割イベントに紐づくかを確認し、紐づかない不一致がゼロ**であること。
- G-daily-3（契約キー）: 契約ファイルによる機械検証。
- G-daily-4（母集団）: 対象コード集合が既存キー集合を包含し、ETF 1321 を含む。
- G-daily-5（外部読者）: 外部読者が調整値の欠損行を除外する実装のため、**除外行数が切替前後で増えていない**こと。
- G-daily-6（読み口）: `/api/daily` を代表200件で切替前後比較。

**未決の扱い**: 分割が起きたとき過去の調整値をどうするか（全期間再取得して上書きするか、古いまま残すか）は**未決定**。現行の kabulab-cf 側の取込も同じ問題を抱えている。全期間再取得は M3 とは衝突しないが Yahoo へのアクセス量が跳ねる。

**前提となる kabulab-cf 側の改修（P6 のブロッカー）**: 日足と5分足が同じ cron / 同じステップで回っている。**5分足を別 cron / 別ステップに切り出す改修が先**。これを忘れると「日足を止めるつもりが5分足も止まる」。

**ロールバック**: 切替前に日足全件を R2 の原本領域へ**コピーせず**、ローカル（または端末B）へ退避する（CF 上に二重に置かない）。旧 writer の cron を戻せば writer も戻る。

---

## P7 — swing 系の縮小（不可逆な DROP を含む）

作業の大半は kabulab-cf 側。**4段に分解する（レビュー F4）。**

日次 cron は起動直後にスキーマ健全性チェックを無条件で呼び、日足テーブルの調整値列が SELECT できないと throw する（`daily.ts:389-399, 407-414`）。**DROP した瞬間に断面・年次・RSI パーセンタイル・swing 系4表・マクロ・セクター集計の計8テーブルが同時に更新停止する。**

```
(1) 003 と 004 のクエリを per-code 日足の binding 読みへ書き換え
(2) daily.ts のスキーマアサーションから当該列のチェックを外す   ← これを飛ばすと全死
(3) daily.ts の当該テーブルへの書込を削除
(4) 28日観測
(5) DROP
```

**otakara の断面派生テーブルは VIEW にしない（レビュー F3）。** 月次 cron が当該テーブルと優待スコアテーブルへ**同一ループ内で INSERT している**（`monthly.ts:125, 170`）。VIEW には INSERT できないので Phase 2 全体が例外で落ち、002 の並び順の正本である優待スコアの再構築も止まる。物理的な重複は 1,645行・約0.5MB で、VIEW 化で得るものより壊すリスクのほうが大きい。→ **実テーブルのまま残し、入力3つ（断面・swing 指標・優待）が stockStock 由来になる形で正本を統一する。** どうしても消すなら月次 cron の書き換えを同じ PR に入れ、波の表に「kabulab-cf 側の改修」として明示する。

**その他**
- swing 指標テーブルからの重複12列削除は 003/004/002 のクエリ書き換え後。**`SELECT *` 依存を全走査していない（未確認）**。SQLite の列削除は索引・VIEW・トリガ・生成列が参照していると失敗し、再追加すると列順が変わる。
- finmath のオンデマンドキャッシュ2表の DROP は**任意に格下げ**する。合計 7,194行で容量上の意味はほぼ無く、指数スロットの日経VI が揃うまで急ぐ理由がない。
- DROP の前に必ず対象テーブルをローカルへエクスポートする。**D1 の Time Travel には依存しない**（テーブル単位ではなく DB 全体の巻き戻しになるはずで、他サービスの正常な書込も巻き戻る。**挙動は未確認**）。

**Free プランの場合の順序問題**: 004 のモメンタム画面は日足テーブルを昇順全走査（1回 336,185行）する。Free の日次 5,000,000 rows read に対し**1日15回の画面表示で枯渇する**。P7 が最後から2番目なので、Free を選ぶと移行の全期間（5〜7ヶ月）この状態が続く。P7 を先頭に持ってくるには P4/P5/P6 の完了が前提という循環があるため、**Free を選ぶなら実質的な解は「Paid に上げる」しかない。**

**検証**: 001〜007 の全ページ（live 5本 + 直URL到達可能な soon 2本）の代表 URL 50本の SSR HTML を DROP 前後で比較。差分ゼロ。

**ロールバック**: エクスポートから再投入。日足テーブル 336,185行は Free の日次 rows written 100,000 で**4日**かかる。つまり実質的にロールバックが高コストなので、28日の観測窓を完走させてから DROP する。

---

## P8 — ④開示メタ（冪等キーの不一致が最大の罠）

kabulab-cf は外部 API の生 id を一意キーにし、stockStock は URL から導出した PDF ファイル名をキーにする。**このまま writer を移すと既存37,338行と一致せず全件が二重挿入される。**

**救い**: URL からキーを導出する処理は**純粋な文字列操作**（HTTP アクセス不要）なのでバックフィルできる。

**バックフィル手順**
```
1. キー列を追加（UNIQUE はまだ張らない）
2. 全37,338行の URL を 1,000件ずつページングで SELECT
3. ローカルで導出関数を適用（HTTP なし）
4. 1,000件バッチで UPDATE
5. 重複検査。0件でなければ UNIQUE を張れない → 既存データ側の重複なので手で解消（承認必要）
6. UNIQUE 索引を作成（NULL は複数可）
7. 導出できなかった行は NULL のまま残す（推定禁止）
```

**writer の分割（M2 の適用）**: stockStock が行の作成と基本列、kabulab-cf がタグ2列と感情スコア3列を**既存行の UPDATE のみ**で埋める。stockStock の UPSERT は新キー優先、NULL なら旧キーにフォールバック（両キーで冪等化）。**タグの格納形式は変えない**（外部読者が JSON 配列文字列前提の部分一致検索をしている）。

**検証**
- G-ir-1: バックフィル後のキー充足率。導出不能行の URL の形を目視確認。
- G-ir-2: 切替後3営業日、行数増分が処理件数と一致（二重挿入ゼロ）。
- G-ir-3: 006 の代表30 URL を切替前後で比較。
- G-ir-4: 外部読者のタグ検索と最新日時取得が切替前後で整合。
- G-ir-5: タグが NULL の行が増え続けていないこと（kabulab-cf の enrich が新規行を拾えている）。

**P8 までの期間、④の正本がどこにも無い（レビュー F6）**
配置表は「④は D1 が正本」と断言する一方、本計画は「第6波まで stockStock は書かない」と決めている。Notion は同じ設計で「サブに降格」済み。つまり P1〜P7 の数ヶ月間、④は宙吊りになる。かつ書込をスキップする実装が成功扱いを返すと、鮮度テーブルに行が出ず**誰も気づかない**。
→ 解決の選択肢は2つ（**判断事項 #2**）。少なくとも「スキップ時は鮮度テーブルに行を作らない」ことで検知可能にするのは**確定仕様**とする。

---

## PX — vwap リポジトリのアーカイブ完了

未追跡ワークフローとスクリプトの退避は既にコミット済み（`79642b6`）。残る作業は4つ。

1. **未コミット差分7ファイル（+132/−34）の処理**。捨てると吸収されていない改修が消える可能性がある。**差分の内容は未確認** → 確認してからコミットするか退避する。
2. **週次のユニバース再生成ワークフローの停止**。これは今も生きている。**止める前に、外部の検証リポジトリが参照しているパスを切る**（参照先を kabulab-cf 側の静的ファイル、または①マスタ由来の生成物へ）。
3. GitHub リポジトリを Archived にする（Actions も発火しなくなるので 2 の代替にもなるが、**先に 2 の参照切りをやる**）。
4. **kabulab-cf の `.env` は絶対に消さない**。外部の検証リポジトリが R2/D1/EDINET の資格情報をここから読んでおり、存在しないと即死する。トークンのローテーションでも静かに壊れる。

---

# 4. 重複の解消（確定した是正）

| 項目 | 是正 |
|---|---|
| バルクエクスポートの株価履歴 | **R2 に置かない。** per-code 日足と同一粒度・同一期間の重複で、容量試算にも未計上だった（2.9〜3.9GB）。エクスポート索引に日足プレフィックスのポインタ（キー一覧と各銘柄の最終日）を置く。CSV は R2 に置かず Notion 添付用のローカル一時ファイルとする |
| 信用残の互換シム | **廃止条件に期限と担当波を入れる。** 具体的には「P6 完了と同じ PR で 007 の該当 API を per-code 読みへ変更し、外部読者のローダも per-code へ変更する」を P6 の作業項目に含める。両リポとも自分の道具なので外部調整は不要。廃止できた時点で新規書込を停止し、既存オブジェクトは残す |
| 既存10週のバックアップ | **CF 上にコピーを作らない。** 守るべきは「削除しない」ことだけで、削除メソッドを実装しない + expiration を置かない、で足りる。バックアップは CF 外に置く |
| 年次断面 | **列追加をやめて現状維持**。新規消費者は財務サマリを引く。既存消費者を向け終えたら DROP（別チケット） |
| otakara の断面派生 | **VIEW 化しない**（P7・月次 cron が INSERT する）。入力3つの正本統一で代替 |
| 銘柄名検索の索引 | **新規に作らない。** kabuMCP の既存シャード索引を再利用する（serving §2.4） |
| TDnet 一覧 JSON の毎時スナップショット | 同一基準日について内容が少しずつ異なるスナップショットが最大11件/日できる。既存の重複回避は「同一内容の別日再取得」しか解いておらず「増分だけ異なる同日再取得」を解いていない。**実装時に保持方針を決める**（最終版のみ保持するか、全件保持して容量を許容するか）。ローカル側は同一 (scope, date) の3つ目の別内容で例外を投げる実装のはずなので、実際に23件発生している経緯の調査も必要 |

---

# 5. writer を止める判断のゲート（共通）

旧 writer の cron を止めてよいのは、**以下6つを3サイクル連続で満たしたとき**だけ。

| | ゲート | 判定 |
|---|---|---|
| G1 | カバレッジ | 新 writer の書込数 ≥ 旧の 99.5%。欠落キー一覧を出し、母集団差以外の欠落がゼロ |
| G2 | キー集合 | 旧のキー集合 ⊆ 新のキー集合 |
| G3 | 値一致 | 数値は相対誤差 ≤ 1e-6、文字列は完全一致、NULL は NULL と一致。不一致は1件ずつ原因を特定し**自動で片方に寄せない** |
| G4 | 契約キー | 契約ファイルの必須キーが全件に存在 |
| G5 | 読み口の等価性 | 公開エンドポイントのレスポンス body の差分が「意図した追加」だけ |
| G6 | 3連続 | G1〜G5 を3回の定期実行ぶん連続で満たす |

比較ツール（新規・読み取り専用）は D1 / R2 / エンドポイントの3モードを持つ。値の突合は**キー単位のハッシュではなく列単位の差分**を出す（ハッシュだけだとどの列がずれたか分からず原因特定に戻れない）。

**日足テーブル（336,185行）は D1 比較の対象にしない**（1回で Free の日次 rows read の 6.7%）。P7 は G5 だけで判定する = **データ層の値一致を直接は確認しない穴が残る**。

---

# 6. 既存データの扱い

| 既存データ | 扱い | 根拠 |
|---|---|---|
| per-code 日足10年（4,444件 0.694GB） | **そのまま活かす。再生成しない** | 10年×4,445銘柄の再取得は無意味に重く、調整値は遡及改訂されるため再取得すると過去値が変わる |
| ④開示メタ 37,338行（2023-06-05〜） | **そのまま活かす**。キーをバックフィル | TDnet PDF は約31日で purge され再取得不能。導出は純文字列操作で HTTP 不要 |
| ⑥エクスポート（Notion 添付） | **移行しない。Notion に残置** | 再生成物。Notion ホストの URL は1時間で失効し外部から物理取得できない |
| 信用残 2026-06-12〜07-31 | **保全（恒久）。ただし CF 上にコピーしない** | JPX から再取得不能。07-03・07-10 は恒久欠測 |
| 優待の LLM 推定4列 | **保全**（退避 + SET 句除外 + DELETE 禁止 + 件数ガード） | 再現不能。優待利回り→スコアの入力 |
| 有報 iXBRL 系3表（59,700行） | **触らない（保留）** | 表構造判定の専用パーサの産物で供給不能 |
| 5分足 | **触らない（保留）** | stockStock に5分足コレクタが存在しない |
| RSI パーセンタイル | **触らない（保留）** | P6 完了後に入力は揃うが第一波に含めない |

---

# 7. 並行稼働期間の二重書込防止（まとめ）

| 層 | 手段 | 検知 |
|---|---|---|
| キー空間 | M1: shadow のみ | 構造的に発生しない |
| 列 | M2: claim で列集合を排他。SET 句ホワイトリスト。**両リポに claim 照合を入れる** | ジョブ冒頭で異常終了 |
| スケジュール | cron 行単位で「旧を止める → 1サイクル空振り → 新を回す」。手動実行は残してロールバック経路にする | 空振り確認で目視 |
| R2 | **429 には依存しない**（M4）。`writer` フィールドと claim の二重照合。切替時に旧トークンを revoke | writer 不一致で abort |
| D1 | 429 が出ないので気付けない。claim 照合が唯一の防御 | 同上 |
| リポジトリ跨ぎ | GitHub Actions の concurrency はリポジトリを跨げない。**CF 側でしか守れない** | — |

カットオーバーは該当 cron の直後（次回発火まで最長の余裕がある時点）に行う。

**GitHub Actions 分の副作用**: kabulab-cf は非公開リポジトリで無料枠 2,000分/月、うち日足・5分足の取込だけで月約800分を使っている。stockStock は公開リポジトリで無制限なので、writer を移すほど kabulab-cf の枠が空く。逆に**並行期間は両方が回るので一時的に枠を圧迫する** → 並行期間の shadow は週3ではなく週1に間引く。

---

# 8. 見積り数値（レビュー再計算を採用）

## 8.1 容量・コスト（5年）

| 項目 | 値 |
|---|---|
| R2 合計 | **約102 GB**（従来試算 73.6 GB の 1.39倍） |
| ─ 内訳の修正点 | 原本のフル年実測は **4.4〜4.8 GB/年**（従来「3.46 GB/年」はバックフィルの薄い年を含む4年平均）。TDnet PDF は開示実績から **年11,420件 × 0.30MB = 3.4 GB/年**（従来「年3万件・9.5 GB/年」は過大）。エクスポートは**削除**（重複） |
| R2 コスト（全 Standard） | **$1.39/月 / $16.6/年** |
| R2 コスト（原本を IA へ） | **$0.94/月 / $11.3/年**。ただし IA 遷移が Class A に計上されるか、公開ファイル配信時の取得課金（$0.01/GB）は**未計上** |
| R2 Class A（書込） | **約228万/年 = 月19万**（Free 1M/月 の 19%）。従来試算は需給の1日1回化を反映しておらず二重計上だった |
| R2 Class B（読取） | **約262万/年 = 月21.8万**（Free 10M/月 の 2.2%）。writer 自身の分だけで確定的に発生する量 |
| D1 容量 | **約507 MB**。Paid(10GB) なら 5.1%。**Free(500MB) は5年で超過する**。縮退（開示メタと原本索引を直近24ヶ月保持）で約280MB（56%）→ Free では縮退は「案」ではなく必須条件 |

D1 の内訳で従来試算とのずれが大きい2点: 原本索引は R2 キー2本と SHA256 だけで本文198B あり 1行200B では収まらない（**205 MB**）。勘定科目の語彙表は変換済み600件のサンプルで distinct 7,677、Heaps' law 外挿で 5年 8〜12万行（**28〜42 MB**。従来「約2万行」）。

**D1 の現使用量は未実測**。上記は新規分のみ。

## 8.2 初回一括投入

**383,000〜423,000行**（マスタ・断面・年次・財務サマリ・原本索引・XBRL 所在・語彙・優待・開示メタのキーバックフィル）。Free の日次 100,000 rows written かつ日常ジョブと同じ枠 → **5〜6日に分割**（従来試算「約22万行 / 3日」は XBRL 系と年次を落としていた）。

## 8.3 所要時間（**CF のみ / CF + Notion の2列で管理する**）

従来の所要時間表は Notion 分を系統的に落としており、Notion 側の設計（サブとしてベストエフォートで書き続ける）と矛盾していた。**どちらの数字も単独では成立しない**ため、以下の2列で管理する。

| ジョブ | 現行 | CF のみ | CF + Notion |
|---|---|---|---|
| ②日次 | 3h26m〜4h11m（req モデルから完全再現） | **17〜21分** | **77〜94分** |
| ①月次 | 約52分 | 約3分 | **60〜73分**（マスタ4,445 + カタログ全置換） |
| ④時次 | 14〜18分 | 約2分 + PDF取得分 | 同左 + Notion 数十 req |
| ③日次(EDINET) | cap 120分 | **60〜80分**（EDINET API の DL が律速。CF 化では減らない） | +31〜38分 |
| ⑥週次 | 約20分 | 約4分 | +1分未満 |
| 需給(日証金) | — | 約2分 | 約2分 |
| 需給(JPX) | — | 約5分 | +60分 |
| ⑨月次 | 約2〜3時間 | 約43分（polite delay が律速・短縮しない） | **153〜178分** |

②日次の内訳（CF 側）: yfinance 日足 7.5分 + バリュエーション（並列8で5.1分 / 逐次81.5分）+ per-code 日足の read-merge-write **4〜8分** + D1 12秒 + ローカル PG 10秒。
**日足の RMW は帯域が律速**: 1ファイル約160KB × 4,445 の GET+PUT = **2.9 GB/日**。実効100Mbps で 3.9分、50Mbps で 7.8分。従来試算「1.9分」はレイテンシだけを見ており実データ量が入っていなかった。
**バリュエーションの並列8が 429 を食わない前提が最も脆い**。逐次に戻ると総所要は 1h31m。並列度を1に戻せるオプションを必ず残す。

**timeout の設定**: ⑨月次は CF + Notion で 153〜178分となり、**180分ではマージンが 2〜27分しかない**。現行の 300分を据え置くか、Notion 書込をジョブ本体から切り離す。他ジョブの timeout 見直しは上記2列表が確定してから行う。

---

# 9. 承認ポイント一覧（他システムが更新しているリソースに触る瞬間）

| # | フェーズ | 内容 | 不可逆性 |
|---|---|---|---|
| A1 | P0 前 | **Workers プランの確定**（Free なら Paid 化の判断を含む） | 可逆 |
| A2 | P0 | R2 新規バケット2本の作成（課金開始） | 可逆 |
| A3 | P0 | D1 への書込トークンを**公開リポジトリ**の Secrets に置く | 可逆（失効可） |
| A4 | P2 | vwap-data（公開 Worker がバインド）への RW トークン発行 | 可逆 |
| A5 | P2 | kabulab-cf の信用残 cron 停止 | 可逆 |
| A6 | P2 | **信用残の行の並び順を変更する場合の受諾**（既定は「変更しない」） | 可逆 |
| A7 | P3 | 優待テーブル（8,314行のライブ表）への初回書込 | 可逆 |
| A8 | P3 | **公開画面・公開APIから優待掲載文を外す**（影響 6箇所） | 可逆 |
| A9 | P4a | ①マスタへの列追加と既存3,818行への銘柄種別充填 | 可逆 |
| A10 | P5 | ②断面の writer 切替 | 可逆 |
| A11 | P5 | kabulab-cf 日次 cron の**部分停止改修**（分割フラグの追加。P5 のブロッカー） | 可逆 |
| A12 | P4b | **母集団の +725行 拡張**（001/003/004/005 の件数が変わる。旧 writer の処理対象も増える） | 可逆 |
| A13 | P6 | per-code 日足の writer 切替（外部読者2つが直読） | 可逆 |
| A14 | P6 | kabulab-cf の日足/5分足の cron 分離改修と日足 cron 停止 | 可逆 |
| A15 | P6/S5 | 007 の信用残 API を per-code 読みへ変更（互換シム廃止の前提） | 可逆 |
| A16 | P7 | スキーマアサーション改修 + 日足テーブルへの書込停止 | 可逆 |
| A17 | P7 | 日足テーブルの DROP | **不可逆**（Free で再投入4日） |
| A18 | P7 | swing 指標テーブルの12列削除 | **不可逆** |
| A19 | P7 | finmath キャッシュ2表の DROP（**任意**） | 不可逆 |
| A20 | P8 | ④開示メタ 37,338行へのキーバックフィル UPDATE | 可逆 |
| A21 | P8 | 重複が見つかった場合の行削除 | **不可逆** |
| A22 | P8 | kabulab-cf の開示取込ステップ停止（同一ジョブ内の別ステップは残す） | 可逆 |
| A23 | PX | vwap リポジトリの Archived 化 + ユニバース再生成の停止（**参照切りが先**） | 可逆 |
| A24 | S1 前 | **既存公開 API の personal-only 無認証配布の是正**（認証を掛ける / 内部ホストへ移す） | 可逆 |
| A25 | S1 | 公開 API / 公開面を立ち上げるという意思決定 | 可逆 |

---

# 10. 全面中止・ロールバック手順（どのフェーズからでも）

1. stockStock 側の書込モードを `off` にする（環境変数1つ。全ジョブの CF 書込が即停止し、Notion + ローカル PG の従来運用に戻る）。
2. kabulab-cf 側の cron 行のコメントアウトを外して push（止めた順と逆順に戻す）。
3. **D1 の追加列は DROP しない**（使われなければ無害。SQLite の列削除はリスクが高い）。
4. 退避スナップショットから復元する対象は3つだけ: ①の active フラグ、⑨の LLM 推定4列、④の重複行。それ以外は旧 writer が次のサイクルで自然に上書きして戻る。
5. **R2 のオブジェクトは削除しない**。旧 writer が read-merge-write で戻すので、キーを消さなければ復元される。

各フェーズの観測窓（14〜28日）は、**この手順を実行できる期間**として設定している。
---

# 残る判断事項

- 【最優先・全体の前提】Workers プランは Free か Paid か。kabuMCP は Paid 専用機能を宣言して本番稼働している一方、kabulab-cf の設定コメントは「Paid を使わない」と明記しており、両者の記述が矛盾している（プランはアカウント単位なので両立しない）。あわせて D1 の現使用量が未実測で、これを測らないと Free/Paid の判断自体ができない。Free の場合、D1 は5年 約507MB で 1DB 500MB を超えるため開示メタと原本索引の24ヶ月保持縮退が必須になり、さらに 004 の全走査（1回 336,185行）による rows read 枯渇を止めるには P7 を先頭に持ってくる必要があるが、P7 は P4/P5/P6 完了が前提という循環になる。実質的な解は「Paid に上げる」だが、$5/月の判断はユーザーのもの。
- 【④開示メタの正本が P8 まで宙吊りになる問題】配置表は「④は D1 が正本」と断言する一方、本計画は「第6波（P8）まで stockStock は D1 に書かない」と決めており、Notion は同じ設計で既にサブへ降格済み。つまり P1〜P7 の数ヶ月間、④の正本がどこにも無い。選択肢は2つ。案A＝ stockStock が先に行を作り（タグを空配列、旧キーに接頭辞付きの代替値を入れる）、kabulab-cf の分類器が UPDATE で後からタグを埋める（M2 の列排他をそのまま適用）。ただし外部読者のタグ部分一致検索が空配列行を拾わないため、タグ未付与行が一時的に検索から漏れる。案B＝「第6波まで④の正本は Notion のまま」と配置表を書き直す。どちらを採るか。
- 【信用残の行の並び順を規約化するか】確定文書は「コード昇順・普通株優先・ISIN 昇順」、別の文書は「普通株かどうかは判定できないので全行 false」、さらに別の文書は「表記名から決定論的に判定」と3通りに分かれており、どれを採っても現行の PDF 出現順と一致する保証がない。007 の該当 API は最初に一致した1行だけを返すため、順序が実質的な公開 API になっている（重複6コード・9434 は3行でうち2行は buy/sell とも0）。本計画の既定は「並び順を導入せず、PDF 出現順のまま row_index を付与する」。規約を導入したい場合は、既存の直近週で旧パーサと新パーサの find() 結果が全4,251コードで完全一致することを先に確認する必要がある。
- 【R2 の公開オブジェクトに追加キーを入れてよいか】実読の結果、007 の日足・5分足 API は R2 の本文をバイト単位で素通しし（passthrough）、信用残 API は行オブジェクトを丸ごとスプレッドする（{week, ...row}）。したがって R2 に足したキーはそのまま無認証で公開される。設計が足す予定だった銘柄種別（JPX data_j.xls 由来＝personal-only）・銘柄表記名・ISIN・ライセンス・writer は全て公開面の拡大になり、信用残のペイロードは5→17フィールドで約3.4倍になる。本計画の既定は「これらを公開オブジェクトに入れない（母集団事故の防止は PUT 前のコード側ガードで行う）」。入れたい場合は 007 を素通しから許可キーのホワイトリスト再構成に変える改修が先に必要で、CPU とコードが増える。どちらを採るか。
- 【Notion をサブとして書き続けるか】ジョブ所要時間の見積りが Notion 分を含むかどうかで 3〜5倍変わる（①月次 3分 vs 60〜73分、⑨月次 43分 vs 153〜178分、②日次 17〜21分 vs 77〜94分）。取り込み層の設計は Notion を落とした数字、Notion 層の設計は書き続ける前提で、両者が矛盾している。書き続けるなら全ジョブの timeout をこの2列表から引き直す必要があり、特に ⑨月次は 180分ではマージンが 2〜27分しかない。断面（①②⑧⑨）を毎日/毎月 Notion にも書くのか、鮮度カタログだけにするのかの方針決定が要る。
- 【みんかぶ優待の扱いを、公認照会の結果を待って決めるか】逐条確認の結論は「規約内に personal-only を緩める根拠は存在しない」で、本計画はタグ維持を前提に P3 の公開面是正（掲載文を公開画面・公開APIの6箇所から外す）を組んでいる。もし後から公認が得られる、あるいは法務確認が通ってタグが変わると、この是正が不要になり掲載文を公開面に戻す逆方向の作業が発生する。公認の申請窓口・条件は規約にも関連法規ページにも明記がなく、運営会社への個別照会が必要。P3 着手前に照会するか、タグ維持で先に進めるか。
---

# 残るリスクと未確認事項

## A. 公開層（API/MCP）に残るリスク

| # | リスク | 影響 | 緩和 |
|---|---|---|---|
| S-1 | **新しい公開面を立てる前に、既存の公開面が personal-only を無認証で配っている**（007 の日足・5分足・信用残、002 の優待スクリーニングと掲載文。全て実読で確定） | 同一アカウント・同一データで厳格な面と緩い面が並存し、規約上の説明がつかない | 公開層 S1 より**前**に是正を切替工程へ組み込む（承認 A24）。少なくとも読取キーを掛けるか内部ホストへ移す |
| S-2 | **公開できる①の列が薄い**。市場区分・33業種・17業種・銘柄種別は JPX data_j.xls 由来＝personal-only | 公開 API の実用性が大きく落ちる。「東証プライムの高ROE」のような最も自然なクエリが公開面で成立しない | (a) EDINET コードリストの提出者業種で代替（commercial-ok）、(b) JPX の市場区分に編集著作物性があるかは**要法務確認**、(c) 当面は EDINET 由来の業種のみで公開 |
| S-3 | 開示メタの感情スコア列の扱いが未決。kabulab-cf の自前計算だが入力が factual-cite なので継承則では factual-cite になる | 公開面の列選定が決まらない | 「原文の要約・感情スコアは原文の派生でメタデータの範囲を超えうる」として**既定では公開しない** |
| S-4 | APIキー表と使用量計上表は**確定した D1 配置表に無い新規テーブル** | 配置表からの逸脱 | 数十〜数千行で 10GB 上限への影響は無視できる。D1 配置の判定基準3条件は満たすので**追加提案**として扱う |
| S-5 | 公開 Worker のソースが PUBLIC リポジトリに載るため、述語・レート制限値が全部公開される | 攻撃者が最も高コストな述語を狙える | 述語を固定パラメータに限っているので「高コストな述語」自体が存在しない設計。走査行上限とレート制限が最後の防御 |
| S-6 | **内部面の APIキーを他者に配ると personal-only の「私的利用」の前提が崩れる**（yfinance / JPX / 日証金「第三者の利用に供することを固く禁じます」/ みんかぶ） | 規約違反 | 発行ルールを明文化し、personal-only スコープを含むキーは**自分の operator_id にのみ**発行する。外部読者2リポは自分の道具なので該当しない |
| S-7 | 公開層の追加により writer 自身とは別に R2 GET が増える（週次エクスポートと整合チェックで年 約46万回） | Free 枠内だが公開 Worker のトラフィックと合算される | 監視対象として鮮度テーブルに計上 |

## B. 移行に残るリスク（不可逆・高コスト）

| 操作 | フェーズ | 復旧コスト |
|---|---|---|
| 日足テーブル（336,185行）の DROP | P7 | Free の日次 rows written 100,000 で**再投入に4日**。Paid なら1回。**28日の観測窓を完走させてから DROP する** |
| swing 指標テーブルの12列削除 | P7 | SQLite の列削除は索引・VIEW・トリガ・生成列が参照していると失敗し、再追加すると**列順が変わる**。`SELECT *` 依存があれば壊れる（**未確認**） |
| finmath キャッシュ2表の DROP | P7 | Worker が書き直すので低い。**任意に格下げ済み** |
| 開示メタの重複行削除 | P8 | 退避スナップショットから復元可能 |

**D1 の Time Travel が使えるかは未確認**。使えてもテーブル単位ではなく DB 全体の巻き戻しになるはずで、他サービスの正常な書込も巻き戻る。→ **依存しない前提**でエクスポートを取る設計にしてある。

## C. 権限・セキュリティ

- **R2 トークンはバケット単位でしかスコープできない。** vwap-data への書込権限を渡すと、公開 Worker がバインドしているバケット全体（日足4,444件・5分足4,272件・信用残）への RW 権限を渡すことになる。5分足は kabulab-cf が writer のままなので、誤操作で触る経路が権限上は開く。緩和は「キー生成を1モジュールに閉じ、5分足のキーを生成する関数を置かない」「削除メソッドを実装しない」だが、**これはコード上の規律であって権限境界ではない**。
- **stockStock は PUBLIC・kabulab-cf は PRIVATE。** 7サービスの本番 D1 への書込トークンを公開リポジトリの Secrets に置くので爆発半径が非対称。緩和は対象1DBのみへのスコープ。かつ SQL 全文をログに出す設計なので、**値そのものがログに出ないようパラメータをマスクする実装**が必須。
- **kabulab-cf の `.env` が認証情報の共有点。** 外部の検証リポジトリが R2/D1/EDINET の資格情報をここから読む。vwap リポジトリをアーカイブしても消せず、トークンをローテーションすると外部リポジトリが静かに壊れる。

## D. 検証が原理的に通らない可能性

- **yfinance の値が両者で一致する保証がない。** stockStock と kabulab-cf は別時刻に Yahoo を叩くため、同一営業日分でも Yahoo 側の更新で値が変わる。G3（相対誤差 ≤ 1e-6）が通らないケースがありうる。緩和は「基準日が一致する行だけ比較し、不一致は比較不能として別カウント」。それでも通らない場合、**どこまで許容誤差を緩めるかは未定（要判断）**。
- **調整値は分割で遡及改訂される。** per-code 日足の read-merge-write は日付キーのマージなので過去の調整値が古いまま残る（現行の kabulab-cf 側の取込も同じ問題を抱えている）。G-daily-2 で「分割イベントに紐づかない不一致がゼロ」を確認する設計にしたが、**紐づく不一致を全期間再取得で直すか古いまま残すかは未決定**。
- **日足テーブルは D1 比較の対象にできない**（1回の全走査で Free の日次 rows read の 6.7%）。P7 は読み口の等価性だけで判定するため、**データ層の値一致を直接は確認しない穴が残る**。

## E. 未確認・未検証のまま残る事項

| 項目 | 影響 |
|---|---|
| Workers プランと D1 の現使用量 | 判断事項 #1。全体の前提 |
| **`workers.dev` でエッジキャッシュが効くか** | 効かないと公開 API の D1 走査行と R2 Class B が素通しになる。カスタムドメインが任意ではなく前提になる |
| **D1 の結果 meta に走査行数が入るか** | 入らなければ `meta.rows_read` を省略し、推定値で埋めない |
| **R2 binding に presigned URL 生成 API があるか** | 無ければ大容量バルクの代替導線が自前 SigV4 になる。v1 では使わないので影響なし |
| **Workers の Rate Limiting binding のプラン要件** | Free で使えないなら Cache API + D1 カウンタの自前実装に縮退（精度低下） |
| **MCP プロトコル版の仕様内容** | kabuMCP の実装からの引用。仕様と食い違っても kabuMCP と同じ挙動にはなる |
| **Worker 上で Parquet を解く CPU コスト** | ファクト抽出ツールを v1 に入れない判断の根拠。検証が済めば S3 以降で追加可能 |
| **バリュエーション取得の返り値に営業利益率・EPS・BPS・ROE・ROA が含まれるか** | P5 の最初のタスク。含まれなければ財務サマリからの join に倒れ、**既存値と定義が変わって 001/002 の画面数値が動く** |
| **バリュエーションの並列化が 429 を食うか** | ②日次の所要が 17〜21分 と 1h31m で4倍変わる。並列度1に戻せるオプションを必ず残す |
| **2026-09-28 の JPX 新様式の実フォーマットとファイル名** | 実物は 09-28 まで存在せず、それまでパーサを実データで検証できない（テストフィクスチャは実レスポンスのみという規約上、新様式のテストが書けない） |
| **9/27 のシステム移行が失敗した場合** | 公式に代替スケジュールが併記されている＝**現実的に起こりうる**。日付分岐を一切書かない設計にしてあるが、9/27 20時ごろの告知を人間が確認する運用は残る |
| **日証金 CSV の実ヘッダ文字列との1対1対応** | P1 のスキーマが実装時に変わる |
| **①以外の既存テーブルの NOT NULL 制約** | 開示メタのみ実読確認済み。列追加の前に全テーブルのスキーマを取って確認する |
| **swing 指標テーブルの12列削除後に `SELECT *` 依存が壊れないか** | P7 で画面が壊れうる |
| **EDINET 追加書類種別の年間件数** | すべて推定。1日だけ dry-run で実件数を測ってから本番投入する |
| **TDnet 一覧 JSON の同日別内容スナップショットが 23件 発生している経緯** | ローカルの保存実装は同一 (scope, date) の3つ目の別内容で例外を投げるはずなので、別経路の書込が存在する可能性がある |
| **vwap リポジトリの未コミット差分7ファイル（+132/−34）の内容** | 捨てると吸収されていない改修が消える可能性 |
| **R2 の IA 遷移が Class A に計上されるか / IA からの公開配信の取得課金** | コスト試算に未計上 |

## F. 「やらないと決めたが、やらないことのリスク」

1. **RSI パーセンタイルを kabulab-cf に残す** → P6 で入力条件は満たされるのに移植しないため、kabulab-cf の日次 cron を動かし続ける必要がある。結果として P5 のカットオーバーは「部分停止」にしかならず、**分割フラグの改修（承認 A11）が入らないと P5 が実行できない**。これを P5 の前提条件として明示的に管理すること。
2. **5分足を保留する** → 日足と同じ cron / 同じステップで回っているため、P6 で日足を止めるには**5分足の切り出し改修が先に要る**（承認 A14）。忘れると「日足を止めるつもりが5分足も止まる」。
3. **みんかぶを personal-only 維持のまま writer だけ移す** → 掲載文を公開面から外す是正（承認 A8、影響6箇所）を別チケットに後回しにすると、**規約違反をそのまま新基盤へ引き継ぐ**。同じ PR に入れることが必須条件。
4. **発行会社一次情報への切り替えを本計画に含めない** → 優待掲載文の実体は発行会社の IR 文の転記であり、TDnet 適時開示から自前で組み立てれば規約の適用外になる。①開示メタと⑤原本を既に持つので構造上つながっているが、**カバレッジがみんかぶ相当（1,670銘柄・8,314件）に達するかは未検証**。別チケット。
5. **kabuMCP の Static Assets 43MB を物理的に統合しない** → 正本の統一（生成元の差し替え）だけ行い、物理コピーは残る。D1/R2 読みに替えたときのコスト差が**未試算**なので、判断材料が揃うまで動かさない。

## G. 期限のあるリスク（2026-09-28）

- JPX の様式変更まで**残り17日**。kabulab-cf と stockStock の**どちらのパーサも旧様式前提**で、無改修で通る保証はない。
- **旧様式の週末残高は 2026-09-18申込分（9/25 16:00 の例外公表）が最後**。一覧には直近約5週しか残らないため、取り逃すと旧様式のデータは二度と取れない（2026-06-12〜07-31 が既に再取得不能なのと同じ状況が繰り返される）。
- P2 を 09-28 に間に合わせるには P0 と承認 A1〜A5 を2週間以内に通す必要がある。間に合わない場合の縮退は「kabulab-cf の cron を止めず、09-28 以降の失敗を許容して P2 を10月に後ろ倒しする」だが、その間の日次データは欠測になる。
- **緩和策**: P1（日証金）を先に完了させておくと、JPX が欠測しても貸借残で需給の連続性が保てる。これが P1 を P2 より前に置いた理由。
- 現行の信用残ジョブの cron（土曜 07:30 JST）は JPX の公表時刻（火曜 16:30）に対して**最大4日遅れ**で、9/25 の金曜16:00 例外公表を確実に拾える保証がない。**P2 に着手しない場合でも、少なくとも cron を火曜17:00 + 金曜17:00 に直して旧様式の残り2週を確保すべき。**

## H. 無音で壊れる実装上の罠（実装時のチェックリスト）

1. **取得期間の短縮で②断面の半分が全滅する** — テクニカル計算は SMA75=75本、SMA200=200本、52週高安=暦日364日を要求する。3ヶ月に縮めると全4,445銘柄でこれらが恒久 NULL になり、分割検出の窓も 260営業日→62本に縮む。**マージ後の長期系列で計算する処理順を仕様として固定すること**（migration P6）。
2. **JPX 新様式の数値ずれ** — 現行の変換は12フィールドへ順に割り当てる。新様式の15数値をそのまま流すと**例外も出さずに全ての値がずれる**。数値トークン数による分岐を入れるまで、09-28 以降に旧ジョブを回してはいけない。
3. **日証金のマスク値を 0 に潰す** — 品貸料率・品貸日数に `*****` が実在する。`int()` の失敗を 0 にフォールバックすると「品貸料率ゼロ」という嘘のデータが生まれる。`None` にする。制度信用の残高列が全行空である点も同様（「パース不能は0」の方針を日証金に流用しない）。
4. **コードの英数字混在** — 貸借銘柄区分に `130A` が実在する。`^\d{4}$` を仮定している箇所があると落ちるか黙って落とす。`^[0-9A-Za-z]{4}$` に統一（007 の API も同じ正規表現なので整合する）。
5. **per-code 日足の全置換で過去バーを失う** — RMW の GET が失敗したときに「空から始める」と10年分が1ヶ月分に上書きされる。**キーが存在しない場合以外は絶対に空から始めない**。この1点だけで 0.694GB と外部2リポジトリの分析が消える。
6. **週次インデックスの後退** — 信用残の週次インデックスは全置換 RMW が必要だが、007 はこれを唯一の入口にしており、空配列を書くと（オブジェクト自体は残っていても）画面上はデータが消えたのと同じになる。**配列の要素数後退を汎用ルールで拒否する**（M3）。
7. **④の二重挿入** — キーのバックフィル前に writer を移すと 37,338行が丸ごと二重になる。本計画では P8 まで書込をスキップするが、**スキップ時に成功扱いを返すと「書けていないのに成功」と誤認する**。スキップ時は鮮度テーブルに行を作らないことで検知可能にする。
8. **⑨の LLM 推定値の上書き** — 全列 SET のデフォルト UPSERT を使うと消える。保護列を除外しない経路を1つも作らないこと。テストで固定し、非 NULL 行数のガードを入れる。
9. **公開リポジトリの CI に CF Secret が混入する** — PUBLIC リポで `pull_request` トリガー。将来「テストで D1 を叩きたい」となったときに足してしまう事故が起こりうる。CI 定義の先頭に禁止コメントを置き、CODEOWNERS か lint で守る。