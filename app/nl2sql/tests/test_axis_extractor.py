"""nl2sql/core/axis_extractor。段階2・工程①（構造化）。

DBもAPIも使わない。出力契約のパースと、LLMの例外がそのまま伝わることを確かめる。
`issue_c015` で追加した `GROUP_BY_GRANULARITY` / `LIMITED` / `PERIOD`、
`issue_c024` で追加した `REGION_FILTER` を含む。

`issue_c034`：`FLAG_STORES`/`FLAG_MEMBERS`/`FLAG_PRODUCTS` を本体（`_PROMPT`、9行）
から切り離し、別の呼び出し（`_FLAG_PROMPT`、3行）にした。`AxisExtractor.extract()`は
本体→`FLAG_*`の順に2回呼ぶ。`AxisExtraction`のフィールド・値域は変わっていない。

`issue_c028`：本体に`RATE_DENOMINATOR`を追加した（10行）。
"""
from unittest import mock

from django.test import SimpleTestCase

from app.common.llm_client import FakeLLMClient, LLMFatalError, LLMTransientError
from app.nl2sql.core import axis_extractor as axis_extractor_module
from app.nl2sql.core.axis_extractor import (
    AxisExtractor,
    build_flag_prompt,
    build_prompt,
    flag_prompt_digest,
    parse_flag_extraction,
    parse_main_extraction,
    prompt_digest,
)

MAIN_RESPONSE = """\
AGGREGATION: EVENT
GROUP_BY: MASTER_STORES
GROUP_BY_GRANULARITY: IDENTITY
LIMITED: NO
GROUP_BY_RESOLVED: UNRESOLVED
SYNONYM: NONE
CATCHALL: NONE
PERIOD: 2026-07-01..2026-08-01
REGION_FILTER: NONE
RATE_DENOMINATOR: NONE"""

FLAG_RESPONSE = """\
FLAG_STORES: UNRESOLVED
FLAG_MEMBERS: NONE
FLAG_PRODUCTS: NONE"""


