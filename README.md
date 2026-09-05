# jp-stock-data-pipeline

日本株のファンダメンタルズ・テクニカル情報を無料ソースから継続収集し、Notion「株式情報」配下に銘柄コード単位で構造化格納するパイプライン。設計の正本は [docs/DESIGN.md](docs/DESIGN.md)。

## 開発環境 (nix 必須)

```bash
nix develop          # Python 3.12 + uv
uv sync              # 依存解決
uv run pytest        # テスト
```

## セットアップ

1. Notion インテグレーションを作成し、親ページ「株式情報」に接続（コネクト追加）
2. P0: スキーマ作成（冪等。DB ID を `db_ids.json` に出力）

   ```bash
   NOTION_TOKEN=secret_xxx uv run python -m jp_stock_pipeline.notion.schema
   ```

3. GitHub Secrets を設定: `NOTION_TOKEN` / `EDINET_API_KEY` / 各DB ID（`NOTION_DB_*`、`db_ids.json` の出力値）

## ジョブ（GitHub Actions cron / 手動実行可）

| ジョブ | 実行 |
|---|---|
| 銘柄マスタ同期 | `uv run python -m jp_stock_pipeline.jobs.master_sync` |
| 株価+テクニカル | `uv run python -m jp_stock_pipeline.jobs.prices_daily` |
| TDnet開示 | `uv run python -m jp_stock_pipeline.jobs.tdnet_hourly` |
| EDINET書類 | `uv run python -m jp_stock_pipeline.jobs.edinet_daily` |
| 株価突合(stooq) | `uv run python -m jp_stock_pipeline.jobs.reconcile_weekly` |
| エクスポート | `uv run python -m jp_stock_pipeline.jobs.export_weekly` |

全ジョブ `--dry-run` 対応（Notion に書き込まない）。

## ローカル API（端末B・任意）

Notion への格納と同時に、別端末（端末B）の PostgreSQL へ dual-write し、
FastAPI(REST + APIキー)で逐次アクセスできる。`LOCAL_DB_HOST` を設定すると有効化
（未設定なら Notion のみ＝従来動作）。接続情報は全て `.env`（[.env.example](.env.example) 参照）。

接続プロファイルは 2 系統で、収集ジョブの `--db-target` で選ぶ（**既定 cloud**）:
- **cloud（既定）**: `LOCAL_DB_HOST` + `sslmode=require`。**クラウド(GitHub Actions 等)から端末B へ格納**。`require` は経路を暗号化するが**サーバ認証はしない**ため直公開は能動的 MITM に弱い → **VPN(Tailscale 等)経由を強く推奨**（VPN を使わないなら `sslmode=verify-full` + ルートCA を設定）。GitHub Actions は同名 Secrets を設定すれば cron 実行で自動 dual-write。
- **lan**: `LOCAL_DB_LAN_HOST`(既定 localhost) + `sslmode=prefer`。同一 LAN で手動実行するとき `--db-target lan`。

- **双方向フェールセーフ**。Notion とローカルへ独立に書き、**片方の保存が失敗してももう片方は必ず試み、どちらか一方にでも残ればその取得単位は成功扱い**（可用性最大化）。設計上の正本は Notion だが、ローカル API の可用性のため対称化。失敗は隠さず `notion_failed`/`mirror_failed` に計上し warning 記録、**両系統とも失敗した分だけ** ⑦ の failed に数える（§3-2）。原本 ⑤ のみ両系統失敗でその取得単位を中止（原本ゼロ＝トレーサビリティ喪失 §3-3）。
- `②株価` はローカルでは `(code, data_date)` を主キーに**時系列を蓄積**（Notion は最新スナップショット）。
- `②` は personal-only（yfinance/stooq）。**ローカル自己利用に限り、公開しないこと**。

```bash
# 端末B: PostgreSQL に DB/ユーザーを用意（テーブルは初回ジョブ実行時に自動作成）
createuser jp_stock --pwprompt && createdb -O jp_stock jp_stock

# 端末A: クラウド(GitHub Secrets)or .env に LOCAL_DB_* を設定してジョブ実行（Notion と同時ミラー）
nix develop -c uv run python -m jp_stock_pipeline.jobs.prices_daily                  # cloud（既定）
nix develop -c uv run python -m jp_stock_pipeline.jobs.prices_daily --db-target lan  # 同一LAN

# 端末B: API を起動（X-API-Key 認証は LOCAL_API_KEY）
nix develop -c uv run uvicorn jp_stock_pipeline.local_store.api:app --host 0.0.0.0 --port 8000
```

| エンドポイント | 内容 |
|---|---|
| `GET /stocks` `GET /stocks/{code}` | ① 銘柄マスタ |
| `GET /prices/{code}?from=&to=` | ② 株価テクニカル（時系列・data_date 降順） |
| `GET /financials/{code}` | ③ 財務サマリ |
| `GET /disclosures?code=&doc_type=&from=&to=` | ④ 開示書類 |
| `GET /raw` `GET /jobs` | ⑤原本メタ / ⑦ジョブログ |
| `GET /facts?doc_id=&code=&element=&text_only=` | ⑧ XBRL 全ファクト（数値＋定性 textBlock・ローカル専用） |
| `GET /facts/search?q=&code=` | ⑧ 定性 textBlock の日本語全文検索（PGroonga、無ければ ILIKE） |
| `GET /health` | 死活確認（認証不要） / `GET /docs` Swagger UI |

- **⑧ XBRL 全ファクト**は Notion に無いローカル専用の派生ストア。有報/短信 XBRL の全ファクト（事業等のリスク等の定性 textBlock 含む）を保持し横断クエリ＋全文検索できる。`factual-cite`（短信原文）も含むため公開用途では `license_tag` で要フィルタ。
- 全文検索を使うなら端末B で `CREATE EXTENSION pgroonga;`（未導入なら `/facts/search` は自動で `ILIKE` にフォールバック）。

