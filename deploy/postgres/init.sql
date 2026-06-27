-- jp-stock-pipeline 本番 DB / ロール作成（冪等）。
-- 実行例:
--   sudo -u postgres psql -v pw="'$(openssl rand -base64 24)'" -f deploy/postgres/init.sql
-- ↑ -v pw='...' で渡したパスワードを CREATE ROLE に注入する。出力された値を控え、
--   .env(LOCAL_DB_PASSWORD) と GitHub Secrets(LOCAL_DB_PASSWORD) に設定すること。
-- テーブルは初回ジョブ実行時に init_schema() が CREATE TABLE IF NOT EXISTS で作る。

-- ロール（既存なら作らない）
SELECT 'CREATE ROLE jp_stock LOGIN PASSWORD ' || :pw
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'jp_stock')
\gexec

-- DB（既存なら作らない。CREATE DATABASE はトランザクション外で実行）
SELECT 'CREATE DATABASE jp_stock OWNER jp_stock'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'jp_stock')
\gexec

\echo 'jp_stock ロール/DB を用意しました。設定したパスワードを .env と GitHub Secrets へ。'