class ParseMainExtractionTests(SimpleTestCase):
    def test_full_response(self):
        fields = parse_main_extraction(MAIN_RESPONSE)
        self.assertEqual(fields["aggregation"], "EVENT")
        self.assertEqual(fields["group_by"], "MASTER_STORES")
        self.assertEqual(fields["group_by_granularity"], "IDENTITY")
        self.assertEqual(fields["limited"], "NO")
        self.assertEqual(fields["group_by_resolved"], "UNRESOLVED")
        self.assertEqual(fields["synonym"], "NONE")
        self.assertEqual(fields["catchall"], "NONE")
        self.assertEqual(fields["period"], "2026-07-01..2026-08-01")
        self.assertEqual(fields["region_filter"], "NONE")
        self.assertEqual(fields["rate_denominator"], "NONE")
        self.assertNotIn("flag_stores", fields)

    def test_entity_aggregation(self):
        text = MAIN_RESPONSE.replace("AGGREGATION: EVENT", "AGGREGATION: ENTITY")
        fields = parse_main_extraction(text)
        self.assertEqual(fields["aggregation"], "ENTITY")

    def test_classification_granularity(self):
        text = MAIN_RESPONSE.replace(
            "GROUP_BY_GRANULARITY: IDENTITY", "GROUP_BY_GRANULARITY: CLASSIFICATION"
        )
        fields = parse_main_extraction(text)
        self.assertEqual(fields["group_by_granularity"], "CLASSIFICATION")

    def test_granularity_na(self):
        text = MAIN_RESPONSE.replace("GROUP_BY: MASTER_STORES", "GROUP_BY: FACT_COLUMN").replace(
            "GROUP_BY_GRANULARITY: IDENTITY", "GROUP_BY_GRANULARITY: NA"
        ).replace("GROUP_BY_RESOLVED: UNRESOLVED", "GROUP_BY_RESOLVED: NA")
        fields = parse_main_extraction(text)
        self.assertEqual(fields["group_by_granularity"], "NA")

    def test_limited_yes(self):
        text = MAIN_RESPONSE.replace("LIMITED: NO", "LIMITED: YES")
        fields = parse_main_extraction(text)
        self.assertEqual(fields["limited"], "YES")

    def test_period_none(self):
        text = MAIN_RESPONSE.replace("PERIOD: 2026-07-01..2026-08-01", "PERIOD: NONE")
        fields = parse_main_extraction(text)
        self.assertEqual(fields["period"], "NONE")

    def test_region_filter_named_value(self):
        text = MAIN_RESPONSE.replace("REGION_FILTER: NONE", "REGION_FILTER: 関西")
        fields = parse_main_extraction(text)
        self.assertEqual(fields["region_filter"], "関西")

    def test_missing_region_filter_line_raises(self):
        text = MAIN_RESPONSE.replace("\nREGION_FILTER: NONE", "")
        with self.assertRaises(LLMTransientError):
            parse_main_extraction(text)

    def test_region_filter_unknown_value_raises(self):
        text = MAIN_RESPONSE.replace("REGION_FILTER: NONE", "REGION_FILTER: 沖縄")
        with self.assertRaises(LLMTransientError):
            parse_main_extraction(text)

    def test_synonym_and_catchall_normalized(self):
        text = MAIN_RESPONSE.replace("SYNONYM: NONE", "SYNONYM: region_unresolved").replace(
            "CATCHALL: NONE", "CATCHALL: gender_resolved"
        )
        fields = parse_main_extraction(text)
        self.assertEqual(fields["synonym"], "region_UNRESOLVED")
        self.assertEqual(fields["catchall"], "gender_RESOLVED")

    def test_group_by_na(self):
        text = MAIN_RESPONSE.replace("GROUP_BY: MASTER_STORES", "GROUP_BY: NONE").replace(
            "GROUP_BY_GRANULARITY: IDENTITY", "GROUP_BY_GRANULARITY: NA"
        ).replace("GROUP_BY_RESOLVED: UNRESOLVED", "GROUP_BY_RESOLVED: NA")
        fields = parse_main_extraction(text)
        self.assertEqual(fields["group_by"], "NONE")
        self.assertEqual(fields["group_by_resolved"], "NA")

    def test_missing_line_raises(self):
        text = MAIN_RESPONSE.replace("AGGREGATION: EVENT\n", "")
        with self.assertRaises(LLMTransientError):
            parse_main_extraction(text)

    def test_missing_period_line_raises(self):
        text = MAIN_RESPONSE.replace("\nPERIOD: 2026-07-01..2026-08-01", "")
        with self.assertRaises(LLMTransientError):
            parse_main_extraction(text)

    def test_garbage_raises(self):
        with self.assertRaises(LLMTransientError):
            parse_main_extraction("よくわかりません")

    def test_case_insensitive(self):
        fields = parse_main_extraction(MAIN_RESPONSE.lower())
        self.assertEqual(fields["aggregation"], "EVENT")
        self.assertEqual(fields["group_by"], "MASTER_STORES")

    def test_group_by_resolved_line_not_confused_with_group_by_line(self):
        """GROUP_BY: と GROUP_BY_RESOLVED: の2行が別々に正しく拾えること。"""
        fields = parse_main_extraction(MAIN_RESPONSE)
        self.assertEqual(fields["group_by"], "MASTER_STORES")
        self.assertEqual(fields["group_by_resolved"], "UNRESOLVED")

    def test_rate_denominator_named_value(self):
        """`issue_c028`：`RATE_DENOMINATOR`のパース。"""
        text = MAIN_RESPONSE.replace("RATE_DENOMINATOR: NONE", "RATE_DENOMINATOR: MASTER_MEMBERS")
        fields = parse_main_extraction(text)
        self.assertEqual(fields["rate_denominator"], "MASTER_MEMBERS")

    def test_missing_rate_denominator_line_raises(self):
        text = MAIN_RESPONSE.replace("\nRATE_DENOMINATOR: NONE", "")
        with self.assertRaises(LLMTransientError):
            parse_main_extraction(text)

    def test_group_by_granularity_line_not_confused_with_group_by_line(self):
        fields = parse_main_extraction(MAIN_RESPONSE)
        self.assertEqual(fields["group_by"], "MASTER_STORES")
        self.assertEqual(fields["group_by_granularity"], "IDENTITY")


