"""nl2sql/core/generator。プロンプトの組み立てとSQLの取り出し。

LLMは FakeLLMClient に差し替えるためAPIは叩かない。
取り出し（extract_sql）は実際の失敗が集まる場所なので、返され方のパターンを厚めに試す。
"""
from datetime import date

from django.test import SimpleTestCase

from app.common.llm_client import FakeLLMClient, LLMFatalError
from app.nl2sql.core.generator import (
    FewShotExample,
    GenerationResult,
    SqlGenerator,
    build_prompt,
    extract_sql,
)

SCHEMA = "CREATE TABLE demo_sales.stores (\n  store_id integer NOT NULL\n);"


class ExtractSqlTests(SimpleTestCase):
    def test_bare_sql(self):
        self.assertEqual(extract_sql("SELECT 1"), "SELECT 1")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(extract_sql("\n  SELECT 1  \n"), "SELECT 1")

    def test_sql_fence(self):
        self.assertEqual(extract_sql("```sql\nSELECT 1\n```"), "SELECT 1")

    def test_bare_fence(self):
        self.assertEqual(extract_sql("```\nSELECT 1\n```"), "SELECT 1")

    def test_fence_with_preamble(self):
        text = "以下のSQLで取得できます。\n\n```sql\nSELECT 1\n```\n\nご確認ください。"
        self.assertEqual(extract_sql(text), "SELECT 1")

    def test_first_fence_wins(self):
        text = "```sql\nSELECT 1\n```\n別案:\n```sql\nSELECT 2\n```"
        self.assertEqual(extract_sql(text), "SELECT 1")

    def test_multiline_sql_in_fence(self):
        text = "```sql\nSELECT store_id\nFROM stores\nORDER BY store_id\n```"
        self.assertEqual(extract_sql(text), "SELECT store_id\nFROM stores\nORDER BY store_id")

    def test_preamble_without_fence(self):
        text = "このクエリで取得できます。SELECT count(*) FROM stores"
        self.assertEqual(extract_sql(text), "SELECT count(*) FROM stores")

    def test_with_clause_is_recognised(self):
        text = "説明です。\nWITH x AS (SELECT 1) SELECT * FROM x"
        self.assertEqual(extract_sql(text), "WITH x AS (SELECT 1) SELECT * FROM x")

    def test_no_sql_returns_stripped_text(self):
        self.assertEqual(extract_sql("  わかりません  "), "わかりません")

    def test_empty(self):
        self.assertEqual(extract_sql(""), "")


class BuildPromptTests(SimpleTestCase):
    def test_contains_question_and_schema(self):
        prompt = build_prompt("店舗数は？", SCHEMA)
        self.assertIn("店舗数は？", prompt)
        self.assertIn("demo_sales.stores", prompt)

    def test_contains_rules(self):
        prompt = build_prompt("q", SCHEMA)
        self.assertIn("SELECT 文のみ", prompt)
        self.assertIn("列のコメントを必ず読む", prompt)

    def test_dialect_is_stated(self):
        self.assertIn("PostgreSQL", build_prompt("q", SCHEMA))
        self.assertIn("SQL Server", build_prompt("q", SCHEMA, dialect="SQL Server"))

    def test_reference_date_is_omitted_by_default(self):
        self.assertNotIn("# 基準日", build_prompt("q", SCHEMA))

    def test_reference_date_is_included_when_given(self):
        prompt = build_prompt("q", SCHEMA, reference_date=date(2026, 8, 14))
        self.assertIn("# 基準日", prompt)
        self.assertIn("2026-08-14", prompt)

    def test_few_shot_section_is_omitted_when_empty(self):
        self.assertNotIn("# 例", build_prompt("q", SCHEMA))

    def test_few_shot_examples_are_rendered(self):
        examples = (
            FewShotExample("店舗数は？", "SELECT count(*) FROM stores"),
            FewShotExample("商品数は？", "SELECT count(*) FROM products"),
        )
        prompt = build_prompt("q", SCHEMA, few_shot=examples)
        self.assertIn("# 例", prompt)
        self.assertIn("質問: 店舗数は？", prompt)
        self.assertIn("SQL: SELECT count(*) FROM products", prompt)

    def test_error_feedback_is_omitted_by_default(self):
        self.assertNotIn("# 直前の試行", build_prompt("q", SCHEMA))

    def test_error_feedback_is_included(self):
        prompt = build_prompt(
            "q", SCHEMA, previous_sql="SELECT bad FROM stores", error='column "bad" does not exist'
        )
        self.assertIn("# 直前の試行", prompt)
        self.assertIn("SELECT bad FROM stores", prompt)
        self.assertIn('column "bad" does not exist', prompt)

    def test_error_feedback_comes_after_question(self):
        """直前の失敗は質問より後に置く。最後に読ませたい情報のため。"""
        prompt = build_prompt("質問文", SCHEMA, error="boom")
        self.assertLess(prompt.index("質問文"), prompt.index("# 直前の試行"))

    def test_hints_are_omitted_by_default(self):
        self.assertNotIn("# 注意", build_prompt("q", SCHEMA))

    def test_hints_are_included(self):
        prompt = build_prompt("q", SCHEMA, hints="- region には表記ゆれがある")
        self.assertIn("# 注意（このデータ固有）", prompt)
        self.assertIn("region には表記ゆれがある", prompt)

    def test_hints_come_after_schema_and_before_question(self):
        """長い説明の中に埋もれないよう、質問の直前に置く。"""
        prompt = build_prompt("質問文", SCHEMA, hints="注意事項")
        self.assertLess(prompt.index("# スキーマ"), prompt.index("# 注意"))
        self.assertLess(prompt.index("# 注意"), prompt.index("質問文"))

    def test_ends_with_sql_header(self):
        self.assertTrue(build_prompt("q", SCHEMA).endswith("# SQL"))


