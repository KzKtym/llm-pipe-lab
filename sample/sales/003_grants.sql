-- ============================================================================
-- 003_grants.sql
--   demo_readonly に demo_sales スキーマの参照権限を付与する。
--   ★ admin（＝スキーマ・テーブルの所有者）で実行すること。何度でも安全に再実行可。
--   ★ 前提: 002_roles_superuser.sql で demo_readonly が作成済みであること。
--   実行例:
--      PGPASSWORD=admin psql -h localhost -U admin -d llm_pipe_lab \
--           -f sample/sales/003_grants.sql
--
--   ALTER DEFAULT PRIVILEGES は「実行したロールが今後作成するオブジェクト」に効く。
--   テーブルを作るのは admin なので、この文も admin で実行する必要がある
--   （superuser で実行しても admin が作る将来のテーブルには適用されない）。
--
--   public スキーマのアプリ用テーブルには一切権限を与えない（付与文を書かない）。
-- ============================================================================

\set ON_ERROR_STOP on

-- スキーマの利用権限
GRANT USAGE ON SCHEMA demo_sales TO demo_readonly;

-- 既存の全テーブルへの SELECT
GRANT SELECT ON ALL TABLES IN SCHEMA demo_sales TO demo_readonly;

-- 将来 admin が demo_sales に追加するテーブルにも自動で SELECT を付与
ALTER DEFAULT PRIVILEGES IN SCHEMA demo_sales GRANT SELECT ON TABLES TO demo_readonly;

-- 確認用（付与済み権限の一覧）
\echo '--- demo_readonly に付与された demo_sales のテーブル権限 ---'
SELECT table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'demo_readonly' AND table_schema = 'demo_sales'
ORDER BY table_name;