class ParseFlagExtractionTests(SimpleTestCase):
    """`issue_c034`：`FLAG_*`用（3行）の単独パース。"""

    def test_full_response(self):
        fields = parse_flag_extraction(FLAG_RESPONSE)
        self.assertEqual(fields["flag_stores"], "UNRESOLVED")
        self.assertEqual(fields["flag_members"], "NONE")
        self.assertEqual(fields["flag_products"], "NONE")

    def test_missing_line_raises(self):
        text = FLAG_RESPONSE.replace("\nFLAG_MEMBERS: NONE", "")
        with self.assertRaises(LLMTransientError):
            parse_flag_extraction(text)

    def test_garbage_raises(self):
        with self.assertRaises(LLMTransientError):
            parse_flag_extraction("よくわかりません")

    def test_case_insensitive(self):
        fields = parse_flag_extraction(FLAG_RESPONSE.lower())
        self.assertEqual(fields["flag_stores"], "UNRESOLVED")


class BuildPromptTests(SimpleTestCase):
    def test_contains_question(self):
        prompt = build_prompt("会員ごとの購入回数、それぞれ何回?")
        self.assertIn("会員ごとの購入回数、それぞれ何回?", prompt)

    def test_no_examples_from_the_ten_questions(self):
        """段階2の10件のどれも例示しない（issue_c009 確定事項dの精神を踏襲）。"""
        prompt = build_prompt("質問")
        self.assertNotIn("廃番の商品も入れて", prompt)
        self.assertNotIn("退会した人も入れて", prompt)

    def test_mentions_schema_hints(self):
        prompt = build_prompt("質問")
        self.assertIn("stores", prompt)
        self.assertIn("close_date", prompt)

    def test_mentions_c015_general_rules(self):
        """issue_c015で足した一般則の要点がプロンプトに含まれること。"""
        prompt = build_prompt("質問")
        self.assertIn("GROUP_BY_GRANULARITY", prompt)
        self.assertIn("LIMITED", prompt)
        self.assertIn("PERIOD", prompt)
        self.assertIn("基準日", prompt)

    def test_flag_fields_not_in_main_prompt(self):
        """`issue_c034`：`FLAG_*`は本体から切り離されている。"""
        prompt = build_prompt("質問")
        self.assertNotIn("FLAG_STORES", prompt)
        self.assertNotIn("FLAG_MEMBERS", prompt)
        self.assertNotIn("FLAG_PRODUCTS", prompt)

    def test_mentions_rate_denominator(self):
        """`issue_c028`：平均・比率の分母を判定する項目が含まれること。"""
        prompt = build_prompt("質問")
        self.assertIn("RATE_DENOMINATOR", prompt)
        self.assertIn("1人あたり", prompt)


class BuildFlagPromptTests(SimpleTestCase):
    """`issue_c034`：`FLAG_*`用の別プロンプト。"""

    def _build(self, **overrides):
        kwargs = {
            "aggregation": "EVENT",
            "group_by": "MASTER_STORES",
            "group_by_granularity": "IDENTITY",
        }
        kwargs.update(overrides)
        return build_flag_prompt("店舗名ごとの7月の平均日商", **kwargs)

    def test_contains_question(self):
        self.assertIn("店舗名ごとの7月の平均日商", self._build())

    def test_mentions_schema_hints(self):
        prompt = self._build()
        self.assertIn("stores", prompt)
        self.assertIn("close_date", prompt)

    def test_carries_main_result_as_context(self):
        """本体の抽出結果（GROUP_BY等）が文字どおり渡ること（食い違い防止）。"""
        prompt = self._build(group_by="MASTER_MEMBERS", group_by_granularity="CLASSIFICATION")
        self.assertIn("GROUP_BY: MASTER_MEMBERS", prompt)
        self.assertIn("GROUP_BY_GRANULARITY: CLASSIFICATION", prompt)

    def test_other_main_only_fields_not_in_flag_prompt(self):
        """`SYNONYM`・`CATCHALL`・`REGION_FILTER`・`RATE_DENOMINATOR`は本体側の
        判定であり、`FLAG_*`用には含めない（`issue_c028`は`_FLAG_PROMPT`に触れて
        いない）。"""
        prompt = self._build()
        self.assertNotIn("SYNONYM", prompt)
        self.assertNotIn("CATCHALL", prompt)
        self.assertNotIn("REGION_FILTER", prompt)
        self.assertNotIn("RATE_DENOMINATOR", prompt)


