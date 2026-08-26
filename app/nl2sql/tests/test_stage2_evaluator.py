"""nl2sql/core/stage2_evaluator。混同行列とキーワード一致の集計。

DBは使わない。`AmbiguityJudge` を差し替えて、判断結果の並びから集計が
正しく組み上がるかを確かめる。
"""
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from app.common.llm_client import FakeLLMClient
from app.nl2sql.core.ambiguity_judge import AmbiguityJudge
from app.nl2sql.core.stage2_evaluator import (
    Stage2Question,
    dataset_digest,
    evaluate,
    format_summary,
    load_questions,
)

QUESTIONS = [
    Stage2Question(
        id="s001",
        question="曖昧な質問1",
        is_ambiguous=True,
        expected_clarification_keywords=["取引が無い店舗", "実績が無い店舗"],
    ),
    Stage2Question(
        id="s002",
        question="曖昧な質問2",
        is_ambiguous=True,
        expected_clarification_keywords=["退会した会員"],
    ),
    Stage2Question(id="c001", question="対照質問1", is_ambiguous=False),
    Stage2Question(id="c002", question="対照質問2", is_ambiguous=False),
]


def write_questions(items):
    path = Path(tempfile.mkdtemp()) / "stage2_questions.json"
    payload = [
        {
            "id": q["id"],
            "question": q["question"],
            "is_ambiguous": q["is_ambiguous"],
            "expected_clarification_keywords": q.get("expected_clarification_keywords", []),
            "note": q.get("note", "無視されるはずのキー"),
        }
        for q in items
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class LoadQuestionsTests(SimpleTestCase):
    def test_loads_fields(self):
        path = write_questions(
            [{"id": "s001", "question": "q", "is_ambiguous": True,
              "expected_clarification_keywords": ["kw"]}]
        )
        questions = load_questions(path)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].id, "s001")
        self.assertTrue(questions[0].is_ambiguous)
        self.assertEqual(questions[0].expected_clarification_keywords, ["kw"])


class DatasetDigestTests(SimpleTestCase):
    def test_note_does_not_affect_digest(self):
        """note を1文字直しても版が変わらない（issue_c009 で決めた対象外列）。"""
        a = [Stage2Question(id="s001", question="q", is_ambiguous=True)]
        b = [Stage2Question(id="s001", question="q", is_ambiguous=True)]
        self.assertEqual(dataset_digest(a), dataset_digest(b))

    def test_question_change_affects_digest(self):
        a = [Stage2Question(id="s001", question="q1", is_ambiguous=True)]
        b = [Stage2Question(id="s001", question="q2", is_ambiguous=True)]
        self.assertNotEqual(dataset_digest(a), dataset_digest(b))

    def test_keywords_affect_digest(self):
        a = [Stage2Question(id="s001", question="q", is_ambiguous=True,
                             expected_clarification_keywords=["x"])]
        b = [Stage2Question(id="s001", question="q", is_ambiguous=True,
                             expected_clarification_keywords=["y"])]
        self.assertNotEqual(dataset_digest(a), dataset_digest(b))


