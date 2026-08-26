"""nl2sql/core/sql_executor。読み取り専用での実行とエラー分類。

分類は純粋な関数なのでDB無しで試す。読み取り専用と実行時間の上限は、
実際にDBへ当てないと「効いているつもり」で終わるため、demo_sales に対して確かめる。
DBが用意されていない環境では結合テストを skip する。
"""
import unittest

from django.test import SimpleTestCase

from app.nl2sql.core.schema_introspector import SchemaSnapshot, TableInfo, introspect
from app.nl2sql.core.sql_executor import (
    QueryResult,
    SqlExecutor,
    allowed_tables_from,
    classify_error,
    connect_demo,
    execute_sql,
)

DEMO_SCHEMA = "demo_sales"


def _admin_connection():
    """書き込み権限のある接続。読み取り専用トランザクションの効きを試すために使う。"""
    import psycopg2
    from decouple import config

    return psycopg2.connect(
        dbname=config("DB_NAME", default="llm_pipe_lab"),
        user=config("DB_USER", default="admin"),
        password=config("DB_PASSWORD", default="admin"),
        host=config("DB_HOST", default="localhost"),
        port=config("DB_PORT", default="5432"),
        options=f"-c search_path={DEMO_SCHEMA}",
    )


def _probe(factory) -> bool:
    try:
        conn = factory()
    except Exception:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {DEMO_SCHEMA}.stores LIMIT 1")
            cur.fetchall()
        return True
    except Exception:
        return False
    finally:
        conn.close()


DEMO_AVAILABLE = _probe(lambda: connect_demo(DEMO_SCHEMA))
ADMIN_AVAILABLE = _probe(_admin_connection)


class ClassifyErrorTests(SimpleTestCase):
    def test_known_codes(self):
        self.assertEqual(classify_error("42601"), "syntax")
        self.assertEqual(classify_error("42P01"), "undefined")
        self.assertEqual(classify_error("42703"), "undefined")
        self.assertEqual(classify_error("42501"), "permission")
        self.assertEqual(classify_error("57014"), "timeout")
        self.assertEqual(classify_error("25006"), "read_only")

    def test_semantic_errors(self):
        self.assertEqual(classify_error("42702"), "ambiguous")
        self.assertEqual(classify_error("42846"), "type")
        self.assertEqual(classify_error("42803"), "grouping")

    def test_falls_back_to_class(self):
        self.assertEqual(classify_error("08006"), "connection")
        self.assertEqual(classify_error("53200"), "resource")

    def test_unknown_is_other(self):
        self.assertEqual(classify_error("99999"), "other")

    def test_none_is_other(self):
        self.assertEqual(classify_error(None), "other")
        self.assertEqual(classify_error(""), "other")


class AllowedTablesFromTests(SimpleTestCase):
    def test_includes_bare_and_qualified_names(self):
        snapshot = SchemaSnapshot(
            schema="demo_sales",
            tables=[
                TableInfo(name="stores", schema="demo_sales"),
                TableInfo(name="sales", schema="demo_sales"),
            ],
        )
        self.assertEqual(
            allowed_tables_from(snapshot),
            {"stores", "demo_sales.stores", "sales", "demo_sales.sales"},
        )

    def test_empty_snapshot(self):
        self.assertEqual(allowed_tables_from(SchemaSnapshot(schema="x")), set())


class ValidationShortCircuitTests(SimpleTestCase):
    """検査で落ちた場合、DBに触れないこと。"""

    def _executor(self):
        def _explode():
            raise AssertionError("接続してはいけない")

        return SqlExecutor(connect=_explode)

    def test_write_statement_is_rejected_before_connecting(self):
        result = self._executor().run("DELETE FROM stores")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "validation")
        self.assertEqual(result.sql, "")

    def test_multiple_statements_rejected(self):
        result = self._executor().run("SELECT 1; SELECT 2")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "validation")

    def test_table_outside_whitelist_rejected(self):
        executor = SqlExecutor(
            connect=lambda: (_ for _ in ()).throw(AssertionError("接続してはいけない")),
            allowed_tables={"stores"},
        )
        result = executor.run("SELECT * FROM auth_user")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "validation")
        self.assertIn("auth_user", result.error)


class ConnectionFailureTests(SimpleTestCase):
    def test_connect_failure_is_reported_as_connection(self):
        def _fail():
            raise OSError("接続できません")

        result = SqlExecutor(connect=_fail).run("SELECT * FROM stores")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "connection")
        self.assertIn("接続できません", result.error)


