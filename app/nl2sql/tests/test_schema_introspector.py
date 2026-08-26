"""nl2sql/core/schema_introspector。スキーマの取得と、LLMへ渡すテキストの整形。

整形はDB不要の純粋な処理なので手組みのデータで試す。取得側は実際にDBへ
テンポラリのスキーマを作って確かめる。SQLの正しさ（複合キーの列順、複合外部キーの
束ね方、コメントの取得）は、実物に当てないと検証にならない。
"""
from django.db import connection
from django.test import SimpleTestCase, TestCase

from app.nl2sql.core.schema_introspector import (
    ColumnInfo,
    ForeignKey,
    SchemaSnapshot,
    TableInfo,
    introspect,
    render_schema_text,
)

TEST_SCHEMA = "introspect_test"

_SETUP_SQL = f"""
CREATE SCHEMA {TEST_SCHEMA};

CREATE TABLE {TEST_SCHEMA}.parent (
    parent_id integer NOT NULL,
    code      text    NOT NULL,
    memo      text,
    PRIMARY KEY (parent_id)
);
COMMENT ON TABLE {TEST_SCHEMA}.parent IS '親テーブル';
COMMENT ON COLUMN {TEST_SCHEMA}.parent.parent_id IS '親ID
（改行を含む説明）';

CREATE TABLE {TEST_SCHEMA}.child (
    child_id  integer NOT NULL,
    parent_id integer,
    amount    numeric(12,2),
    PRIMARY KEY (child_id),
    FOREIGN KEY (parent_id) REFERENCES {TEST_SCHEMA}.parent (parent_id)
);

-- 複合主キーの列順が (b, a) であることを確かめるための表
CREATE TABLE {TEST_SCHEMA}.composite (
    a integer NOT NULL,
    b integer NOT NULL,
    PRIMARY KEY (b, a)
);

CREATE VIEW {TEST_SCHEMA}.child_view AS
    SELECT child_id, amount FROM {TEST_SCHEMA}.child;
"""


def _column(table: TableInfo, name: str) -> ColumnInfo:
    return next(c for c in table.columns if c.name == name)


class IntrospectTests(TestCase):
    """実DBに対する取得。TestCase の transaction でロールバックされる。"""

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute(_SETUP_SQL)
        self.snapshot = introspect(connection, TEST_SCHEMA)

    def test_tables_are_sorted_by_name(self):
        self.assertEqual(
            self.snapshot.table_names, ["child", "child_view", "composite", "parent"]
        )

    def test_unknown_schema_returns_empty(self):
        snap = introspect(connection, "no_such_schema")
        self.assertEqual(snap.tables, [])
        self.assertEqual(snap.schema, "no_such_schema")

    def test_table_comment(self):
        self.assertEqual(self.snapshot.get("parent").comment, "親テーブル")

    def test_column_comment_is_collapsed_to_one_line(self):
        col = _column(self.snapshot.get("parent"), "parent_id")
        self.assertEqual(col.comment, "親ID （改行を含む説明）")

    def test_column_without_comment_is_empty_string(self):
        self.assertEqual(_column(self.snapshot.get("parent"), "memo").comment, "")

    def test_column_types(self):
        child = self.snapshot.get("child")
        self.assertEqual(_column(child, "child_id").data_type, "integer")
        self.assertEqual(_column(child, "amount").data_type, "numeric(12,2)")
        self.assertEqual(_column(self.snapshot.get("parent"), "code").data_type, "text")

    def test_not_null_flags(self):
        child = self.snapshot.get("child")
        self.assertTrue(_column(child, "child_id").not_null)
        self.assertFalse(_column(child, "parent_id").not_null)

    def test_column_order_follows_definition(self):
        self.assertEqual(
            [c.name for c in self.snapshot.get("child").columns],
            ["child_id", "parent_id", "amount"],
        )

    def test_single_primary_key(self):
        self.assertEqual(self.snapshot.get("parent").primary_key, ["parent_id"])

    def test_composite_primary_key_keeps_declared_order(self):
        self.assertEqual(self.snapshot.get("composite").primary_key, ["b", "a"])

    def test_foreign_key(self):
        fks = self.snapshot.get("child").foreign_keys
        self.assertEqual(len(fks), 1)
        fk = fks[0]
        self.assertEqual(fk.columns, ["parent_id"])
        self.assertEqual(fk.ref_schema, TEST_SCHEMA)
        self.assertEqual(fk.ref_table, "parent")
        self.assertEqual(fk.ref_columns, ["parent_id"])

    def test_table_without_foreign_key(self):
        self.assertEqual(self.snapshot.get("parent").foreign_keys, [])

    def test_view_is_included_with_its_relkind(self):
        view = self.snapshot.get("child_view")
        self.assertEqual(view.relkind, "v")
        self.assertEqual([c.name for c in view.columns], ["child_id", "amount"])

    def test_relkind_filter_excludes_views(self):
        snap = introspect(connection, TEST_SCHEMA, relkinds=("r",))
        self.assertEqual(snap.table_names, ["child", "composite", "parent"])

    def test_rendered_text_is_usable(self):
        text = render_schema_text(self.snapshot)
        self.assertIn(f"CREATE TABLE {TEST_SCHEMA}.parent (", text)
        self.assertIn("-- 親テーブル", text)
        self.assertIn("PRIMARY KEY (b, a)", text)
        self.assertIn(
            f"FOREIGN KEY (parent_id) REFERENCES {TEST_SCHEMA}.parent (parent_id)", text
        )
        self.assertIn(f"CREATE VIEW {TEST_SCHEMA}.child_view (", text)


