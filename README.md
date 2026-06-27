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

Notion への格納と同時に、LAN 内の別端末（端末B）の PostgreSQL へ dual-write し、
FastAPI(REST + APIキー)で逐次アクセスできる。`LOCAL_DB_HOST` を設定すると有効化
（未設定なら Notion のみ＝従来動作）。接続情報は全て `.env`（[.env.example](.env.example) 参照）。

- **正本は Notion**。ローカルミラー失敗はジョブを止めず degrade（warning 記録）。
- `②株価` はローカルでは `(code, data_date)` を主キーに**時系列を蓄積**（Notion は最新スナップショット）。
- `②` は personal-only（yfinance/stooq）。**ローカル自己利用に限り、公開しないこと**。

```bash
# 端末B: PostgreSQL に DB/ユーザーを用意（テーブルは初回ジョブ実行時に自動作成）
createuser jp_stock --pwprompt && createdb -O jp_stock jp_stock

# 端末A: .env に LOCAL_DB_* を設定して通常どおりジョブを実行（Notion と同時にミラー）
nix develop -c uv run python -m jp_stock_pipeline.jobs.prices_daily

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
