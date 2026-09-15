# stockStock → kabulab-cf 一本化 移行計画

作成: 2026-09-14 / 作成者: Muse Code / 依頼者: satoki252595 / 状態: 承認待ち

## Goal

stockStock の全資産（Python パイプライン、worker/jss-api、ワークフロー、文書）を
kabulab-cf に移設し、stockStock を archive する。データ・契約・文書の正を 1 リポに寄せ、
二重管理（契約ファイル複製、`cross-repo-contract`、P6 同時マージ手順）を解消する。

## Success Criteria

- Python パイプライン・jss-api・全ワークフローが kabulab-cf 上で稼働している
- 移設した cron が kabulab-cf から平日 1 サイクル以上すべて success している
- jss-api public/private が移設前と同一 URL で応答している
- 契約ファイルが単一配置になり、両リポの cross-repo 機構が削除されている
- stockStock が archived で、依存先（kabuMCP・端末B・projects README）が更新済み
- D1 / R2 / Notion のデータに差分が無い（動くのはコードの置き場所だけ）

## Context And Current Facts

規模（実測）: stockStock は Python 約 26,000 行（src 57 + tests 41 + scripts 4 本）
+ worker 639 行。kabulab-cf は TS 約 48,000 行 + SQL 19 本。どちらも pnpm workspace
なし、kabulab-cf はルート単一 package、stockStock worker は standalone（独自 lock）。

stockStock の外部インタフェース（移設対象の全表面）:

- worker/jss-api（public/private 2 デプロイ）: `/v1/meta/*`、`/v1/xbrl/elements`、
  `/v1/files/:sha256`、`/v1/supply/*`、`/v1/yutai/:code`、`/health`、`POST /mcp`
- MCP 5 ツール: `jp_supply_latest`、`jp_supply_series`、`jp_dataset_freshness`、
  `jp_xbrl_elements`、`jp_raw_file`（サーバ名 `jp-stock-pipeline`）
- R2: `jp-stock-raw`（⑤原本）、`jp-stock-supply`（⑧'需給 per-code）
- D1: `jss_*` 11 表の writer（`apply_schema` + 各ジョブ）。`ir_disclosures` は除く
  （writer は kabulab-cf）。D1 実体は両 worker とも同一 DB を bind 済み
- Notion ①③④⑤の writer（②⑥⑦は廃止済み）
- kabuMCP 連携: `edinet_daily --kabumcp-cache-dir` が type5 ZIP を `{docID}.zip`
  で追記（SHA-256 照合・上書き禁止）
- local_store: 端末B PostgreSQL への dual-write + FastAPI 配信アプリ

依存先（`~/projects` 横断検索で確定）:

- kabuMCP: 上記キャッシュの消費者 + README/HANDOVER.md が stockStock を参照
- 端末B: local_store の実行環境。コード同期方式（git pull か手動か）は文書に無し
- `~/projects/README.md`: カタログ 1 行
- jss-api 消費元（外部含め不明）: Worker 名・URL 不変のため影響なし
- Notion / R2 / D1: サービス側は置き場所に無関心のため影響なし
- stockStock の open Issue #66（SLO 違反）。`ops_check` は `gh issue` を
  リポ指定なしで呼ぶため、移設後は自動で kabulab-cf に立つ

コード上の結合点:

- kabulab-cf 側の `jss_*` 参照は 0 件（D1 リソース共有のみでコード結合なし）
- stockStock 側のハードコード cross-repo 参照は `ci.yml` の peer checkout 1 件のみ
- 契約ファイルは両リポ同一バイト列（blob sha `e175d94…`、P6 で検証済み）
- ワークフロー名の衝突は `ci.yml` のみ。他は stockStock 7 本
  （ci / cloud_check / edinet_daily / master_sync / ops_check / supply_daily /
  tdnet_hourly）vs kabulab-cf 4 本で重なりなし
- cron 付きは stockStock 5 本のうち平日のみ 3 本（edinet/supply/tdnet）、
  `ops_check` は毎日、`master_sync` は月次（毎月 1 日）。kabulab-cf 側も平日中心
- Secrets は stockStock 15 本・kabulab-cf 12 本。名前衝突なし（`CF_*` と
  `CLOUDFLARE_*` は別名）。値は API で読めないため再入力が必要
- flake は両方 nixpkgs 系（25.05 と 25.11）。stockStock が python312 + uv +
  wheelLibs を追加で持つ。jss-api の依存は旧ピン止め（wrangler 3.99 /
  vitest 2 / hono 4.6。kabulab-cf は 4.20 / 4 / 4.12）

## Constraints And Non-goals