class RenderSchemaTextTests(SimpleTestCase):
    """整形。DBに依存しない。"""

    def _snapshot(self) -> SchemaSnapshot:
        stores = TableInfo(
            name="stores",
            schema="demo_sales",
            comment="店舗マスタ",
            columns=[
                ColumnInfo("store_id", "integer", True, "店舗ID"),
                ColumnInfo("region", "text", False, "広域区分"),
            ],
            primary_key=["store_id"],
        )
        sales = TableInfo(
            name="sales",
            schema="demo_sales",
            comment="取引ヘッダ",
            columns=[
                ColumnInfo("sale_id", "integer", True, "取引ID"),
                ColumnInfo("store_id", "integer", True, ""),
            ],
            primary_key=["sale_id"],
            foreign_keys=[
                ForeignKey(["store_id"], "demo_sales", "stores", ["store_id"])
            ],
        )
        return SchemaSnapshot(schema="demo_sales", tables=[sales, stores])

    def test_includes_comments_by_default(self):
        text = render_schema_text(self._snapshot())
        self.assertIn("-- 店舗マスタ", text)
        self.assertIn("-- 店舗ID", text)

    def test_comments_can_be_omitted(self):
        text = render_schema_text(self._snapshot(), include_comments=False)
        self.assertNotIn("店舗マスタ", text)
        self.assertNotIn("店舗ID", text)
        # 構造そのものは残る
        self.assertIn("CREATE TABLE demo_sales.stores (", text)
        self.assertIn("PRIMARY KEY (store_id)", text)

    def test_not_null_is_rendered(self):
        text = render_schema_text(self._snapshot())
        self.assertIn("store_id integer NOT NULL", text)
        self.assertIn("region text,", text)

    def test_last_line_has_no_trailing_comma(self):
        text = render_schema_text(self._snapshot(), tables=["stores"])
        self.assertIn("PRIMARY KEY (store_id)\n);", text)

    def test_comma_precedes_inline_comment(self):
        """コメントを行末に置くため、カンマはコメントより前に来る。"""
        text = render_schema_text(self._snapshot(), tables=["stores"])
        self.assertIn("store_id integer NOT NULL, -- 店舗ID", text)

    def test_tables_filter_keeps_snapshot_order(self):
        text = render_schema_text(self._snapshot(), tables=["stores", "sales"])
        # snapshot の順序（sales, stores）に従う
        self.assertLess(text.index("demo_sales.sales"), text.index("demo_sales.stores"))

    def test_tables_filter_ignores_unknown_names(self):
        text = render_schema_text(self._snapshot(), tables=["stores", "nope"])
        self.assertIn("demo_sales.stores", text)
        self.assertNotIn("demo_sales.sales", text)

    def test_empty_filter_renders_nothing(self):
        self.assertEqual(render_schema_text(self._snapshot(), tables=[]), "")

    def test_value_hints_are_appended(self):
        hints = {"stores": {"region": ["関西", "近畿", "関東"]}}
        text = render_schema_text(self._snapshot(), tables=["stores"], value_hints=hints)
        self.assertIn("値の例: 関西, 近畿, 関東", text)

    def test_value_hint_is_combined_with_comment(self):
        hints = {"stores": {"region": ["関西"]}}
        text = render_schema_text(self._snapshot(), tables=["stores"], value_hints=hints)
        self.assertIn("-- 広域区分 / 値の例: 関西", text)

    def test_value_hint_without_comments_stands_alone(self):
        hints = {"stores": {"region": ["関西"]}}
        text = render_schema_text(
            self._snapshot(), tables=["stores"], include_comments=False, value_hints=hints
        )
        self.assertIn("-- 値の例: 関西", text)
        self.assertNotIn("広域区分", text)

    def test_foreign_key_line(self):
        text = render_schema_text(self._snapshot(), tables=["sales"])
        self.assertIn(
            "FOREIGN KEY (store_id) REFERENCES demo_sales.stores (store_id)", text
        )

    def test_tables_are_separated_by_blank_line(self):
        text = render_schema_text(self._snapshot())
        self.assertIn(");\n\n", text)

    def test_empty_snapshot(self):
        self.assertEqual(render_schema_text(SchemaSnapshot(schema="x")), "")
