"""nl2sql/stage2_rule_services。段階2（ルール判定版）実験1回分の組み立てと記録。"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from app.nl2sql import stage2_rule_services, stage2_rule_store
from app.nl2sql.models import Nl2SqlStage2RuleExperiment

QUESTIONS = [
    {"id": "q1", "question": "7月の平均日商、店舗名ごとに"},
    {"id": "q2", "question": "店舗形態別の店舗数"},
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


def write_questions(data=QUESTIONS):
    path = Path(tempfile.mkdtemp()) / "questions.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@override_settings(NL2SQL_STAGE2_RULE_LOGS_DIR=tempfile.mkdtemp())
class RunStage2RuleExperimentTests(TestCase):
    def _run(self, responses, questions=QUESTIONS, **kwargs):
        """`responses` は実際の呼び出し順どおりに積む。

        `issue_c034`：抽出（①）は本体→`FLAG_*`の2回に分かれた。1問あたり
        本体1回＋`FLAG_*`1回が基本で、抽出とルール判定の結果その問題が
        「言うことが無い」場合は文言化（②）を呼ばない（`phrase_disclosure`の
        ショートカット）ため、問題ごとに2回（no-risk）か3回（要聞き返し）になる。
        """
        from app.common.llm_client import FakeLLMClient

        path = write_questions(questions)
        fake = FakeLLMClient(responses=list(responses))
        # 既定は本物のDB照会を使うが、ユニットテストでは明示的に無効化して
        # DBに触れないようにする（フィクスチャは判定にかからない値のため
        # 実害は無いが、依存を切っておく）。
        # **照会は3つある。1つでも落とし忘れると demo DB へ接続しにいき、
        # 接続情報の無い環境でテストが落ちる。** 増えたら必ずここへ足すこと。
        kwargs.setdefault("flagged_activity_checker", None)
        kwargs.setdefault("roster_emptiness_checker", None)
        kwargs.setdefault("time_emptiness_checker", None)
        with patch.object(stage2_rule_services, "get_client", return_value=fake):
            outcome = stage2_rule_services.run_stage2_rule_experiment(
                "ルール判定テスト", questions_path=path, **kwargs
            )
        # 失敗時に原因が分かるようにする。run_experiment は例外を握り潰して
        # {"status": "error"} を返すため、これが無いと後続が KeyError で落ち、
        # 真の原因（DB接続失敗など）に辿り着けない。
        self.assertEqual(outcome.get("status"), "success", outcome.get("error"))
        return outcome, fake

    def test_all_no_risk_is_recorded(self):
        outcome, _ = self._run([EXTRACTION_NO_RISK] * 4)  # q1(本体+FLAG_*)・q2(本体+FLAG_*)

        self.assertEqual(outcome["status"], "success")
        experiment = Nl2SqlStage2RuleExperiment.objects.get(id=outcome["experiment_id"])
        self.assertEqual(experiment.name, "ルール判定テスト")
        self.assertEqual(experiment.question_count, 2)
        self.assertEqual(experiment.judged, 2)
        self.assertEqual(experiment.needs_clarification_count, 0)

    def test_type1_question_is_recorded_with_phrasing(self):
        # q1(type1): 本体→FLAG_*→文言化 の3回、q2(no-risk): 本体→FLAG_* の2回、計5回
        outcome, fake = self._run(
            [
                EXTRACTION_TYPE1,
                EXTRACTION_TYPE1,
                "取引の無い店舗を含めますか？",
                EXTRACTION_NO_RISK,
                EXTRACTION_NO_RISK,
            ],
            questions=QUESTIONS,
        )
        experiment = Nl2SqlStage2RuleExperiment.objects.get(id=outcome["experiment_id"])

        self.assertEqual(experiment.type1_detected, 1)
        self.assertEqual(experiment.needs_clarification_count, 1)
        self.assertEqual(len(fake.calls), 5)

    def test_parameters_are_recorded(self):
        outcome, _ = self._run(
            [EXTRACTION_NO_RISK] * 4, model="openai:gpt-4.1-mini", temperature=0.0
        )
        experiment = Nl2SqlStage2RuleExperiment.objects.get(id=outcome["experiment_id"])
        self.assertEqual(experiment.parameters["model"], "openai:gpt-4.1-mini")
        self.assertEqual(experiment.parameters["temperature"], 0.0)

    def test_dataset_digest_is_recorded(self):
        outcome, _ = self._run([EXTRACTION_NO_RISK] * 4)
        experiment = Nl2SqlStage2RuleExperiment.objects.get(id=outcome["experiment_id"])
        self.assertEqual(len(experiment.dataset_digest), 64)
        self.assertEqual(experiment.dataset_short, experiment.dataset_digest[:8])

    def test_prompt_digest_is_recorded(self):
        """`issue_c031`：DB行とJSON明細の`meta`の両方に同じ値が入ること。
        `issue_c034`：`flag_prompt_digest`も同様に入ること。
        """
        from app.nl2sql.core.axis_extractor import flag_prompt_digest, prompt_digest

        outcome, _ = self._run([EXTRACTION_NO_RISK] * 4)
        experiment = Nl2SqlStage2RuleExperiment.objects.get(id=outcome["experiment_id"])
        self.assertEqual(experiment.prompt_digest, prompt_digest())
        self.assertEqual(experiment.flag_prompt_digest, flag_prompt_digest())

        details = stage2_rule_store.read_details(outcome["experiment_id"])
        self.assertEqual(details["meta"]["prompt_digest"], prompt_digest())
        self.assertEqual(details["meta"]["flag_prompt_digest"], flag_prompt_digest())

    def test_details_file_is_written(self):
        outcome, _ = self._run([EXTRACTION_NO_RISK] * 4)
        details = stage2_rule_store.read_details(outcome["experiment_id"])

        self.assertIsNotNone(details)
        self.assertEqual(len(details["items"]), 2)
        self.assertEqual(details["items"][0]["id"], "q1")

    def test_details_missing_returns_none(self):
        self.assertIsNone(stage2_rule_store.read_details(999999))

    def test_extraction_failure_does_not_abort_the_run(self):
        # q1: 本体のパースで失敗し FLAG_* は呼ばれない(1回)。q2: 本体+FLAG_*(2回)
        outcome, _ = self._run(["わかりません", EXTRACTION_NO_RISK, EXTRACTION_NO_RISK])
        experiment = Nl2SqlStage2RuleExperiment.objects.get(id=outcome["experiment_id"])

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
        with patch.object(stage2_rule_services, "get_client", return_value=Dead()):
            outcome = stage2_rule_services.run_stage2_rule_experiment(
                "x", questions_path=path, flagged_activity_checker=None
            )

        self.assertEqual(outcome["status"], "success")
        experiment = Nl2SqlStage2RuleExperiment.objects.get(id=outcome["experiment_id"])
        self.assertTrue(experiment.aborted)

    def test_summary_text_is_returned(self):
        outcome, _ = self._run([EXTRACTION_NO_RISK] * 4)
        self.assertIn("未確定軸の検出", outcome["text"])

    def test_default_checker_is_the_real_db_backed_one(self):
        """`flagged_activity_checker` を指定しなければ本物のDB照会になる（issue_c015）。"""
        import inspect

        from app.nl2sql.core.flagged_activity import check_flagged_activity

        default = inspect.signature(
            stage2_rule_services.run_stage2_rule_experiment
        ).parameters["flagged_activity_checker"].default
        self.assertIs(default, check_flagged_activity)

    def test_flagged_activity_checker_is_wired_through_to_absorption(self):
        """パターンAの吸収判定が、サービス層まで通しで効くことを確認する（フェイクでDB回避）。"""
        extraction_with_flag = """\
