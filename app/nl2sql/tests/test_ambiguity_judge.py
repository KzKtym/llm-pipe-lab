"""nl2sql/core/ambiguity_judge。段階2の判断ステップ（SQLは扱わない）。

DBもAPIも使わない。出力契約のパースと、LLMの例外がそのまま伝わることを確かめる。
"""
from django.test import SimpleTestCase

from app.common.llm_client import FakeLLMClient, LLMFatalError, LLMTransientError
from app.nl2sql.core.ambiguity_judge import (
    _AXIS_DISPLAY_NAME,
    AmbiguityJudge,
    _display_name,
    build_disclosure_prompt,
    build_prompt,
    parse_judgement,
    phrase_disclosure,
)
from app.nl2sql.core.population_rules import (
    _MASTER_GROUP_BY_NAME,
    _TIME_AXIS_NAME,
    FLAG_COLUMN,
    RuleAxis,
    RuleResult,
)


class ParseJudgementTests(SimpleTestCase):
    def test_answer(self):
        judged_ask, clarification = parse_judgement("JUDGEMENT: ANSWER")
        self.assertFalse(judged_ask)
        self.assertEqual(clarification, "")

    def test_clarify(self):
        judged_ask, clarification = parse_judgement(
            "JUDGEMENT: CLARIFY\nQUESTION: 閉店した店舗も含めますか？"
        )
        self.assertTrue(judged_ask)
        self.assertEqual(clarification, "閉店した店舗も含めますか？")

    def test_missing_judgement_line_is_transient_error(self):
        with self.assertRaises(LLMTransientError):
            parse_judgement("わかりません")

    def test_clarify_without_question_is_transient_error(self):
        with self.assertRaises(LLMTransientError):
            parse_judgement("JUDGEMENT: CLARIFY")

    def test_case_insensitive(self):
        judged_ask, _ = parse_judgement("judgement: answer")
        self.assertFalse(judged_ask)

    def test_ignores_surrounding_text(self):
        """前置き・後書きが付いても、JUDGEMENT行があれば拾う。"""
        judged_ask, clarification = parse_judgement(
            "承知しました。\nJUDGEMENT: CLARIFY\nQUESTION: 廃番の商品も含めますか？\n以上です。"
        )
        self.assertTrue(judged_ask)
        self.assertEqual(clarification, "廃番の商品も含めますか？")


class BuildPromptTests(SimpleTestCase):
    def test_contains_question(self):
        prompt = build_prompt("会員ごとの購入回数、それぞれ何回?")
        self.assertIn("会員ごとの購入回数、それぞれ何回?", prompt)

    def test_no_schema_and_no_examples(self):
        """スキーマ説明を渡さず、段階2の10件のどれも例示しない（issue_c009 確定事項d）。"""
        prompt = build_prompt("質問")
        for forbidden in ("CREATE TABLE", "demo_sales", "廃番の商品も入れて"):
            self.assertNotIn(forbidden, prompt)

    def test_scopes_to_population_axis_only(self):
        prompt = build_prompt("質問")
        self.assertIn("母集合", prompt)

    def test_empty_schema_and_hints_reproduce_issue_c009_prompt(self):
        """schema_text/hints を渡さなければ、issue_c009 時点と1文字も変わらない。"""
        with_defaults = build_prompt("質問")
        explicit_empty = build_prompt("質問", schema_text="", hints="")
        self.assertEqual(with_defaults, explicit_empty)

    def test_schema_text_is_inserted_without_changing_instructions(self):
        prompt = build_prompt("質問", schema_text="CREATE TABLE demo_sales.stores (...);")
        self.assertIn("# スキーマ", prompt)
        self.assertIn("CREATE TABLE demo_sales.stores (...);", prompt)
        # 既存の指示文言は残ったまま（確定事項e：追加するだけで変えない）
        self.assertIn("それ以外の曖昧さ（言葉づかいの粗さなど）は判断の対象にしないでください。", prompt)
        self.assertIn("JUDGEMENT: ANSWER", prompt)

    def test_hints_are_inserted_after_schema(self):
        prompt = build_prompt(
            "質問",
            schema_text="CREATE TABLE demo_sales.stores (...);",
            hints="- 取引ゼロの日は行が無い",
        )
        self.assertIn("# 注意（このデータ固有）", prompt)
        self.assertIn("取引ゼロの日は行が無い", prompt)
        self.assertLess(prompt.index("# スキーマ"), prompt.index("# 注意（このデータ固有）"))

    def test_hints_without_schema_text_still_render(self):
        prompt = build_prompt("質問", hints="- ヒントだけ")
        self.assertNotIn("# スキーマ", prompt)
        self.assertIn("# 注意（このデータ固有）", prompt)