class SqlGeneratorTests(SimpleTestCase):
    def _generator(self, responses, **kwargs):
        client = FakeLLMClient(model="fake", responses=responses)
        return SqlGenerator(client, schema_text=SCHEMA, **kwargs), client

    def test_generate_extracts_sql(self):
        generator, _ = self._generator(["```sql\nSELECT count(*) FROM stores\n```"])
        result = generator.generate("店舗数は？")
        self.assertEqual(result.sql, "SELECT count(*) FROM stores")

    def test_generate_keeps_raw_output(self):
        raw = "以下です。\n```sql\nSELECT 1\n```"
        generator, _ = self._generator([raw])
        self.assertEqual(generator.generate("q").raw, raw)

    def test_generate_records_prompt(self):
        generator, client = self._generator(["SELECT 1"])
        result = generator.generate("店舗数は？")
        self.assertIn("店舗数は？", result.prompt)
        # 実際に送ったものと一致していること
        self.assertEqual(result.prompt, client.calls[0])

    def test_generate_records_model(self):
        generator, _ = self._generator(["SELECT 1"])
        self.assertEqual(generator.generate("q").model, "fake")

    def test_hints_reach_the_prompt(self):
        generator, _ = self._generator(["SELECT 1"], hints="- 注意事項")
        self.assertIn("- 注意事項", generator.generate("q").prompt)

    def test_reference_date_reaches_the_prompt(self):
        generator, _ = self._generator(["SELECT 1"], reference_date=date(2026, 8, 14))
        self.assertIn("2026-08-14", generator.generate("q").prompt)

    def test_error_feedback_reaches_the_prompt(self):
        generator, _ = self._generator(["SELECT 1"])
        result = generator.generate("q", previous_sql="SELECT bad", error="boom")
        self.assertIn("SELECT bad", result.prompt)
        self.assertIn("boom", result.prompt)

    def test_second_call_uses_fresh_prompt(self):
        generator, client = self._generator(["SELECT 1", "SELECT 2"])
        generator.generate("最初の質問")
        second = generator.generate("次の質問")
        self.assertEqual(second.sql, "SELECT 2")
        self.assertIn("次の質問", client.calls[1])
        self.assertNotIn("最初の質問", client.calls[1])

    def test_llm_errors_propagate(self):
        class Boom(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                raise LLMFatalError("認証エラー")

        generator = SqlGenerator(Boom(), schema_text=SCHEMA)
        with self.assertRaises(LLMFatalError):
            generator.generate("q")


class GenerationResultTests(SimpleTestCase):
    def test_total_tokens(self):
        result = GenerationResult(sql="SELECT 1", prompt_tokens=100, completion_tokens=20)
        self.assertEqual(result.total_tokens, 120)

    def test_defaults(self):
        result = GenerationResult(sql="SELECT 1")
        self.assertEqual(result.raw, "")
        self.assertEqual(result.total_tokens, 0)
