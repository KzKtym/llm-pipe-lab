"""nl2sql/core/sql_validator。実行前のSQL検査。

ここは「通してはいけないものを通さない」ことと同時に、
「通すべきものを落とさない」ことも試す。誤検出でまともなクエリが落ちると、
正答率の測定そのものが濁るため。
"""
from django.test import SimpleTestCase

from app.nl2sql.core.sql_validator import (
    DEFAULT_MAX_ROWS,
    strip_code_fence,
    validate,
)


class StripCodeFenceTests(SimpleTestCase):
    """LLMは ```sql で囲んで返してくることが多い。"""

    def test_sql_fence_is_removed(self):
        self.assertEqual(strip_code_fence("```sql\nSELECT 1\n```"), "SELECT 1")

    def test_bare_fence_is_removed(self):
        self.assertEqual(strip_code_fence("```\nSELECT 1\n```"), "SELECT 1")

    def test_plain_sql_is_untouched(self):
        self.assertEqual(strip_code_fence("SELECT 1"), "SELECT 1")

    def test_empty_is_empty(self):
        self.assertEqual(strip_code_fence(""), "")


class AcceptedSqlTests(SimpleTestCase):
    """通すべきものを落としていないか。"""

    def test_simple_select_gets_limit(self):
        r = validate("SELECT * FROM sales")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.sql, f"SELECT * FROM sales LIMIT {DEFAULT_MAX_ROWS}")
        self.assertEqual(r.tables, ["sales"])

    def test_code_fenced_select_is_accepted(self):
        r = validate("```sql\nSELECT * FROM sales\n```")
        self.assertTrue(r.ok, r.reason)
        self.assertTrue(r.sql.startswith("SELECT * FROM sales"))

    def test_trailing_semicolon_is_dropped(self):
        r = validate("SELECT * FROM sales;")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.sql, f"SELECT * FROM sales LIMIT {DEFAULT_MAX_ROWS}")

    def test_with_clause_is_accepted(self):
        sql = (
            "WITH monthly AS ("
            "  SELECT store_id, sum(line_amount) AS amt FROM sale_items GROUP BY store_id"
            ") SELECT * FROM monthly ORDER BY amt DESC"
        )
        r = validate(sql)
        self.assertTrue(r.ok, r.reason)
        # CTE名は物理テーブルではないので、参照テーブルから除かれる
        self.assertEqual(r.tables, ["sale_items"])

    def test_line_comment_containing_delete_is_not_flagged(self):
        r = validate("SELECT 1 -- DELETE FROM sales")
        self.assertTrue(r.ok, r.reason)
        self.assertNotIn("DELETE", r.sql)

    def test_nested_block_comment_is_stripped(self):
        r = validate("SELECT /* outer /* inner */ still outer */ 1")
        self.assertTrue(r.ok, r.reason)
        self.assertNotIn("outer", r.sql)

    def test_string_literal_containing_delete_is_not_flagged(self):
        r = validate("SELECT * FROM sales WHERE memo = 'delete from stores'")
        self.assertTrue(r.ok, r.reason)
        # 実行用SQLではリテラルの中身が保たれている
        self.assertIn("'delete from stores'", r.sql)
        self.assertEqual(r.tables, ["sales"])

    def test_escaped_quote_in_literal(self):
        r = validate("SELECT * FROM sales WHERE memo = 'it''s fine'")
        self.assertTrue(r.ok, r.reason)
        self.assertIn("'it''s fine'", r.sql)

    def test_quoted_identifier_named_like_keyword(self):
        r = validate('SELECT "delete" FROM sales')
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["sales"])