class AmbiguityJudgeTests(SimpleTestCase):
    def test_answer_case(self):
        client = FakeLLMClient(responses=["JUDGEMENT: ANSWER"])
        judge = AmbiguityJudge(client)
        result = judge.judge("支払い方法、何が多い？")

        self.assertFalse(result.judged_ask)
        self.assertEqual(result.clarification, "")
        self.assertEqual(result.raw_response, "JUDGEMENT: ANSWER")
        self.assertIn("支払い方法、何が多い？", result.prompt)

    def test_clarify_case(self):
        client = FakeLLMClient(
            responses=["JUDGEMENT: CLARIFY\nQUESTION: 退会した会員も含めますか？"]
        )
        judge = AmbiguityJudge(client)
        result = judge.judge("会員ごとの購入回数、それぞれ何回?")

        self.assertTrue(result.judged_ask)
        self.assertEqual(result.clarification, "退会した会員も含めますか？")

    def test_tokens_and_model_are_carried(self):
        class CountingClient(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                response = super().complete(prompt, **kwargs)
                response.prompt_tokens = 50
                response.completion_tokens = 5
                return response

        judge = AmbiguityJudge(CountingClient(model="fake", responses=["JUDGEMENT: ANSWER"]))
        result = judge.judge("q")

        self.assertEqual(result.prompt_tokens, 50)
        self.assertEqual(result.completion_tokens, 5)
        self.assertEqual(result.total_tokens, 55)
        self.assertEqual(result.model, "fake")

    def test_transient_parse_failure_propagates(self):
        judge = AmbiguityJudge(FakeLLMClient(responses=["さあ？"]))
        with self.assertRaises(LLMTransientError):
            judge.judge("q")

    def test_schema_text_is_fixed_at_construction(self):
        """SqlGenerator と同じく、スキーマは実験の1回を通して固定する。"""
        client = FakeLLMClient(responses=["JUDGEMENT: ANSWER"])
        judge = AmbiguityJudge(client, schema_text="CREATE TABLE demo_sales.stores (...);")
        judge.judge("質問1")
        judge.judge("質問2")

        for prompt in client.calls:
            self.assertIn("CREATE TABLE demo_sales.stores (...);", prompt)

    def test_fatal_llm_error_propagates(self):
        class Dead(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                raise LLMFatalError("401")

        judge = AmbiguityJudge(Dead())
        with self.assertRaises(LLMFatalError):
            judge.judge("q")


class AxisDisplayNameTests(SimpleTestCase):
    """issue_c018: `RuleAxis.name`（監査用の識別子）を業務の言葉へ変換する対応表。

    対象になりうる name は `population_rules.py` の列挙で尽くされている（有限）。
    列挙が増えたときに変換表が追随しているかを、実際の定数と突き合わせて確かめる。
    """

    def test_covers_every_flag_column_name(self):
        for table, column in FLAG_COLUMN.items():
            name = f"{table}.{column}"
            self.assertIn(name, _AXIS_DISPLAY_NAME, f"{name} が変換表に無い")

    def test_covers_every_master_group_by_name(self):
        for name in _MASTER_GROUP_BY_NAME.values():
            self.assertIn(name, _AXIS_DISPLAY_NAME, f"{name} が変換表に無い")

    def test_covers_time_axis_name(self):
        self.assertIn(_TIME_AXIS_NAME, _AXIS_DISPLAY_NAME)

    def test_covers_synonym_and_catchall_columns(self):
        # axis_extractor.py の SYNONYM(region/channel) / CATCHALL(pref/gender) から
        # population_rules.classify() が組み立てる名前（列挙が小さいためここで固定）
        for name in (
            "regionの表記ゆれ",
            "channelの表記ゆれ",
            "prefの包括値",
            "genderの包括値",
        ):
            self.assertIn(name, _AXIS_DISPLAY_NAME, f"{name} が変換表に無い")

    def test_no_display_value_leaks_an_identifier(self):
        for name, display in _AXIS_DISPLAY_NAME.items():
            self.assertNotIn(".", display, f"{name} の表示名 {display!r} にドットが残っている")
            for jargon in ("結合欠落", "表記ゆれ", "包括値"):
                self.assertNotIn(jargon, display, f"{name} の表示名 {display!r} に内部語が残っている")

    def test_unknown_name_passes_through(self):
        self.assertEqual(_display_name("未登録の軸"), "未登録の軸")


class BuildDisclosurePromptTests(SimpleTestCase):
    def test_lists_defaults_and_clarifications(self):
        result = RuleResult(axes=[
            RuleAxis(name="stores.close_date", kind=2, resolved=False, default="全部含める"),
            RuleAxis(name="店舗の母集合（結合欠落）", kind=1, resolved=False),
        ])
        prompt = build_disclosure_prompt("7月の平均日商、店舗名ごとに", result)

        self.assertIn("全部含める", prompt)
        self.assertIn("7月の平均日商、店舗名ごとに", prompt)
        # issue_c018: 監査用の識別子（列名・内部の分類語）は業務の言葉へ変換して渡し、
        # コード上の name をそのまま②へ渡さない
        self.assertNotIn("stores.close_date", prompt)
        self.assertNotIn("店舗の母集合（結合欠落）", prompt)
        self.assertIn("閉店した店舗", prompt)
        self.assertIn("売上の無い店舗を並べるかどうか", prompt)

    def test_no_defaults_no_clarification_shows_none(self):
        prompt = build_disclosure_prompt("q", RuleResult(axes=[]))
        self.assertIn("（無し）", prompt)

    def test_resolved_axes_are_not_listed_as_defaults_or_clarification(self):
        result = RuleResult(axes=[
            RuleAxis(name="stores.close_date", kind=2, resolved=True, default=""),
        ])
        prompt = build_disclosure_prompt("q", result)
        # 確定済みの軸は開示対象にも確認対象にも出さない
        self.assertNotIn("閉店した店舗", prompt)

    def test_unknown_axis_name_falls_back_to_itself(self):
        # population_rules.py が将来 name を増やしても、変換表に無ければ素通しで壊れない
        result = RuleResult(axes=[
            RuleAxis(name="未知の軸", kind=3, resolved=False, default="統合する"),
        ])
        prompt = build_disclosure_prompt("q", result)
        self.assertIn("未知の軸", prompt)


class PhraseDisclosureTests(SimpleTestCase):
    def test_skips_llm_call_when_nothing_to_say(self):
        client = FakeLLMClient(responses=["呼ばれたら困る"])
        result = phrase_disclosure(client, "q", RuleResult(axes=[]))
        self.assertEqual(result.text, "")
        self.assertEqual(client.calls, [])

    def test_calls_llm_when_default_applied(self):
        client = FakeLLMClient(responses=["現役の店舗のみを対象に集計しました。"])
        result = RuleResult(axes=[
            RuleAxis(name="stores.close_date", kind=2, resolved=False, default="現役のみ"),
        ])
        phrasing = phrase_disclosure(client, "店舗数は？", result)

        self.assertEqual(phrasing.text, "現役の店舗のみを対象に集計しました。")
        self.assertEqual(len(client.calls), 1)

    def test_empty_llm_response_is_transient_error(self):
        client = FakeLLMClient(responses=[""])
        result = RuleResult(axes=[RuleAxis(name="x", kind=3, resolved=False, default="統合する")])
        with self.assertRaises(LLMTransientError):
            phrase_disclosure(client, "q", result)

    def test_fatal_error_propagates(self):
        class Dead(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                raise LLMFatalError("429")

        result = RuleResult(axes=[RuleAxis(name="x", kind=1, resolved=False)])
        with self.assertRaises(LLMFatalError):
            phrase_disclosure(Dead(), "q", result)

    def test_tokens_carried(self):
        class CountingClient(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                response = super().complete(prompt, **kwargs)
                response.prompt_tokens = 30
                response.completion_tokens = 10
                return response

        result = RuleResult(axes=[RuleAxis(name="x", kind=4, resolved=False, default="独立区分")])
        phrasing = phrase_disclosure(CountingClient(responses=["文言"]), "q", result)
        self.assertEqual(phrasing.total_tokens, 40)