class PromptDigestTests(SimpleTestCase):
    """`issue_c031`：`_PROMPT`（本体）の版を実験記録に残すためのハッシュ。"""

    def test_is_a_sha256_hex_digest(self):
        digest = prompt_digest()
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # 16進数として読めること

    def test_stable_across_calls(self):
        self.assertEqual(prompt_digest(), prompt_digest())

    def test_changes_when_prompt_text_changes(self):
        """プロンプトを1文字変えるとハッシュが変わること。"""
        original = prompt_digest()
        changed_text = axis_extractor_module._PROMPT + "x"
        with mock.patch.object(axis_extractor_module, "_PROMPT", changed_text):
            changed = prompt_digest()
        self.assertNotEqual(original, changed)


class FlagPromptDigestTests(SimpleTestCase):
    """`issue_c034`：`_FLAG_PROMPT`の版を実験記録に残すためのハッシュ。"""

    def test_is_a_sha256_hex_digest(self):
        digest = flag_prompt_digest()
        self.assertEqual(len(digest), 64)
        int(digest, 16)

    def test_stable_across_calls(self):
        self.assertEqual(flag_prompt_digest(), flag_prompt_digest())

    def test_changes_when_prompt_text_changes(self):
        original = flag_prompt_digest()
        changed_text = axis_extractor_module._FLAG_PROMPT + "x"
        with mock.patch.object(axis_extractor_module, "_FLAG_PROMPT", changed_text):
            changed = flag_prompt_digest()
        self.assertNotEqual(original, changed)

    def test_independent_of_main_prompt_digest(self):
        """本体と`FLAG_*`は別のプロンプトであり、別の値であること。"""
        self.assertNotEqual(prompt_digest(), flag_prompt_digest())


class AxisExtractorTests(SimpleTestCase):
    def test_extract_success(self):
        """`issue_c034`：本体→`FLAG_*`の順に2回呼び、1つの`AxisExtraction`にまとめる。"""
        client = FakeLLMClient(responses=[MAIN_RESPONSE, FLAG_RESPONSE])
        extractor = AxisExtractor(client)
        extraction = extractor.extract("店舗名ごとの7月の平均日商")

        self.assertEqual(extraction.aggregation, "EVENT")
        self.assertEqual(extraction.group_by, "MASTER_STORES")
        self.assertEqual(extraction.group_by_granularity, "IDENTITY")
        self.assertEqual(extraction.limited, "NO")
        self.assertEqual(extraction.period, "2026-07-01..2026-08-01")
        self.assertEqual(extraction.flag_stores, "UNRESOLVED")
        self.assertEqual(extraction.flag_members, "NONE")
        self.assertEqual(extraction.flag_products, "NONE")
        self.assertIn("店舗名ごとの7月の平均日商", extraction.prompt)
        self.assertEqual(len(client.calls), 2)

    def test_flag_call_receives_main_result(self):
        """2回目（`FLAG_*`）の呼び出しに、1回目の`GROUP_BY`が渡っていること。"""
        text = MAIN_RESPONSE.replace("GROUP_BY: MASTER_STORES", "GROUP_BY: MASTER_MEMBERS")
        client = FakeLLMClient(responses=[text, FLAG_RESPONSE])
        AxisExtractor(client).extract("会員ランク別の会員数")
        self.assertEqual(len(client.calls), 2)
        self.assertIn("GROUP_BY: MASTER_MEMBERS", client.calls[1])

    def test_parse_failure_propagates(self):
        extractor = AxisExtractor(FakeLLMClient(responses=["さあ？"]))
        with self.assertRaises(LLMTransientError):
            extractor.extract("q")

    def test_fatal_error_propagates(self):
        class Dead(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                raise LLMFatalError("401")

        extractor = AxisExtractor(Dead())
        with self.assertRaises(LLMFatalError):
            extractor.extract("q")

    def test_tokens_are_carried(self):
        """2回分のトークン・所要時間が合算されること。"""

        class CountingClient(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                response = super().complete(prompt, **kwargs)
                response.prompt_tokens = 40
                response.completion_tokens = 8
                return response

        extractor = AxisExtractor(CountingClient(responses=[MAIN_RESPONSE, FLAG_RESPONSE]))
        extraction = extractor.extract("q")
        self.assertEqual(extraction.prompt_tokens, 80)
        self.assertEqual(extraction.completion_tokens, 16)
        self.assertEqual(extraction.total_tokens, 96)
