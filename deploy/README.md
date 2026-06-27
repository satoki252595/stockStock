# 本番デプロイ Runbook（トポロジ B: 収集=GitHub Actions / サーバ=DB+API）

このディレクトリは **サーバ上で PostgreSQL + FastAPI を常駐運用**するための成果物。
収集ジョブは引き続き **GitHub Actions の cron**（cloud 経路）で回し、Notion を正本に
しつつ、サーバの PostgreSQL へ **Tailscale 経由で dual-write** する構成（DESIGN §7.1）。

```
┌─ GitHub Actions (cron) ──────────┐         ┌─ 本番サーバ ───────────────┐
│ 6 collection workflows           │         │ PostgreSQL (localhost:5432)│
│   --db-target cloud (既定)        │         │   ↑ localhost              │
│   ├─ Notion へ upsert（正本）      │         │ FastAPI (uvicorn)          │
│   └─ dual-write ──────────────────┼─ tailnet ┼→ tailscale0:5432 へ書込    │
│      LOCAL_DB_HOST=<tailscale IP> │  (暗号+認証) │   X-API-Key で読取公開    │
└───────────────────────────────────┘         └────────────────────────────┘
        ↑ tailscale/github-action で tailnet に参加        ↑ ユーザーは tailnet から API 照会
```

**なぜ Tailscale 必須か**: PostgreSQL を直接インターネット公開すると `sslmode=require` は
経路を暗号化するだけでサーバ認証をせず能動的 MITM に脆弱（既知リスク）。Tailscale（WireGuard）は
経路自体を暗号化＋相互認証するため、PG を tailnet 内だけに晒せば安全。tailnet 上では
`LOCAL_DB_SSLMODE=disable` で構わない（二重暗号は不要）。

---

## 前提

- サーバ: Linux（systemd 利用）。本 Runbook は Debian/Ubuntu 系の例。
- このリポジトリの開発・実行は **nix** に統一（CLAUDE.md 方針）。サーバにも Nix を入れ、
  CI と同じ `nix develop -c ...` で API を起動する。
- Tailscale アカウント（サーバ・GitHub Actions・閲覧端末を同一 tailnet に参加させる）。

---

## 1. サーバ初期セットアップ

### 1-1. Nix とリポジトリ
```bash
# Determinate Systems Nix installer（CI と同じ）
curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
sudo mkdir -p /opt && sudo chown "$USER" /opt
git clone https://github.com/satoki252595/stockStock.git /opt/jp-stock-pipeline
cd /opt/jp-stock-pipeline && nix develop -c uv sync   # 依存解決の動作確認
```

### 1-2. PostgreSQL
```bash
sudo apt-get update && sudo apt-get install -y postgresql
# DB / ユーザー作成（パスワードは安全な値に）
sudo -u postgres psql -v pw="'$(openssl rand -base64 24)'" -f /opt/jp-stock-pipeline/deploy/postgres/init.sql
# ↑ 出力された CREATE ROLE のパスワードを控える（後で .env と GitHub Secrets に使う）
```
テーブルは初回ジョブ実行時に `init_schema()` が冪等 DDL（`CREATE TABLE IF NOT EXISTS`）で
自動作成するので手動 DDL は不要。

### 1-3. Tailscale（サーバを tailnet へ）
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname jp-stock-server     # 表示される URL で認証
tailscale ip -4                                  # → 100.x.y.z（これがサーバの tailnet アドレス）
```

### 1-4. PostgreSQL を tailnet からのみ受け付ける
GitHub Actions（cloud 経路）がこのアドレスへ書き込めるよう、tailnet インターフェースで
listen し、tailnet CIDR だけ許可する。**`0.0.0.0` で全公開しないこと。**

`/etc/postgresql/*/main/postgresql.conf`:
```
listen_addresses = 'localhost,100.x.y.z'     # localhost(API用) + 自分の tailscale IP のみ
```
`/etc/postgresql/*/main/pg_hba.conf`（末尾に追記。100.64.0.0/10 は Tailscale の CGNAT 帯）:
```
host  jp_stock  jp_stock  100.64.0.0/10  scram-sha-256
```
```bash
sudo systemctl restart postgresql
```
> ufw 等を使うなら 5432 は tailscale0 インターフェース限定にする（公開 NIC では閉じる）。

---

## 2. FastAPI を常駐させる（systemd + nix）

```bash
# サーバ用 .env を配置（API は localhost の PG に lan プロファイルで接続する）
cp deploy/env.server.example /opt/jp-stock-pipeline/.env
$EDITOR /opt/jp-stock-pipeline/.env      # LOCAL_DB_PASSWORD / LOCAL_API_KEY / LOCAL_API_HOST を設定