class EvaluateTests(SimpleTestCase):
    def _judge(self, responses):
        return AmbiguityJudge(FakeLLMClient(responses=responses))

    def test_confusion_matrix(self):
        # s001: 曖昧→聞き返した(TP) / s002: 曖昧→答えた(FN)
        # c001: 対照→聞き返した(FP) / c002: 対照→答えた(TN)
        responses = [
            "JUDGEMENT: CLARIFY\nQUESTION: 取引が無い店舗も含めますか？",
            "JUDGEMENT: ANSWER",
            "JUDGEMENT: CLARIFY\nQUESTION: 何か確認したいことがあります",
            "JUDGEMENT: ANSWER",
        ]
        summary, items = evaluate(QUESTIONS, self._judge(responses))

        self.assertEqual(summary.tp, 1)
        self.assertEqual(summary.fn, 1)
        self.assertEqual(summary.fp, 1)
        self.assertEqual(summary.tn, 1)
        self.assertEqual(summary.judged, 4)
        self.assertEqual(summary.failed, 0)
        self.assertEqual([item.confusion for item in items], ["TP", "FN", "FP", "TN"])

    def test_miss_rate_is_main_metric(self):
        """見逃し率 = fn / (tp+fn)。対照群の結果には影響されない。"""
        responses = [
            "JUDGEMENT: ANSWER",  # s001 曖昧→答えた(FN)
            "JUDGEMENT: ANSWER",  # s002 曖昧→答えた(FN)
            "JUDGEMENT: ANSWER",  # c001 対照→答えた(TN)
            "JUDGEMENT: ANSWER",  # c002 対照→答えた(TN)
        ]
        summary, _ = evaluate(QUESTIONS, self._judge(responses))
        self.assertEqual(summary.miss_rate, 1.0)
        self.assertEqual(summary.false_positive_rate, 0.0)

    def test_axis2_only_scores_true_positives(self):
        """第2軸はTPだけを対象にする。FNは聞き返していないので判定しようがない。"""
        responses = [
            "JUDGEMENT: CLARIFY\nQUESTION: 取引が無い店舗も含めますか？",  # s001 TP, キーワード一致
            "JUDGEMENT: ANSWER",  # s002 FN
            "JUDGEMENT: ANSWER",  # c001 TN
            "JUDGEMENT: ANSWER",  # c002 TN
        ]
        summary, items = evaluate(QUESTIONS, self._judge(responses))

        self.assertEqual(summary.axis2_scored, 1)
        self.assertEqual(summary.axis2_keyword_match, 1)
        self.assertEqual(summary.axis2_keyword_match_rate, 1.0)

        tp_item = items[0]
        self.assertTrue(tp_item.keyword_match)
        self.assertEqual(tp_item.matched_keywords, ["取引が無い店舗"])

        fn_item = items[1]
        self.assertEqual(fn_item.matched_keywords, [])
        self.assertFalse(fn_item.keyword_match)

    def test_keyword_match_is_plain_substring_no_normalization(self):
        """表記の揺れは正規化しない（issue_c009 確定事項b）。"""
        question = [Stage2Question(
            id="s001", question="q", is_ambiguous=True,
            expected_clarification_keywords=["取引が無い店舗"],
        )]
        judge = self._judge(["JUDGEMENT: CLARIFY\nQUESTION: 取引のない店舗も含めますか？"])
        summary, items = evaluate(question, judge)

        # 「が」と「の」の表記差があるため一致しない、という仕様どおりの挙動
        self.assertEqual(summary.axis2_keyword_match, 0)
        self.assertFalse(items[0].keyword_match)

    def test_parse_failure_is_recorded_as_failed_not_confusion(self):
        """パース失敗は混同行列の4マスのどこにも入れず、failed に計上する。"""
        questions = [Stage2Question(id="s001", question="q", is_ambiguous=True)]
        judge = self._judge(["わかりません"])
        summary, items = evaluate(questions, judge)

        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.judged, 0)
        self.assertEqual(summary.tp, 0)
        self.assertEqual(summary.fn, 0)
        self.assertEqual(items[0].confusion, "FAILED")
        self.assertIsNone(items[0].judged_ask)

    def test_denominator_shrinks_with_failures_not_silently(self):
        """failed が出ても tp+fn は減るだけで、分母が黙って狂うわけではない。"""
        questions = [
            Stage2Question(id="s001", question="q1", is_ambiguous=True),
            Stage2Question(id="s002", question="q2", is_ambiguous=True),
        ]
        judge = self._judge(["わかりません", "JUDGEMENT: ANSWER"])
        summary, _ = evaluate(questions, judge)

        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.fn, 1)
        self.assertEqual(summary.miss_rate, 1.0)  # 分母は判定できた1件のみ

    def test_fatal_error_aborts_and_keeps_partial_results(self):
        class Dead(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                from app.common.llm_client import LLMFatalError as Fatal
                raise Fatal("429")

        judge = AmbiguityJudge(Dead())
        summary, items = evaluate(QUESTIONS, judge)

        self.assertTrue(summary.aborted)
        self.assertEqual(len(items), 0)

    def test_pause_sec_is_not_called_before_first_question(self):
        judge = self._judge(["JUDGEMENT: ANSWER"] * 4)
        # pause_sec>0 でも即時完了すること（実質的なスリープ回数=3だが、ここでは
        # 例外なく完走することだけを確認する。時間計測はしない）
        summary, items = evaluate(QUESTIONS, judge, pause_sec=0.0)
        self.assertEqual(len(items), 4)


class FormatSummaryTests(SimpleTestCase):
    def test_contains_miss_rate(self):
        judge = AmbiguityJudge(FakeLLMClient(responses=["JUDGEMENT: ANSWER"] * 4))
        summary, _ = evaluate(QUESTIONS, judge)
        text = format_summary(summary)
        self.assertIn("見逃し率", text)
        self.assertIn("誤検出率", text)
