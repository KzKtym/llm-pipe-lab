-- ============================================================================
-- 002_roles_superuser.sql
--   参照専用ロール demo_readonly を作成する。
--   ★ superuser（例: postgres）で「一度だけ」実行すること。admin では実行できない
--      （admin は superuser でも CREATEROLE でもないため）。
--   ★ パスワードはこのファイルに書かない。psql 変数 :demo_pw で外から渡す。
--   実行例（README も参照）:
--      demo_pw="$(openssl rand -base64 24)"
--      sudo -u postgres psql -d llm_pipe_lab -v demo_pw="$demo_pw" \
--           -f sample/sales/002_roles_superuser.sql
--   冪等: ロールが既に存在する場合は作成せず、パスワードも変更しない。
-- ============================================================================

\set ON_ERROR_STOP on

-- psql 変数をセッション設定に移し、ドル引用ブロック内から参照できるようにする
-- （psql の :'demo_pw' 展開はドル引用文字列の内側では効かないため、この方式を使う）
SELECT set_config('demo.pw', :'demo_pw', false);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'demo_readonly') THEN
        EXECUTE format('CREATE ROLE demo_readonly LOGIN PASSWORD %L', current_setting('demo.pw'));
        RAISE NOTICE 'ロール demo_readonly を作成しました。';
    ELSE
        RAISE NOTICE 'ロール demo_readonly は既に存在します。作成をスキップ（パスワードは変更しません）。';
    END IF;
END
$$;

-- セッション変数からパスワードを消去
SELECT set_config('demo.pw', '', false);

COMMENT ON ROLE demo_readonly IS 'demo_sales スキーマの参照専用ロール。NL→SQL 実行用。SELECT のみ。';