class TableExtractionTests(SimpleTestCase):
    def test_join(self):
        r = validate(
            "SELECT * FROM sales JOIN stores ON sales.store_id = stores.store_id"
        )
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["sales", "stores"])

    def test_comma_separated_with_aliases(self):
        r = validate(
            "SELECT * FROM sales s, stores st WHERE s.store_id = st.store_id"
        )
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["sales", "stores"])

    def test_subquery_is_not_a_table_but_inner_from_is_found(self):
        r = validate("SELECT * FROM (SELECT * FROM sale_items) t")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["sale_items"])

    def test_schema_qualified_name(self):
        r = validate("SELECT * FROM demo_sales.sales")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["demo_sales.sales"])

    def test_commas_inside_a_derived_table_are_not_split(self):
        """導出テーブルの中のカンマを区切りにすると、列名を表名と誤認する。"""
        sql = (
            "SELECT store_type FROM ("
            "  SELECT st.store_id, st.store_type, st.floor_area"
            "  FROM stores st"
            ") t GROUP BY store_type"
        )
        r = validate(sql)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["stores"])

    def test_function_arguments_are_not_tables(self):
        sql = (
            "SELECT (FLOOR(date_part('year', age(DATE '2026-08-14', m.birth_date)) / 10)) AS b"
            " FROM members m"
        )
        r = validate(sql)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["members"])

    def test_extract_from_is_not_a_table(self):
        """EXTRACT(YEAR FROM x) の FROM は表参照ではない。"""
        r = validate("SELECT EXTRACT(YEAR FROM sold_at) FROM sales")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["sales"])

    def test_substring_from_is_not_a_table(self):
        r = validate("SELECT substring(store_code FROM 1 FOR 2) FROM stores")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["stores"])

    def test_subquery_in_where_is_still_checked(self):
        """IN (SELECT ...) の中の表は拾う。ここを漏らすと照合が緩む。"""
        r = validate("SELECT * FROM stores WHERE store_id IN (SELECT store_id FROM auth_user)")
        self.assertEqual(r.tables, ["stores", "auth_user"])

    def test_lateral_is_skipped(self):
        r = validate("SELECT * FROM stores s, LATERAL (SELECT 1) x")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["stores"])

    def test_only_is_skipped(self):
        r = validate("SELECT * FROM ONLY stores")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["stores"])

    def test_duplicates_are_collapsed_in_order(self):
        r = validate("SELECT * FROM sales JOIN sales s2 ON true JOIN stores ON true")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["sales", "stores"])


class RejectedSqlTests(SimpleTestCase):
    """通してはいけないもの。"""

    def test_empty(self):
        r = validate("   ")
        self.assertFalse(r.ok)
        self.assertIn("空", r.reason)

    def test_multiple_statements(self):
        r = validate("SELECT 1; DROP TABLE sales")
        self.assertFalse(r.ok)
        self.assertIn("複数の文", r.reason)

    def test_not_starting_with_select(self):
        r = validate("UPDATE sales SET total_amount = 0")
        self.assertFalse(r.ok)

    def test_insert_inside_cte(self):
        """Postgres は CTE 内に INSERT を書ける。ここが一番の穴。"""
        sql = (
            "WITH x AS (INSERT INTO sales (total_amount) VALUES (1) RETURNING *) "
            "SELECT * FROM x"
        )
        r = validate(sql)
        self.assertFalse(r.ok)
        self.assertIn("INSERT", r.reason)

    def test_delete_inside_cte(self):
        sql = "WITH x AS (DELETE FROM sales RETURNING *) SELECT * FROM x"
        r = validate(sql)
        self.assertFalse(r.ok)
        self.assertIn("DELETE", r.reason)

    def test_select_into_creates_table(self):
        r = validate("SELECT * INTO backup FROM sales")
        self.assertFalse(r.ok)
        self.assertIn("INTO", r.reason)

    def test_select_for_update_takes_lock(self):
        r = validate("SELECT * FROM sales FOR UPDATE")
        self.assertFalse(r.ok)
        self.assertIn("UPDATE", r.reason)

    def test_table_shorthand_is_rejected(self):
        """`TABLE foo` は FROM を持たないためホワイトリスト照合をすり抜ける。"""
        r = validate("TABLE demo_sales.sales")
        self.assertFalse(r.ok)

    def test_forbidden_function(self):
        r = validate("SELECT pg_sleep(10)")
        self.assertFalse(r.ok)
        self.assertIn("pg_sleep", r.reason)

    def test_set_config_function(self):
        r = validate("SELECT set_config('a', 'b', true)")
        self.assertFalse(r.ok)
        self.assertIn("set_config", r.reason)

    def test_dollar_quote(self):
        r = validate("SELECT $$abc$$")
        self.assertFalse(r.ok)
        self.assertIn("ドル引用符", r.reason)

    def test_fetch_first(self):
        r = validate("SELECT * FROM sales FETCH FIRST 10 ROWS ONLY")
        self.assertFalse(r.ok)
        self.assertIn("FETCH", r.reason)


