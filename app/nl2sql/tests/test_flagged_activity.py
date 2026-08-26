"""nl2sql/core/flagged_activity。段階2・パターンA（issue_c015）の存在確認クエリ。

`issue_c015` 追加指示（統括）：この照会は自然文の理解を問うものではなく、
「特定の期間にフラグ付き実体の活動があるか」という**データそのものに対する
機械的な正誤判定**。盲検の対象ではなく、既知のデータ事実で直接検証する。

既知の事実（`issue_c012`／独立再測で確認済み）：`stores.store_code='S038'`
（上大岡店、`close_date=2026-01-27`）は、2026年7月の売上が **0件**
（活動なし＝型2が型1に吸収される）、2025年は916件（閉店前で活動あり＝吸収されない）。
同じ実体・同じフラグでも期間次第で分岐が変わることを、期間だけ変えて確かめる。

`issue_c020`：`has_unrepresented_entity`（母集合に実績ゼロの実体が実在するか）も
同じ作法で実データにより検証する。既知の事実（統括が数え、独立に再確認済み）：
営業中の店舗37店のうち実績ゼロは**0店**（空の分岐）、在籍中の会員17,076人のうち
実績ゼロは**1,560人**（非空の分岐）。

`issue_c024`：`region_filter`（`stores.region`の絞り込み）を両関数に足した。
既知の事実（統括が数え、独立に再確認済み）：閉店店舗3件は九州・中国・北海道に
あり、関西／近畿には**0件**（空の分岐）。絞り込み無しなら**3件**（非空の分岐）。

`issue_c028` (a)：`has_unrepresented_time_unit`（対象期間に実績ゼロの暦日が
実在するか）も同じ作法で実データにより検証する。既知の事実（実測）：`sales`の
実績範囲（2024-08-15〜2026-08-14）を通じて実績ゼロの暦日は**0日**（空の分岐、
全期間・2025年・2026年1月のいずれで確かめても同じ）。範囲の外（実績が存在しない
未来の期間、2027年1月）を指定すると**その期間の全日が実績ゼロ**（非空の分岐）。

DBが用意されていない環境では skip する（`test_sql_executor.py` と同じ作法）。
"""
import unittest

from django.test import SimpleTestCase

from app.nl2sql.core.flagged_activity import (
    check_flagged_activity,
    has_unrepresented_entity,
    has_unrepresented_time_unit,
)
from app.nl2sql.core.sql_executor import connect_demo

DEMO_SCHEMA = "demo_sales"


def _probe() -> bool:
    try:
        conn = connect_demo(DEMO_SCHEMA)
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


DEMO_AVAILABLE = _probe()


@unittest.skipUnless(DEMO_AVAILABLE, "demo_sales へ demo_readonly で接続できない")
class CheckFlaggedActivityTests(SimpleTestCase):
    def test_no_activity_when_flagged_store_is_dormant_in_period(self):
        """S038（2026-01-27閉店）は2026年7月の売上が0件→活動なし。"""
        result = check_flagged_activity("stores", "2026-07-01..2026-08-01")
        self.assertFalse(result)

    def test_activity_when_flagged_store_was_open_in_period(self):
        """同じS038でも、閉店前の2025年は916件の売上がある→活動あり。

        実体・フラグは同じで期間だけ変えているため、クエリが期間を
        正しく効かせていることも同時に確かめられる。
        """
        result = check_flagged_activity("stores", "2025-01-01..2026-01-01")
        self.assertTrue(result)

    def test_none_period_means_all_time(self):
        """期間指定が無ければ全期間で見る。S038は2025年に活動があるため真になる。"""
        result = check_flagged_activity("stores", "NONE")
        self.assertTrue(result)

    def test_members_table_runs_without_error(self):
        """members / products も同じ経路で動くことだけ確認する（結果は問わない）。"""
        result = check_flagged_activity("members", "NONE")
        self.assertIn(result, (True, False))

    def test_products_table_runs_without_error(self):
        result = check_flagged_activity("products", "NONE")
        self.assertIn(result, (True, False))

    def test_unknown_table_raises(self):
        with self.assertRaises(KeyError):
            check_flagged_activity("sales", "NONE")


