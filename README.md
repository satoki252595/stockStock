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

3. GitHub Secrets を設定: `NOTION_TOKEN` / `EDINET_API_KEY` / `JQUANTS_MAIL_ADDRESS` / `JQUANTS_PASSWORD` / 各DB ID（`NOTION_DB_*`、`db_ids.json` の出力値）

## ジョブ（GitHub Actions cron / 手動実行可）

| ジョブ | 実行 |
|---|---|
| 銘柄マスタ同期 | `uv run python -m jp_stock_pipeline.jobs.master_sync` |
| 株価+テクニカル | `uv run python -m jp_stock_pipeline.jobs.prices_daily` |
| TDnet開示 | `uv run python -m jp_stock_pipeline.jobs.tdnet_hourly` |
| EDINET書類 | `uv run python -m jp_stock_pipeline.jobs.edinet_daily` |
| J-Quants突合 | `uv run python -m jp_stock_pipeline.jobs.jquants_weekly` |
| エクスポート | `uv run python -m jp_stock_pipeline.jobs.export_weekly` |

全ジョブ `--dry-run` 対応（Notion に書き込まない）。

## 不変条件

- 取得単位ごとに原本を ⑤原本ファイルDB へ必ず保存（失敗時は構造化データを書かない）
- ダミー・推定・補間データの生成禁止。欠損は欠損のまま
- 全行にソース・ライセンスタグ（`commercial-ok`/`factual-cite`/`personal-only`）・来歴を付与
- J-Quants 無料版・yfinance・stooq・JPX統計は **personal-only**（公開・商用利用禁止）

## テストフィクスチャ

実APIレスポンスの保存物のみ使用（捏造禁止）。未取得分は skip される。`scripts/capture_*.py` で取得。

## 免責

本基盤が提供するのは情報のみであり、投資助言ではない。
# stockStock