```bash
# 利用例（端末B の IP が 192.168.1.50 の場合）
curl -H "X-API-Key: $LOCAL_API_KEY" "http://192.168.1.50:8000/prices/7203?from=2026-01-01"
# 定性情報の全文検索（例: 為替変動リスクに言及する開示を横断検索）
curl -H "X-API-Key: $LOCAL_API_KEY" "http://192.168.1.50:8000/facts/search?q=為替変動リスク"
```

### 本番サーバ運用（収集=GitHub Actions / サーバ=DB+API）

サーバ上で PostgreSQL + FastAPI を常駐させ、収集は GitHub Actions（cloud 経路）から
**Tailscale 経由**で dual-write する構成。systemd ユニット・PostgreSQL 初期化・日次
バックアップ・Tailscale 接続・GitHub Secrets 配線の手順は
**[deploy/README.md](deploy/README.md)** にまとめてある。

## kabuMCPへの原本引渡し（2026-09-05追加・任意）

既存収集後のEDINET type5 CSV ZIPを、kabuMCPの既存パーサが読める `{docID}.zip` でローカルキャッシュへ追加する。Notionの財務要約をもう一度取り出したり、新しいAI・DB・R2・常駐サーバを作る必要はない。

```bash
# 既存全社分を保持した累積キャッシュを指定する。通常の収集ジョブのオプション。
nix develop -c uv run python -m jp_stock_pipeline.jobs.edinet_daily \
  --kabumcp-cache-dir /絶対パス/累積EDINETキャッシュ
```

- 指定なしは従来動作。`--dry-run` は引渡し先にも書き込まない。
- 原本が既存Notion／ローカルの少なくとも一方に保存された後だけ引き渡す。
- EDINET・type5 CSV・`commercial-ok`、docID／原本URL／SHA-256／ZIPを検査。同一原本は変更せず、異内容の既存ファイル・symlink・不正入力は拒否する。
- 非上書きのatomic追加で途中のファイルを見せない。出力はローカルの通常ファイルシステム（hard-link対応）を使う。
- type1 XBRL fallbackはこの連携では未対応。連携失敗・type1スキップはジョブログの部分失敗に計上し、保存済みNotion／ローカルデータは維持する。既存runnerは部分失敗でも終了コード0なので、ジョブログと `kabumcp:` の失敗項目も確認する。
- このオプションによる追加API呼出しはない。ただしコマンド本体の通常収集は実行される。検証目的で過去の日付を何度も再収集しない。
- **日次の差分だけでkabuMCP全社データを再生成しない。** 既存全社分を含む累積キャッシュへ追加し、kabuMCPで索引→既存パーサ→golden／原典照合→既存Cloudflareデプロイの順に進める。
- GitHub ActionsからCloudflareへの常設転送・自動デプロイは今回未配線。既存workflowの頻度・費用・Secrets、Notionの内容は変更していない。
- TDnet・株価はこの経路に流さない。内部タグは商用許諾の証明ではなく、既存trackAにもTDnet財務数値が含まれるため丸ごと公開禁止。TDnetの商用配信条件と株価ベンダー契約を先に確認する。[JPX利用条件](https://www.jpx.co.jp/term-of-use/)

### 更新記録

| 日付・時間帯 | 更新者／依頼者 | 目的・変更内容 |
|---|---|---|
| 2026-09-05 JST | Codex（OpenAI）／satoki252595の依頼 | kabuMCPへの低コスト原本再利用。edinet_dailyに任意のキャッシュ引渡しと安全性テストを追加。通常収集・Notion・workflow・公開範囲は維持。 |
| 2026-09-05 12:15 JST | Codex（OpenAI）／satoki252595の依頼 | EDINETの期中報告を利用するため、一覧収集のみだった訂正四半期（150）・訂正半期（170）をCSV/XBRL財務抽出・任意kabuMCPキャッシュ引渡しにも追加。有報・四半期・半期と各訂正の計6種を既存経路で処理。Notion分類・TDnet・workflowは変更せず、実取得／Notion書込みは未実施。 |

正確な更新時刻・更新者・差分は `git log --format=fuller --stat` を参照。コミット本文にも目的と依頼者を残す。

検証記録: 新規キャッシュ連携テスト11件合格。INPEXの実EDINET原本 `S100XU9L` をkabuMCPへ渡して再解析し、現行JSONの19指標と完全一致。テストと実原本のコピーだけを行い、収集API再実行・Notion更新・本番配信は行っていない。

期中訂正対応の検証: EDINET・ジョブ・キャッシュ関連59件合格、既存の実APIフィクスチャ未取得2件skip、変更ファイルのruff合格。6書類種別の永続化前引渡し禁止と、訂正四半期／訂正半期のtype5優先・type1 fallback・財務保存経路を確認。type1のkabuMCPキャッシュ未対応は引き続き部分失敗として記録する。

## 不変条件

- 取得単位ごとに原本を ⑤原本ファイルDB へ必ず保存（失敗時は構造化データを書かない）
- ダミー・推定・補間データの生成禁止。欠損は欠損のまま
- 全行にソース・ライセンスタグ（`commercial-ok`/`factual-cite`/`personal-only`）・来歴を付与
- yfinance・stooq・JPX統計は **personal-only**（公開・商用利用禁止）

## テストフィクスチャ

実APIレスポンスの保存物のみ使用（捏造禁止）。未取得分は skip される。`scripts/capture_*.py` で取得。

## 免責

本基盤が提供するのは情報のみであり、投資助言ではない。
# stockStock