制約: D1 / R2 / Notion のデータ層は無変更。jss-api の Worker 名・URL は不変。
Secrets の値は画面に表示しない（名前のみ扱う）。

非目標: jss-api の依存更新（wrangler 3→4 等は別件）、L-23 シャード化、
jss-api の kabulab-cf 本体 Worker への統合、リポ名変更、SLO ルール変更、
`pending-peer` 跡地のような既に終わった整理の蒸し返し。

## Key Decisions

1. 方向は kabulab-cf 寄せ（承認済みの推奨）。稼働系の配線（Worker 本番・D1
   bind・cron・Secrets）が kabulab-cf にあり、D-14-1 で writer が TS 側に
   固定済み。逆向きは公開面 7 サービス全部の移設になるため棄却。
2. 配置: Python 一式は `pipeline/`（uv プロジェクト化し `import
   jp_stock_pipeline` を不変に保つ）。jss-api は `services/jss-api/` に
   standalone のまま置く（独自 lock・旧ピン維持）。`worker/` 直下は本番
   Worker のエントリと紛らわしいため避ける。
3. 契約ファイルは kabulab-cf 現行パス `tests/fixtures/contracts/` に一本化し、
   pipeline 側は相対参照に直す。別案の再複製・生成物二重化はしない。
4. cron 切替は週末ウィンドウで「stockStock 無効化 + kabulab-cf 有効化」を
   同時実行。平日のみ 3 本 + 日次 `ops_check` + 月次 `master_sync` の構成上、
   毎月 1 日を外した週末なら重複は `ops_check` だけになり、かつ同ジョブは
   重複実行が無害（冪等・Issue 追記型）。
5. Secrets は値の再入力で移管する（GitHub API で値は読めない）。正は手元の
   `.env` と `db_ids.json`。廃止予定の 3 本（`NOTION_DB_PRICES/EXPORTS/JOB_LOG`、
   P3 対象）は移管しない。
6. stockStock は archive する（削除しない。履歴・Issue・PR を保全）。
   削除案は P3 revert 参照と来歴を失うため棄却。archive は解除可（Sources 参照）。
7. 依存先は影響 OK の前提で追随更新する。kabuMCP は README のコマンドパス更新
   （ファイル形式は不変）。端末B は手動切替 + 検証コマンド実行。

## Recommended Approach

データ層に触れず、コードの置き場所だけを移す。順序は「移設 → CI 統合 →
無効スケジュールで試運転 → 週末カットオーバー → 文書統合 → archive」。
各ウェーブは独立に revert 可能で、カットオーバー後のロールバックも
スケジュールの戻しだけで完結する（データ層が同一のため）。

## Work Plan

PR 分割（kabulab-cf 4 本 + stockStock 2 本 + 手動手順）。承認後の実行・公開時は
この分割を維持すること。

- Wave 0 準備（PR なし・手動）: Open Questions 1〜4 を確定する。Secrets 値
  の用意（`.env`・`db_ids.json` から転記、画面表示なし）。端末B の現行
  同期方式を確認し Wave 7 の手順を fix する。
- PR-1 コード移設（kabulab-cf）: `src/jp_stock_pipeline/` →
  `pipeline/src/jp_stock_pipeline/`、`tests/` → `pipeline/tests/`（fixtures
  を除く）、`scripts/` → `pipeline/scripts/`、`pyproject.toml`・`uv.lock` →
  `pipeline/`、`worker/` → `services/jss-api/`、`db_ids.json` → ルート、
  `.env.example` をマージ、flake に python312 + uv + wheelLibs を追加し
  `flake.lock` 再生成。契約ファイルは kabulab-cf 側の現物を正とし、
  pipeline 側の参照（`test_governance.py`、`export_contracts.py`、
  jss-api の契約テスト）を相対参照に直す。CI 配線は PR-2 で行い、本 PR は
  既存 check が緑のまま通ること。
- PR-2 CI 統合（kabulab-cf）: `ci.yml` に `python-pipeline` ジョブを追加
  （`uv sync` → `ruff check` → `pytest` → jss-api の install/typecheck/test）。
  kabulab-cf 側の `cross-repo-contract` ジョブと peer checkout を削除。
  失敗通知は composite action に寄せる。stockStock 側の同ジョブは Wave 5 で消す。
- PR-3 ワークフロー移設（kabulab-cf）: 5 cron + `cloud_check` を移設するが
  cron トリガは付けず `workflow_dispatch` のみで追加する。Secrets を
  kabulab-cf に設定する（手動・Wave 0 の転記値で）。
- Wave 4 試運転（PR なし）: 移設した 5 本を dispatch で流し、D1・Notion・R2
  の出力を検証する。jss-api を新パスから public → private の順でデプロイし
  （Worker 名は同一）、`/health` と代表 API で smoke する。
