"""nl2sql/core/result_compare。実行結果の突き合わせ。

ここが甘いと、正しいSQLを不正解と判定して以降の比較がすべて濁る。
「同じ答えなのに型や書き方が違うだけ」のパターンを落とさないことを重点的に試す。
"""
from datetime import date, datetime
from decimal import Decimal

from django.test import SimpleTestCase

from app.nl2sql.core.result_compare import (
    ComparisonResult,
    compare_query_results,
    compare_rows,
    has_top_level_order_by,
    normalize_value,
)
from app.nl2sql.core.sql_executor import QueryResult


class NormalizeValueTests(SimpleTestCase):
    def test_numeric_types_converge(self):
        self.assertEqual(normalize_value(1200), normalize_value(Decimal("1200.00")))
        self.assertEqual(normalize_value(1200), normalize_value(1200.0))

    def test_float_rounding(self):
        self.assertEqual(normalize_value(0.1 + 0.2), normalize_value(0.3))

    def test_negative_zero_matches_zero(self):
        self.assertEqual(normalize_value(Decimal("-0.0")), normalize_value(0))

    def test_bool_is_not_treated_as_number(self):
        """Python では True == 1 == Decimal(1) になるため、文字列へ逃がしてある。"""
        self.assertNotEqual(normalize_value(True), normalize_value(1))
        self.assertNotEqual(normalize_value(False), normalize_value(0))
        self.assertEqual(normalize_value(True), "true")
        self.assertEqual(normalize_value(False), "false")

    def test_none(self):
        self.assertIsNone(normalize_value(None))

    def test_date_and_datetime(self):
        self.assertEqual(normalize_value(date(2026, 8, 14)), "2026-08-14")
        self.assertEqual(
            normalize_value(datetime(2026, 8, 14, 9, 30)), "2026-08-14T09:30:00"
        )

    def test_trailing_space_is_ignored(self):
        """char(n) のパディング対策。先頭の空白は意味があり得るので残す。"""
        self.assertEqual(normalize_value("関西  "), "関西")
        self.assertEqual(normalize_value("  関西"), "  関西")

    def test_large_number_does_not_raise(self):
        self.assertEqual(normalize_value(Decimal("1E+30")), Decimal("1E+30"))


class HasTopLevelOrderByTests(SimpleTestCase):
    def test_top_level(self):
        self.assertTrue(has_top_level_order_by("SELECT a FROM t ORDER BY a"))

    def test_case_insensitive(self):
        self.assertTrue(has_top_level_order_by("select a from t order by a"))

    def test_none(self):
        self.assertFalse(has_top_level_order_by("SELECT a FROM t"))

    def test_inside_subquery_is_not_top_level(self):
        sql = "SELECT * FROM (SELECT a FROM t ORDER BY a) x"
        self.assertFalse(has_top_level_order_by(sql))

    def test_inside_window_function_is_not_top_level(self):
        sql = "SELECT row_number() OVER (ORDER BY a) FROM t"
        self.assertFalse(has_top_level_order_by(sql))

    def test_inside_cte_is_not_top_level(self):
        sql = "WITH x AS (SELECT a FROM t ORDER BY a) SELECT * FROM x"
        self.assertFalse(has_top_level_order_by(sql))

    def test_cte_with_outer_order_by(self):
        sql = "WITH x AS (SELECT a FROM t) SELECT * FROM x ORDER BY a"
        self.assertTrue(has_top_level_order_by(sql))

    def test_string_literal_is_ignored(self):
        self.assertFalse(has_top_level_order_by("SELECT 'order by' FROM t"))

    def test_comment_is_ignored(self):
        self.assertFalse(has_top_level_order_by("SELECT a FROM t -- ORDER BY a"))

    def test_word_boundary(self):
        self.assertFalse(has_top_level_order_by("SELECT ordering FROM t"))
        self.assertFalse(has_top_level_order_by("SELECT a FROM t ORDERBY a"))


