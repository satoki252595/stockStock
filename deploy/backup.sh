#!/usr/bin/env bash
# jp_stock DB の日次 pg_dump バックアップ（14 世代ローテーション）。
# ②株価の時系列は Notion に無くサーバ DB が唯一の保持先なので必須。
# systemd timer (jp-stock-backup.timer) から呼ばれる想定。手動実行も可。
set -euo pipefail

DB_NAME="${LOCAL_DB_NAME:-jp_stock}"
DB_USER="${LOCAL_DB_USER:-jp_stock}"
DEST="${JP_STOCK_BACKUP_DIR:-/var/backups/jp-stock}"
KEEP="${JP_STOCK_BACKUP_KEEP:-14}"

mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/jp_stock-$STAMP.sql.gz"

# localhost の peer/scram 認証を利用（PGPASSWORD を使うなら環境で渡す）
pg_dump -U "$DB_USER" -h localhost "$DB_NAME" | gzip > "$OUT"
echo "backup: $OUT ($(du -h "$OUT" | cut -f1))"

# 古い世代を削除（KEEP 個を残す）
ls -1t "$DEST"/jp_stock-*.sql.gz | tail -n +"$((KEEP + 1))" | xargs -r rm -f
