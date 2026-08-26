"""nl2sql/stage2_services と stage2_store。段階2実験1回分の組み立てと記録。

LLMは fake に差し替える。DBは実験レコードの保存にだけ使う（テストDB）。
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from app.nl2sql import stage2_services, stage2_store
from app.nl2sql.models import Nl2SqlStage2Experiment

QUESTIONS = [
    {"id": "s001", "question": "曖昧な質問", "is_ambiguous": True,
     "expected_clarification_keywords": ["取引が無い店舗"]},
    {"id": "c001", "question": "対照質問", "is_ambiguous": False,
     "expected_clarification_keywords": []},
]


def write_questions(data=QUESTIONS):
    path = Path(tempfile.mkdtemp()) / "stage2_questions.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@override_settings(NL2SQL_STAGE2_LOGS_DIR=tempfile.mkdtemp())
class RunStage2ExperimentTests(TestCase):
    """明細の保存先を一時ディレクトリへ逃がす（段階1と同じ理由）。"""

    def _run(self, responses, questions=QUESTIONS, **kwargs):
        from app.common.llm_client import FakeLLMClient

        path = write_questions(questions)
        with patch.object(stage2_services, "get_client") as get_client:
            get_client.return_value = FakeLLMClient(model="fake", responses=responses)
            outcome = stage2_services.run_stage2_experiment(
                "段階2テスト", questions_path=path, **kwargs
            )
        return outcome

    def test_all_answered_is_recorded(self):
        outcome = self._run(["JUDGEMENT: ANSWER", "JUDGEMENT: ANSWER"])

        self.assertEqual(outcome["status"], "success")
        experiment = Nl2SqlStage2Experiment.objects.get(id=outcome["experiment_id"])
        self.assertEqual(experiment.name, "段階2テスト")
        self.assertEqual(experiment.question_count, 2)
        self.assertEqual(experiment.judged, 2)
        self.assertEqual(experiment.failed, 0)
        self.assertEqual(experiment.fn, 1)  # s001（曖昧）が答えられた→見逃し
        self.assertEqual(experiment.tn, 1)  # c001（対照）が答えられた→正しい

    def test_sql_is_never_generated(self):
        """判断ステップだけが呼ばれ、SQL生成用のクライアント呼び出しは1回のみ×質問数。"""
        from app.common.llm_client import FakeLLMClient

        path = write_questions(QUESTIONS)
        fake = FakeLLMClient(model="fake", responses=["JUDGEMENT: ANSWER"] * 2)
        with patch.object(stage2_services, "get_client", return_value=fake):
            stage2_services.run_stage2_experiment("x", questions_path=path)

        # 質問数と同じ回数だけ呼ばれる（生成の再試行や別呼び出しが無い）
        self.assertEqual(len(fake.calls), 2)
        for call in fake.calls:
            self.assertNotIn("# スキーマ", call)

    def test_parameters_are_recorded(self):
        outcome = self._run(["JUDGEMENT: ANSWER"] * 2, model="openai:gpt-4.1-mini", temperature=0.0)
        experiment = Nl2SqlStage2Experiment.objects.get(id=outcome["experiment_id"])

        self.assertEqual(experiment.parameters["model"], "openai:gpt-4.1-mini")
        self.assertEqual(experiment.parameters["temperature"], 0.0)
        # schema_desc を指定しなければスキーマを渡していない。issue_c009 のベースラインと同じ
        self.assertIsNone(experiment.parameters["schema_desc"])

    def test_dataset_digest_is_recorded(self):
        outcome = self._run(["JUDGEMENT: ANSWER"] * 2)
        experiment = Nl2SqlStage2Experiment.objects.get(id=outcome["experiment_id"])

        self.assertEqual(len(experiment.dataset_digest), 64)
        self.assertEqual(experiment.dataset_short, experiment.dataset_digest[:8])

    def test_details_file_is_written_with_raw_response(self):
        outcome = self._run(["JUDGEMENT: CLARIFY\nQUESTION: 取引が無い店舗も含めますか？",
                              "JUDGEMENT: ANSWER"])
        details = stage2_store.read_details(outcome["experiment_id"])

        self.assertIsNotNone(details)
        self.assertEqual(len(details["items"]), 2)
        first = details["items"][0]
        self.assertEqual(first["id"], "s001")
        self.assertIn("raw_response", first)
        self.assertTrue(first["raw_response"])
        self.assertEqual(first["confusion"], "TP")

    def test_details_missing_returns_none(self):
        self.assertIsNone(stage2_store.read_details(999999))

    def test_failed_parse_does_not_abort_the_run(self):
        outcome = self._run(["わかりません", "JUDGEMENT: ANSWER"])
        experiment = Nl2SqlStage2Experiment.objects.get(id=outcome["experiment_id"])

        self.assertEqual(outcome["status"], "success")
        self.assertEqual(experiment.failed, 1)
        self.assertEqual(experiment.judged, 1)
        self.assertFalse(experiment.aborted)

    def test_fatal_error_is_reported_as_aborted(self):
        from app.common.llm_client import FakeLLMClient, LLMFatalError

        class Dead(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                raise LLMFatalError("429")

        path = write_questions(QUESTIONS)
        with patch.object(stage2_services, "get_client", return_value=Dead()):
            outcome = stage2_services.run_stage2_experiment("x", questions_path=path)

        self.assertEqual(outcome["status"], "success")
        experiment = Nl2SqlStage2Experiment.objects.get(id=outcome["experiment_id"])
        self.assertTrue(experiment.aborted)
        self.assertIn("429", experiment.abort_reason)

    def test_summary_text_is_returned(self):
        outcome = self._run(["JUDGEMENT: ANSWER"] * 2)
        self.assertIn("見逃し率", outcome["text"])


@override_settings(NL2SQL_STAGE2_LOGS_DIR=tempfile.mkdtemp())
class SchemaDescTests(TestCase):
    """`issue_c010`: 判断ステップへのスキーマ説明の導入。

    DB接続（`_build_schema_text`）はモックし、ここでは
    「渡した値がどう伝わるか」だけを確かめる。
    """

    def _run(self, responses, schema_text="CREATE TABLE demo_sales.stores (...);", hints="", **kwargs):
        from app.common.llm_client import FakeLLMClient

        path = write_questions(QUESTIONS)
        fake = FakeLLMClient(model="fake", responses=responses)
        with patch.object(stage2_services, "get_client", return_value=fake), \
             patch.object(stage2_services, "_build_schema_text", return_value=(schema_text, hints)) as build:
            outcome = stage2_services.run_stage2_experiment("x", questions_path=path, **kwargs)
        return outcome, fake, build

    def test_none_hint_comment_all_pass(self):
        for schema_desc in stage2_services.SCHEMA_DESC_VALUES:
            with self.subTest(schema_desc=schema_desc):
                outcome, _, build = self._run(["JUDGEMENT: ANSWER"] * 2, schema_desc=schema_desc)
                self.assertEqual(outcome["status"], "success")
                build.assert_called_once_with(schema_desc)

    def test_unsupported_value_is_an_error(self):
        outcome, _, build = self._run(["JUDGEMENT: ANSWER"] * 2, schema_desc="retrieved")
        self.assertEqual(outcome["status"], "error")
        self.assertIn("schema_desc", outcome["error"])
        build.assert_not_called()

    def test_default_does_not_fetch_schema(self):
        """schema_desc を指定しなければ、DB接続（_build_schema_text）自体を呼ばない。"""
        outcome, fake, build = self._run(["JUDGEMENT: ANSWER"] * 2)
        self.assertEqual(outcome["status"], "success")
        build.assert_not_called()
        for call in fake.calls:
            self.assertNotIn("# スキーマ", call)

    def test_schema_text_reaches_the_prompt(self):
        _, fake, _ = self._run(
            ["JUDGEMENT: ANSWER"] * 2,
            schema_text="CREATE TABLE demo_sales.sales (sale_id integer);",
            schema_desc="none",
        )
        for call in fake.calls:
            self.assertIn("# スキーマ", call)
            self.assertIn("CREATE TABLE demo_sales.sales", call)

    def test_hints_reach_the_prompt(self):
        _, fake, _ = self._run(
            ["JUDGEMENT: ANSWER"] * 2,
            hints="- daily_store_summary は取引ゼロの日の行が無い",
            schema_desc="hint",
        )
        for call in fake.calls:
            self.assertIn("# 注意（このデータ固有）", call)
            self.assertIn("daily_store_summary", call)

    def test_schema_desc_is_recorded_per_value(self):
        for schema_desc in stage2_services.SCHEMA_DESC_VALUES:
            with self.subTest(schema_desc=schema_desc):
                outcome, _, _ = self._run(["JUDGEMENT: ANSWER"] * 2, schema_desc=schema_desc)
                experiment = Nl2SqlStage2Experiment.objects.get(id=outcome["experiment_id"])
                self.assertEqual(experiment.parameters["schema_desc"], schema_desc)

    def test_missing_hint_file_is_reported_as_error(self):
        """hint はファイルが無ければ落とす（段階1の load_hints() をそのまま使うため）。

        `_build_schema_text` の中身（DB接続・introspect・render はモックし、
        `load_hints` だけ本物の「ファイルが無い」失敗を起こす）。
        """
        path = write_questions(QUESTIONS)
        from app.common.llm_client import FakeLLMClient

        with patch.object(stage2_services, "get_client", return_value=FakeLLMClient()), \
             patch.object(stage2_services, "connect_demo"), \
             patch.object(stage2_services, "introspect"), \
             patch.object(stage2_services, "render_schema_text", return_value="schema"), \
             patch.object(stage2_services, "load_hints", side_effect=FileNotFoundError("no hints")):
            outcome = stage2_services.run_stage2_experiment(
                "x", questions_path=path, schema_desc="hint"
            )

        self.assertEqual(outcome["status"], "error")
        self.assertIn("no hints", outcome["error"])


class ModelTests(TestCase):
    def test_total_tokens(self):
        experiment = Nl2SqlStage2Experiment.objects.create(prompt_tokens=50, completion_tokens=5)
        self.assertEqual(experiment.total_tokens, 55)

    def test_miss_rate(self):
        experiment = Nl2SqlStage2Experiment.objects.create(tp=1, fn=3)
        self.assertEqual(experiment.miss_rate, 0.75)

    def test_miss_rate_zero_denominator(self):
        experiment = Nl2SqlStage2Experiment.objects.create()
        self.assertEqual(experiment.miss_rate, 0.0)

    def test_false_positive_rate(self):
        experiment = Nl2SqlStage2Experiment.objects.create(fp=2, tn=4)
        self.assertEqual(experiment.false_positive_rate, round(2 / 6, 4))

    def test_axis2_keyword_match_rate(self):
        experiment = Nl2SqlStage2Experiment.objects.create(axis2_scored=4, axis2_keyword_match=3)
        self.assertEqual(experiment.axis2_keyword_match_rate, 0.75)

    def test_ordering_is_newest_first(self):
        first = Nl2SqlStage2Experiment.objects.create(name="1")
        second = Nl2SqlStage2Experiment.objects.create(name="2")
        self.assertEqual(list(Nl2SqlStage2Experiment.objects.all()), [second, first])