class LimitTests(SimpleTestCase):
    def test_existing_limit_within_cap_is_kept(self):
        r = validate("SELECT * FROM sales LIMIT 10", max_rows=1000)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.sql, "SELECT * FROM sales LIMIT 10")

    def test_existing_limit_over_cap_is_clamped(self):
        r = validate("SELECT * FROM sales LIMIT 5000", max_rows=1000)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.sql, "SELECT * FROM sales LIMIT 1000")

    def test_limit_with_offset_is_kept(self):
        r = validate("SELECT * FROM sales LIMIT 10 OFFSET 20", max_rows=1000)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.sql, "SELECT * FROM sales LIMIT 10 OFFSET 20")

    def test_limit_all_is_replaced(self):
        r = validate("SELECT * FROM sales LIMIT ALL", max_rows=1000)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.sql, "SELECT * FROM sales LIMIT 1000")

    def test_order_by_is_preserved_before_appended_limit(self):
        """上位N件を問う質問のため、ORDER BY の効きを変えない付け方にしている。"""
        r = validate("SELECT * FROM sales ORDER BY total_amount DESC", max_rows=5)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.sql, "SELECT * FROM sales ORDER BY total_amount DESC LIMIT 5")


class AllowedTablesTests(SimpleTestCase):
    def test_allowed_table_passes(self):
        r = validate("SELECT * FROM sales", allowed_tables={"sales", "stores"})
        self.assertTrue(r.ok, r.reason)

    def test_unknown_table_is_rejected(self):
        r = validate("SELECT * FROM auth_user", allowed_tables={"sales"})
        self.assertFalse(r.ok)
        self.assertIn("auth_user", r.reason)

    def test_schema_qualified_matches_bare_name_in_whitelist(self):
        r = validate("SELECT * FROM demo_sales.sales", allowed_tables={"sales"})
        self.assertTrue(r.ok, r.reason)

    def test_cte_name_is_not_checked_against_whitelist(self):
        sql = "WITH t AS (SELECT 1 AS a FROM sales) SELECT * FROM t"
        r = validate(sql, allowed_tables={"sales"})
        self.assertTrue(r.ok, r.reason)

    def test_nested_from_is_checked(self):
        r = validate(
            "SELECT * FROM (SELECT * FROM auth_user) x", allowed_tables={"sales"}
        )
        self.assertFalse(r.ok)
        self.assertIn("auth_user", r.reason)

    def test_none_means_no_check(self):
        r = validate("SELECT * FROM anything", allowed_tables=None)
        self.assertTrue(r.ok, r.reason)


class FromFunctionTests(SimpleTestCase):
    """`FROM func(...)` は表参照ではない。

    許可した集合返却関数だけ読み飛ばし、**知らない関数は表名として扱って
    ホワイトリストで落とす**（素通しさせない）。

    `generate_series` を許すのは、「取引の無かった日も 0 で並べる」
    カレンダースパインがこのプロジェクトの検出対象（型1）の答えそのもの
    だからで、弾くと正しい SQL をこちらが落とすことになる。
    """

    ALLOWED = {"demo_sales.sales", "sales"}

    def test_calendar_spine_passes(self):
        sql = (
            "SELECT d::date, count(s.sale_id) "
            "FROM generate_series('2026-01-01'::date, '2026-01-31'::date, "
            "interval '1 day') d "
            "LEFT JOIN demo_sales.sales s ON s.sold_at::date = d GROUP BY 1"
        )
        r = validate(sql, allowed_tables=self.ALLOWED)
        self.assertTrue(r.ok, r.reason)
        # 関数は表として数えない。結合先の実表だけが残る
        self.assertEqual(r.tables, ["demo_sales.sales"])

    def test_unnest_passes(self):
        r = validate("SELECT x FROM unnest(ARRAY[1,2,3]) x", allowed_tables=self.ALLOWED)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, [])

    def test_unlisted_function_is_rejected(self):
        """許可リストに無い集合返却関数は、表名として落とす。"""
        r = validate(
            "SELECT * FROM json_populate_record(null::record, '{}') r",
            allowed_tables=self.ALLOWED,
        )
        self.assertFalse(r.ok)
        self.assertIn("json_populate_record", r.reason)

    def test_forbidden_function_still_blocked(self):
        """禁止関数の検査は、この読み飛ばしより前に効く。"""
        r = validate("SELECT * FROM pg_ls_dir('/etc') f", allowed_tables=self.ALLOWED)
        self.assertFalse(r.ok)
        self.assertIn("pg_ls_dir", r.reason)

    def test_bare_word_is_still_treated_as_table(self):
        """`(` が続かなければ関数ではない。表名として扱う。"""
        r = validate("SELECT 1 FROM generate_series", allowed_tables=self.ALLOWED)
        self.assertFalse(r.ok)
        self.assertIn("generate_series", r.reason)

    def test_function_in_select_list_is_unrelated(self):
        r = validate(
            "SELECT generate_series(1, 3) FROM demo_sales.sales",
            allowed_tables=self.ALLOWED,
        )
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.tables, ["demo_sales.sales"])