AGGREGATION: EVENT
GROUP_BY: MASTER_STORES
GROUP_BY_GRANULARITY: IDENTITY
LIMITED: NO
GROUP_BY_RESOLVED: UNRESOLVED
FLAG_STORES: UNRESOLVED
FLAG_MEMBERS: NONE
FLAG_PRODUCTS: NONE
SYNONYM: NONE
CATCHALL: NONE
PERIOD: 2026-07-01..2026-08-01
REGION_FILTER: NONE
RATE_DENOMINATOR: NONE"""
        outcome, _ = self._run(
            [extraction_with_flag, extraction_with_flag, EXTRACTION_NO_RISK, EXTRACTION_NO_RISK],
            flagged_activity_checker=lambda table, period, *, region_filter=None: False,  # 活動なし→吸収
        )
        experiment = Nl2SqlStage2RuleExperiment.objects.get(id=outcome["experiment_id"])
        self.assertEqual(experiment.type1_detected, 1)
        self.assertEqual(experiment.type2_detected, 0)  # 吸収されて別立てされない


class ModelTests(TestCase):
    def test_total_tokens(self):
        experiment = Nl2SqlStage2RuleExperiment.objects.create(prompt_tokens=20, completion_tokens=5)
        self.assertEqual(experiment.total_tokens, 25)

    def test_ordering_is_newest_first(self):
        first = Nl2SqlStage2RuleExperiment.objects.create(name="1")
        second = Nl2SqlStage2RuleExperiment.objects.create(name="2")
        self.assertEqual(list(Nl2SqlStage2RuleExperiment.objects.all()), [second, first])