@unittest.skipUnless(DEMO_AVAILABLE, "demo_sales へ demo_readonly で接続できない")
class DemoQueryTests(SimpleTestCase):
    """demo_readonly での実行。"""

    def setUp(self):
        self.executor = SqlExecutor(schema=DEMO_SCHEMA)
        self.addCleanup(self.executor.close)

    def test_select_returns_rows_and_columns(self):
        result = self.executor.run("SELECT store_code, store_name FROM stores ORDER BY store_code")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.columns, ["store_code", "store_name"])
        self.assertEqual(result.row_count, 40)
        self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_qualified_name_also_works(self):
        result = self.executor.run(f"SELECT count(*) FROM {DEMO_SCHEMA}.stores")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.rows[0][0], 40)

    def test_limit_is_applied_and_truncation_flagged(self):
        executor = SqlExecutor(schema=DEMO_SCHEMA, max_rows=10)
        self.addCleanup(executor.close)
        result = executor.run("SELECT * FROM sale_items")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.row_count, 10)
        self.assertTrue(result.truncated)
        self.assertIn("LIMIT 10", result.sql)

    def test_not_truncated_when_under_cap(self):
        result = self.executor.run("SELECT * FROM stores")
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.truncated)

    def test_undefined_column_is_classified(self):
        result = self.executor.run("SELECT no_such_column FROM stores")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "undefined")
        self.assertTrue(result.error)

    def test_undefined_table_is_classified(self):
        result = self.executor.run("SELECT * FROM no_such_table")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "undefined")

    def test_syntax_error_is_classified(self):
        result = self.executor.run("SELECT FROM WHERE stores")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "syntax")

    def test_public_schema_is_out_of_search_path(self):
        """search_path を demo_sales に絞っているため、修飾なしでは public に届かない。"""
        result = self.executor.run("SELECT * FROM auth_user")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "undefined")

    def test_public_schema_is_denied_even_when_qualified(self):
        """修飾しても、ロールに権限が無いので読めない。"""
        result = self.executor.run("SELECT * FROM public.auth_user")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "permission")

    def test_timeout_is_classified(self):
        executor = SqlExecutor(schema=DEMO_SCHEMA, timeout_ms=50)
        self.addCleanup(executor.close)
        result = executor.run(
            "SELECT count(*) FROM sale_items a JOIN sale_items b ON a.quantity = b.quantity"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "timeout")

    def test_connection_is_reused_across_runs(self):
        first = self.executor.run("SELECT 1 AS a")
        second = self.executor.run("SELECT 2 AS a")
        self.assertTrue(first.ok, first.error)
        self.assertTrue(second.ok, second.error)
        self.assertEqual(second.rows[0][0], 2)

    def test_error_does_not_break_the_next_query(self):
        """失敗したトランザクションを rollback しているので、次のクエリが通ること。"""
        failed = self.executor.run("SELECT no_such_column FROM stores")
        self.assertFalse(failed.ok)
        recovered = self.executor.run("SELECT count(*) FROM stores")
        self.assertTrue(recovered.ok, recovered.error)
        self.assertEqual(recovered.rows[0][0], 40)

    def test_allowed_tables_from_live_snapshot(self):
        conn = connect_demo(DEMO_SCHEMA)
        try:
            snapshot = introspect(conn, DEMO_SCHEMA)
        finally:
            conn.close()
        allowed = allowed_tables_from(snapshot)
        self.assertIn("demo_sales.sale_items", allowed)
        self.assertIn("sale_items", allowed)
        self.assertNotIn("auth_user", allowed)


@unittest.skipUnless(ADMIN_AVAILABLE, "admin で demo_sales へ接続できない")
class ReadOnlyTransactionTests(SimpleTestCase):
    """2層目だけの効きを見る。

    検査層（1層目）とロール（3層目）を外し、書き込み権限のある admin 接続で
    書き込みを試す。`SET TRANSACTION READ ONLY` だけで止まることを確かめる。
    """

    def setUp(self):
        self.conn = _admin_connection()
        self.addCleanup(self.conn.close)

    def test_insert_is_blocked_by_read_only_transaction(self):
        result = execute_sql(
            self.conn,
            "INSERT INTO stores (store_code, store_name, region, pref, store_type, open_date)"
            " VALUES ('X999', 'テスト店', '関西', '大阪府', '路面', CURRENT_DATE)",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "read_only")

    def test_update_is_blocked(self):
        result = execute_sql(self.conn, "UPDATE stores SET store_name = 'x'")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "read_only")

    def test_create_table_is_blocked(self):
        result = execute_sql(self.conn, "CREATE TABLE should_not_exist (a int)")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "read_only")

    def test_select_still_works(self):
        result = execute_sql(self.conn, "SELECT count(*) FROM stores")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.rows[0][0], 40)

    def test_nothing_was_written(self):
        """上の書き込みが1件も残っていないこと。"""
        result = execute_sql(self.conn, "SELECT count(*) FROM stores WHERE store_code = 'X999'")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.rows[0][0], 0)


class QueryResultTests(SimpleTestCase):
    def test_defaults(self):
        result = QueryResult(ok=True)
        self.assertEqual(result.columns, [])
        self.assertEqual(result.rows, [])
        self.assertEqual(result.row_count, 0)
        self.assertFalse(result.truncated)