class CompareRowsTests(SimpleTestCase):
    def test_both_empty(self):
        self.assertTrue(compare_rows([], [], ordered=False).match)

    def test_identical(self):
        rows = [("A", 1), ("B", 2)]
        self.assertTrue(compare_rows(rows, rows, ordered=False).match)

    def test_type_difference_still_matches(self):
        gold = [("A", Decimal("1200.00"))]
        actual = [("A", 1200)]
        self.assertTrue(compare_rows(gold, actual, ordered=False).match)

    def test_row_order_ignored_when_unordered(self):
        gold = [("A", 1), ("B", 2)]
        actual = [("B", 2), ("A", 1)]
        self.assertTrue(compare_rows(gold, actual, ordered=False).match)

    def test_row_order_matters_when_ordered(self):
        gold = [("A", 1), ("B", 2)]
        actual = [("B", 2), ("A", 1)]
        result = compare_rows(gold, actual, ordered=True)
        self.assertFalse(result.match)
        self.assertEqual(result.reason, "行の並び順が違う")

    def test_ordered_and_identical(self):
        rows = [("A", 1), ("B", 2)]
        self.assertTrue(compare_rows(rows, rows, ordered=True).match)

    def test_row_count_mismatch(self):
        result = compare_rows([("A",)], [("A",), ("B",)], ordered=False)
        self.assertFalse(result.match)
        self.assertIn("行数が違う", result.reason)

    def test_column_count_mismatch(self):
        result = compare_rows([("A", 1)], [("A", 1, 2)], ordered=False)
        self.assertFalse(result.match)
        self.assertIn("列数が違う", result.reason)

    def test_value_mismatch(self):
        result = compare_rows([("A", 1)], [("A", 2)], ordered=False)
        self.assertFalse(result.match)
        self.assertEqual(result.reason, "値が違う")

    def test_duplicate_rows_are_counted(self):
        """多重集合として比べる。重複の数が違えば不一致。"""
        gold = [("A",), ("A",)]
        actual = [("A",), ("A",), ("A",)]
        self.assertFalse(compare_rows(gold, actual, ordered=False).match)

    def test_null_matches_null(self):
        self.assertTrue(compare_rows([(None,)], [(None,)], ordered=False).match)

    def test_null_does_not_match_zero(self):
        self.assertFalse(compare_rows([(None,)], [(0,)], ordered=False).match)

    def test_gold_empty_actual_not(self):
        result = compare_rows([], [("A",)], ordered=False)
        self.assertFalse(result.match)
        self.assertIn("行数が違う", result.reason)


class CompareQueryResultsTests(SimpleTestCase):
    def _result(self, rows, ok=True):
        return QueryResult(ok=ok, rows=rows, row_count=len(rows))

    def test_order_sensitivity_comes_from_gold_sql(self):
        gold = self._result([("A", 1), ("B", 2)])
        actual = self._result([("B", 2), ("A", 1)])

        unordered = compare_query_results(
            gold, actual, gold_sql="SELECT a, b FROM t"
        )
        ordered = compare_query_results(
            gold, actual, gold_sql="SELECT a, b FROM t ORDER BY b DESC"
        )

        self.assertTrue(unordered.match)
        self.assertFalse(ordered.match)

    def test_gold_failure(self):
        result = compare_query_results(
            self._result([], ok=False), self._result([("A",)]), gold_sql="SELECT a FROM t"
        )
        self.assertFalse(result.match)
        self.assertIn("正解SQL", result.reason)

    def test_actual_failure(self):
        result = compare_query_results(
            self._result([("A",)]), self._result([], ok=False), gold_sql="SELECT a FROM t"
        )
        self.assertFalse(result.match)
        self.assertIn("生成SQL", result.reason)


class ComparisonResultTests(SimpleTestCase):
    def test_truthiness(self):
        self.assertTrue(bool(ComparisonResult(True)))
        self.assertFalse(bool(ComparisonResult(False, "だめ")))