- Wave 5 カットオーバー（週末・2 PR）: stockStock 側で 5 本の cron を外す
  PR と、kabulab-cf 側で cron を付ける PR を同ウィンドウでマージする。
  stockStock 側の `cross-repo-contract` もここで削除する。翌平日に全 cron
  の success と鮮度を確認する（1 サイクル検証）。
- PR-6 文書統合（kabulab-cf）: `docs/` 6 本を移設し HANDOFF を一本化する
  （kabulab-cf 側の抜粋版は全文に置換、P6 節は history 扱いに）。
  README・AGENTS.md・CLAUDE.md をマージする。`satoki252595/stockStock`
  参照を archive 後の URL・新パスに更新する。
- Wave 7 依存先 + archive: kabuMCP README のコマンドパス更新 PR、
  `~/projects/README.md` のカタログ更新、端末B の切替（手動・Wave 0 fix の
  手順で）、#66 の close 判断、stockStock README に移転告知を追記して
  archive を実行する。ローカル `~/projects/stockStock` は最終 pull して残す。

## Validation Plan

- PR-1: `nix develop -c` で `uv sync` が通る（`pipeline/`起点）。
  `pytest`・`ruff`・jss-api の `typecheck`/`test` が新パスで緑。
  移設前後のファイル数が一致（fixtures 除く）。契約ファイルが移設前と
  バイト一致（`cmp`）。
- PR-2: `gh pr checks` が全緑。わざと落とす確認はしない（既存テストが gating）。
- PR-3: 追加直後に cron が無いこと（`grep cron` で 0 件）。
  Secrets 名の一覧突合（15 本中、廃止予定 3 本を除く 12 本 + 既存分）。
- Wave 4: dispatch 5 本が success。`jss_dataset_freshness` の
  `latest_data_date` が前進、`jss_job_runs` に二重記録なし。
  `curl /health` が public/private とも 200、`SURFACE` が一致。
- Wave 5: 平日 1 サイクルで 8 本（移設 5 + 既存 3）すべて success。
  §8.3 方式の鮮度確認（最新日付・行数）。stockStock 側で cron 起動が
  0 件（`gh run list` の event/schedule 確認）。
- PR-6: `grep -rn "stockStock" --include="*.md"` の残存が意図的なもの
  （archive 告知・来歴）のみ。リンク切れの目視。
- Wave 7: kabuMCP の `edinet_daily --dry-run --kabumcp-cache-dir` が新パスで
  動く。`gh api repos/satoki252595/stockStock --jq .archived` が `true`。
  端末B FastAPI の `/health` が 200。

最高リスクの検証は Wave 5 の「平日 1 サイクル」である。これが通るまで
Wave 7（archive）に入らないこと。

## Risks / Rollback

- cron 二重実行: 週末ウィンドウ + `ops_check` のみ重複許容で回避する。
  万が一重なっても同ジョブは冪等・Issue 追記型のため実害なし。
- Secrets 値の不一致: Wave 0 の転記 + Wave 4 の試運転で検出する。
  検出時は値を入れ直して dispatch 再実行。
- 端末B の取り残し: Wave 0 で同期方式を確定させ、Wave 7 を手動ゲートにする。
- CI 肥大化: nix キャッシュ（`magic_nix_cache`）と既存の 5 分枠運用で吸収する。
  `python-pipeline` は `check` と並列のため wall-time 増は小さい。
- archive 後の参照切れ: PR-6 と Wave 7 で README・バッジ・リンクを更新する。
- Rollback: Wave 5 前は各 PR の revert。切替後は stockStock の cron 復活
  revert + kabulab-cf の cron 無効化で戻る（データ層同一のため安全）。
  archive 後も unarchive で復帰できる（GitHub Docs の archive 手順）。

## Open Questions

1. 端末B はコードをどう同期しているか（stockStock の git pull か、手動コピーか）。
   Wave 0 で確認し、Wave 7 の手順を fix する。
2. `NOTION_TOKEN` と R2 キーは両リポに同名である。値が一致しているか、
   どちらを正にするか。画面に値は出さず、ユーザー判断で決める。
3. #66（SLO 違反 Issue）はカットオーバー時に close するか。推奨は close
   （新 Issue は kabulab-cf に立つため）。
4. ローカルの `~/projects/stockStock` フォルダは残すか消すか。推奨は残す
   （最終 pull 済みの参照用。P3 完了までは `db_ids.json` の予備にもなる）。

## Sources

- https://docs.github.com/en/repositories/archiving-a-github-repository/archiving-repositories
