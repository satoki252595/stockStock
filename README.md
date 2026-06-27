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
| `GET /health` | 死活確認（認証不要） / `GET /docs` Swagger UI |

```bash
# 利用例（端末B の IP が 192.168.1.50 の場合）
curl -H "X-API-Key: $LOCAL_API_KEY" "http://192.168.1.50:8000/prices/7203?from=2026-01-01"
```

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