sudo cp deploy/systemd/jp-stock-api.service /etc/systemd/system/
# サービスの実行ユーザーを自分に合わせる（User=/WorkingDirectory= を編集）
sudo systemctl daemon-reload
sudo systemctl enable --now jp-stock-api
systemctl status jp-stock-api
curl -s http://100.x.y.z:8000/health      # {"status":"ok"} を確認（tailnet 上の端末から）
```
API は `LOCAL_API_HOST=100.x.y.z`（自分の tailscale IP）で listen させ、閲覧は tailnet 内から
`X-API-Key` 付きで行う。これなら公開 TLS 不要。**公開インターネットに出したい場合のみ**
Caddy 等で TLS 終端する（`deploy/Caddyfile.example` 参照）。

---

## 3. GitHub Actions 側（収集→サーバ書き込みを有効化）

### 3-1. Secrets（リポジトリ Settings → Secrets and variables → Actions）
| Secret | 値 |
|---|---|
| `NOTION_TOKEN` / `EDINET_API_KEY` | 既存（収集本体） |
| `NOTION_DB_*`（7つ） | `db_ids.json` の値 |
| `LOCAL_DB_HOST` | **サーバの tailscale IP（100.x.y.z）** |
| `LOCAL_DB_PORT` | `5432` |
| `LOCAL_DB_NAME` | `jp_stock` |
| `LOCAL_DB_USER` | `jp_stock` |
| `LOCAL_DB_PASSWORD` | 1-2 で設定した値 |
| `LOCAL_DB_SSLMODE` | `disable`（tailnet 上なので経路は既に暗号+認証） |
| `TS_OAUTH_CLIENT_ID` / `TS_OAUTH_SECRET` | Tailscale OAuth クライアント（下記） |

### 3-2. Actions を tailnet に参加させる
Tailscale 管理画面 → Settings → OAuth clients で **ephemeral** な OAuth クライアントを発行し、
`tag:ci` を付与（ACL に `tag:ci` を定義しておく）。6 本の収集 workflow には
`tailscale/github-action` の参加ステップを追加済み（収集ステップの直前）。OAuth Secrets を
設定すれば cron 実行時に runner が tailnet に入り、`LOCAL_DB_HOST` のサーバへ到達できる。
未設定なら参加に失敗するだけで、収集自体は Notion へは書ける（dual-write が degrade するのみ）。

> `LOCAL_DB_*` を未設定にすれば dual-write は無効化され、従来どおり Notion のみで動く。
> 段階導入したい場合はまず Secrets 無しで Notion 運用を確認し、後から有効化してよい。

---

## 4. バックアップ（②時系列はローカルが唯一の保持先）

Notion は ②株価の **最新スナップショットのみ**保持する（時系列はサーバ DB が唯一）。
`pg_dump` を毎日取得する systemd timer を同梱:
```bash
sudo cp deploy/systemd/jp-stock-backup.{service,timer} /etc/systemd/system/
sudo install -m 0755 deploy/backup.sh /opt/jp-stock-pipeline/deploy/backup.sh
sudo systemctl enable --now jp-stock-backup.timer
systemctl list-timers jp-stock-backup.timer
```
出力先は `/var/backups/jp-stock/`（14 世代ローテーション）。`backup.sh` 冒頭で調整可。

---

## 5. 運用確認・トラブルシュート

- 収集成否: ⑦ 収集ジョブログ（Notion と サーバ DB の `job_log` 両方に記録）。
  `GET /jobs` か `GET /jobs?job_name=prices_daily` で参照。
- dual-write の degrade: Actions ログに `dual-write degrade: Notion書き込み失敗 N 件 /
  ローカルミラー失敗 M 件` が出る。**両系統失敗分のみ** ⑦ の failed に計上される。
- サーバへ書けているか: Actions 実行後に `GET /prices/7203?from=YYYY-MM-DD` で時系列が伸びるか確認。
- API が 503: PostgreSQL 未起動 or 接続不可。`systemctl status postgresql` と `.env` の DB 値を確認。
- Actions が DB へ到達しない: tailnet 参加（OAuth Secrets）と pg_hba/listen_addresses を確認。

詳細な設計根拠は [docs/DESIGN.md §7.1](../docs/DESIGN.md) を参照。
