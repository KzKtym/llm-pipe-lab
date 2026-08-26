"""nl2sql/core/stage2_rule_evaluator。①抽出→③判定→②文言化を通しで確かめる。

抽出用クライアントと文言化用クライアントを分けて渡せるので、
`FakeLLMClient` を2本用意して噛み合わせる。
"""
from django.test import SimpleTestCase

from app.common.llm_client import FakeLLMClient, LLMFatalError
from app.nl2sql.core.axis_extractor import AxisExtractor
from app.nl2sql.core.stage2_rule_evaluator import (
    RuleQuestion,
    dataset_digest,
    evaluate,
    format_summary,
)

QUESTIONS = [
    RuleQuestion(id="q1", question="7月の平均日商、店舗名ごとに"),
    RuleQuestion(id="q2", question="店舗形態別の店舗数"),
]

EXTRACTION_TYPE1 = """\
AGGREGATION: EVENT
GROUP_BY: MASTER_STORES
GROUP_BY_GRANULARITY: IDENTITY
LIMITED: NO
GROUP_BY_RESOLVED: UNRESOLVED
FLAG_STORES: NONE
FLAG_MEMBERS: NONE
FLAG_PRODUCTS: NONE
SYNONYM: NONE
CATCHALL: NONE
PERIOD: 2026-07-01..2026-08-01
REGION_FILTER: NONE
RATE_DENOMINATOR: NONE"""

EXTRACTION_NO_RISK = """\
AGGREGATION: ENTITY
GROUP_BY: MASTER_STORES
GROUP_BY_GRANULARITY: CLASSIFICATION
LIMITED: NO
GROUP_BY_RESOLVED: NA
FLAG_STORES: RESOLVED
FLAG_MEMBERS: NONE
FLAG_PRODUCTS: NONE
SYNONYM: NONE
CATCHALL: NONE
PERIOD: NONE
REGION_FILTER: NONE
RATE_DENOMINATOR: NONE"""


class DatasetDigestTests(SimpleTestCase):
    def test_same_content_same_digest(self):
        a = [RuleQuestion(id="q1", question="x")]
        b = [RuleQuestion(id="q1", question="x")]
        self.assertEqual(dataset_digest(a), dataset_digest(b))

    def test_question_change_changes_digest(self):
        a = [RuleQuestion(id="q1", question="x")]
        b = [RuleQuestion(id="q1", question="y")]
        self.assertNotEqual(dataset_digest(a), dataset_digest(b))


class EvaluateTests(SimpleTestCase):
    def test_type1_triggers_clarification_and_phrasing_call(self):
        extractor = AxisExtractor(FakeLLMClient(responses=[EXTRACTION_TYPE1]))
        phrasing_client = FakeLLMClient(responses=["取引の無かった店舗を含めますか？"])

        summary, items = evaluate([QUESTIONS[0]], extractor, phrasing_client)

        self.assertEqual(summary.judged, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.type1_detected, 1)
        self.assertEqual(summary.needs_clarification_count, 1)
        self.assertTrue(items[0].needs_clarification)
        self.assertEqual(items[0].disclosure_text, "取引の無かった店舗を含めますか？")
        self.assertEqual(len(phrasing_client.calls), 1)

    def test_no_risk_question_skips_phrasing_call(self):
        extractor = AxisExtractor(FakeLLMClient(responses=[EXTRACTION_NO_RISK]))
        phrasing_client = FakeLLMClient(responses=["呼ばれたら困る"])

        summary, items = evaluate([QUESTIONS[1]], extractor, phrasing_client)

        self.assertEqual(summary.type1_detected, 0)
        self.assertEqual(summary.type2_detected, 0)
        self.assertFalse(items[0].needs_clarification)
        self.assertEqual(items[0].disclosure_text, "")
        self.assertEqual(phrasing_client.calls, [])

    def test_extraction_failure_is_recorded_as_failed(self):
        extractor = AxisExtractor(FakeLLMClient(responses=["わかりません"]))
        phrasing_client = FakeLLMClient(responses=[])

        summary, items = evaluate([QUESTIONS[0]], extractor, phrasing_client)

        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.judged, 0)
        self.assertIn("extraction failed", items[0].error)
        self.assertEqual(phrasing_client.calls, [])  # 抽出できなければ文言化も呼ばない

    def test_fatal_error_during_extraction_aborts(self):
        class Dead(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                raise LLMFatalError("429")

        extractor = AxisExtractor(Dead())
        summary, items = evaluate(QUESTIONS, extractor, FakeLLMClient())

        self.assertTrue(summary.aborted)
        self.assertEqual(len(items), 0)

    def test_phrasing_failure_does_not_invalidate_axis_detection(self):
        extractor = AxisExtractor(FakeLLMClient(responses=[EXTRACTION_TYPE1]))
        phrasing_client = FakeLLMClient(responses=[""])  # 空応答→LLMTransientError

        summary, items = evaluate([QUESTIONS[0]], extractor, phrasing_client)

        self.assertEqual(summary.judged, 1)
        self.assertEqual(summary.type1_detected, 1)  # 抽出・判定は有効のまま
        self.assertIn("phrasing failed", items[0].error)
        self.assertEqual(items[0].disclosure_text, "")

    def test_multiple_axes_counted_by_type(self):
        extraction = """\
AGGREGATION: EVENT
GROUP_BY: MASTER_STORES
GROUP_BY_GRANULARITY: IDENTITY
LIMITED: NO
GROUP_BY_RESOLVED: UNRESOLVED
FLAG_STORES: UNRESOLVED
FLAG_MEMBERS: NONE
FLAG_PRODUCTS: NONE
SYNONYM: region_UNRESOLVED
CATCHALL: pref_UNRESOLVED
PERIOD: NONE
REGION_FILTER: NONE
RATE_DENOMINATOR: NONE"""
        extractor = AxisExtractor(FakeLLMClient(responses=[extraction]))
        phrasing_client = FakeLLMClient(responses=["開示と確認をまとめた文章。"])

        summary, items = evaluate([QUESTIONS[0]], extractor, phrasing_client)

        self.assertEqual(summary.type1_detected, 1)
        self.assertEqual(summary.type2_detected, 1)
        self.assertEqual(summary.type3_detected, 1)
        self.assertEqual(summary.type4_detected, 1)
        self.assertEqual(len(items[0].axes), 4)


class FormatSummaryTests(SimpleTestCase):
    def test_contains_type_breakdown(self):
        extractor = AxisExtractor(FakeLLMClient(responses=[EXTRACTION_NO_RISK]))
        summary, _ = evaluate([QUESTIONS[1]], extractor, FakeLLMClient())
        text = format_summary(summary)
        self.assertIn("型1", text)
        self.assertIn("聞き返しが要ると判定した件数", text)
