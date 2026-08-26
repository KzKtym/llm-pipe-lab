"""nl2sql/params。実験パラメータの正規化と検証。

画面のテキスト欄から来る文字列を型に落とす部分と、
未実装のパラメータで落ちることを確かめる。
"""
from datetime import date

from django.test import SimpleTestCase

from app.nl2sql.params import Nl2SqlParams, UnsupportedParameter


class NormalizeTests(SimpleTestCase):
    def test_strips_quotes(self):
        self.assertEqual(Nl2SqlParams.normalize({'"model"': "'gpt-4.1-mini'"}), {"model": "gpt-4.1-mini"})

    def test_integers(self):
        self.assertEqual(Nl2SqlParams.normalize({"few_shot": "3"})["few_shot"], 3)

    def test_negative_integers(self):
        self.assertEqual(Nl2SqlParams.normalize({"retry_k": "-1"})["retry_k"], -1)

    def test_floats(self):
        self.assertEqual(Nl2SqlParams.normalize({"temperature": "0.7"})["temperature"], 0.7)

    def test_booleans(self):
        normalized = Nl2SqlParams.normalize({"self_correct": "true", "value_hint": "off"})
        self.assertIs(normalized["self_correct"], True)
        self.assertIs(normalized["value_hint"], False)

    def test_none_stays_text_until_the_type_is_known(self):
        """`schema_desc: none` の none は値そのもの。文字列段階では潰さない。"""
        self.assertEqual(Nl2SqlParams.normalize({"schema_k": "none"})["schema_k"], "none")

    def test_zero_and_one_are_integers_not_booleans(self):
        normalized = Nl2SqlParams.normalize({"retry_k": "0", "few_shot": "1"})
        self.assertEqual(normalized["retry_k"], 0)
        self.assertNotIsInstance(normalized["retry_k"], bool)
        self.assertEqual(normalized["few_shot"], 1)

    def test_non_string_values_pass_through(self):
        self.assertEqual(Nl2SqlParams.normalize({"few_shot": 5})["few_shot"], 5)


class FromDictTests(SimpleTestCase):
    def test_defaults(self):
        params = Nl2SqlParams.from_dict({})
        self.assertEqual(params.schema, "demo_sales")
        self.assertEqual(params.schema_mode, "full")
        self.assertEqual(params.few_shot, 0)
        self.assertFalse(params.self_correct)

    def test_unknown_keys_are_ignored(self):
        params = Nl2SqlParams.from_dict({"nonexistent": "x", "few_shot": 2})
        self.assertEqual(params.few_shot, 2)

    def test_none_values_fall_back_to_defaults(self):
        self.assertEqual(Nl2SqlParams.from_dict({"max_rows": None}).max_rows, 1000)

    def test_none_text_clears_a_nullable_field(self):
        self.assertIsNone(Nl2SqlParams.from_dict({"schema_k": "none"}).schema_k)

    def test_none_text_is_kept_for_a_non_nullable_field(self):
        """schema_desc の none は「コメントを載せない」という値。未指定ではない。"""
        self.assertEqual(Nl2SqlParams.from_dict({"schema_desc": "none"}).schema_desc, "none")

    def test_integer_is_coerced_for_boolean_fields(self):
        self.assertIs(Nl2SqlParams.from_dict({"self_correct": 1}).self_correct, True)
        self.assertIs(Nl2SqlParams.from_dict({"self_correct": 0}).self_correct, False)

    def test_reference_date_is_parsed(self):
        params = Nl2SqlParams.from_dict({"reference_date": "2026-08-14"})
        self.assertEqual(params.reference_date_value, date(2026, 8, 14))

    def test_include_comments_follows_schema_desc(self):
        self.assertTrue(Nl2SqlParams.from_dict({}).include_comments)
        self.assertFalse(Nl2SqlParams.from_dict({"schema_desc": "none"}).include_comments)

    def test_hint_drops_comments_and_turns_on_hints(self):
        params = Nl2SqlParams.from_dict({"schema_desc": "hint"})
        self.assertFalse(params.include_comments)
        self.assertTrue(params.uses_hints)

    def test_comment_does_not_use_hints(self):
        self.assertFalse(Nl2SqlParams.from_dict({"schema_desc": "comment"}).uses_hints)


class ValidationTests(SimpleTestCase):
    def test_invalid_schema_mode(self):
        with self.assertRaises(ValueError):
            Nl2SqlParams.from_dict({"schema_mode": "auto"})

    def test_invalid_schema_desc(self):
        with self.assertRaises(ValueError):
            Nl2SqlParams.from_dict({"schema_desc": "verbose"})

    def test_negative_retry_k(self):
        with self.assertRaises(ValueError):
            Nl2SqlParams.from_dict({"retry_k": -1})

    def test_unimplemented_schema_mode_raises(self):
        """黙って無視すると、記録上のパラメータと実際の処理がずれる。"""
        with self.assertRaises(UnsupportedParameter):
            Nl2SqlParams.from_dict({"schema_mode": "retrieved"})

    def test_unimplemented_value_hint_raises(self):
        with self.assertRaises(UnsupportedParameter):
            Nl2SqlParams.from_dict({"value_hint": True})


class SummaryLineTests(SimpleTestCase):
    def test_contains_main_parameters(self):
        line = Nl2SqlParams.from_dict({"few_shot": 3}).summary_line()
        self.assertIn("few_shot: 3", line)
        self.assertIn("schema_mode: full", line)
        self.assertIn("self_correct: false", line)

    def test_retry_k_only_when_self_correct(self):
        without = Nl2SqlParams.from_dict({}).summary_line()
        with_correct = Nl2SqlParams.from_dict({"self_correct": True, "retry_k": 2}).summary_line()
        self.assertNotIn("retry_k", without)
        self.assertIn("retry_k: 2", with_correct)

    def test_to_dict_round_trip(self):
        params = Nl2SqlParams.from_dict({"few_shot": 2, "self_correct": True})
        self.assertEqual(Nl2SqlParams.from_dict(params.to_dict()).to_dict(), params.to_dict())