@unittest.skipUnless(DEMO_AVAILABLE, "demo_sales へ demo_readonly で接続できない")
class RegionFilterTests(SimpleTestCase):
    """issue_c024：`region_filter`で絞り込んだ集合に閉店店舗が実在するか（両分岐）。"""

    def test_empty_branch_no_closed_store_in_kansai(self):
        """閉店店舗3件は九州・中国・北海道にあり、関西には0件（空の分岐）。"""
        result = check_flagged_activity("stores", "NONE", region_filter="関西")
        self.assertFalse(result)

    def test_empty_branch_no_closed_store_in_kinki(self):
        """`関西`と表記ゆれの`近畿`でも同じ（同じ実体を指すため）。"""
        result = check_flagged_activity("stores", "NONE", region_filter="近畿")
        self.assertFalse(result)

    def test_non_empty_branch_without_region_filter(self):
        """絞り込み無しなら、閉店店舗3件のうちS038（活動あり）を含むため真になる。"""
        result = check_flagged_activity("stores", "NONE", region_filter=None)
        self.assertTrue(result)

    def test_none_region_filter_string_is_treated_as_no_filter(self):
        """`AxisExtraction.region_filter`の既定値`"NONE"`（文字列）も絞り込み無しと同じ。"""
        result = check_flagged_activity("stores", "NONE", region_filter="NONE")
        self.assertTrue(result)

    def test_region_filter_is_ignored_for_members(self):
        """`region`列を持つのは`stores`のみ。他表では絞り込みを無視して動く。"""
        result = check_flagged_activity("members", "NONE", region_filter="関西")
        self.assertIn(result, (True, False))


@unittest.skipUnless(DEMO_AVAILABLE, "demo_sales へ demo_readonly で接続できない")
class HasUnrepresentedEntityTests(SimpleTestCase):
    """issue_c020：型1を立てる根拠（母集合に実績ゼロの実体が実在するか）。"""

    def test_empty_branch_active_stores_have_no_zero_sales_entity(self):
        """営業中の店舗37店は全店が売上を持つ→実績ゼロは0店（空の分岐）。"""
        result = has_unrepresented_entity("stores", active_only=True, period="NONE")
        self.assertFalse(result)

    def test_non_empty_branch_active_members_have_zero_sales_entities(self):
        """在籍中の会員17,076人のうち1,560人は実績ゼロ（非空の分岐）。"""
        result = has_unrepresented_entity("members", active_only=True, period="NONE")
        self.assertTrue(result)

    def test_products_table_runs_without_error(self):
        result = has_unrepresented_entity("products", active_only=False, period="NONE")
        self.assertIn(result, (True, False))

    def test_unknown_table_raises(self):
        with self.assertRaises(KeyError):
            has_unrepresented_entity("sales", active_only=False, period="NONE")


@unittest.skipUnless(DEMO_AVAILABLE, "demo_sales へ demo_readonly で接続できない")
class HasUnrepresentedTimeUnitTests(SimpleTestCase):
    """issue_c028 (a)：時間軸（GROUP_BY=TIME）の型1を立てる根拠（実績ゼロの暦日）。"""

    def test_empty_branch_no_period_means_all_time(self):
        """期間指定が無ければ全期間で見る。実績範囲を通じて実績ゼロの暦日は0日。"""
        result = has_unrepresented_time_unit("NONE")
        self.assertFalse(result)

    def test_empty_branch_full_year_2025(self):
        """2025年は全期間の内側であり、実績ゼロの暦日は0日（空の分岐）。"""
        result = has_unrepresented_time_unit("2025-01-01..2026-01-01")
        self.assertFalse(result)

    def test_empty_branch_single_month(self):
        """月次の粒度（2026年1月）でも同じ——日次で網羅されていれば月次も網羅される。"""
        result = has_unrepresented_time_unit("2026-01-01..2026-02-01")
        self.assertFalse(result)

    def test_non_empty_branch_period_outside_data_range(self):
        """実績が存在しない未来の期間（2027年1月）は、その期間の全日が実績ゼロ
        （非空の分岐）。"""
        result = has_unrepresented_time_unit("2027-01-01..2027-02-01")
        self.assertTrue(result)
