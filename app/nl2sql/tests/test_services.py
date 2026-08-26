"""nl2sql/services と store。実験1回分の組み立てと記録。

LLMは fake に差し替え、スキーマ取得も差し替えるので、DBは実験レコードの
保存にだけ使う（テストDB）。ここで確かめたいのは、
スコアがそのままレコードに載ることと、失敗時に握りつぶさないこと。
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from app.nl2sql import services, store
from app.nl2sql.models import Nl2SqlExperiment

SCHEMA_TEXT = "CREATE TABLE demo_sales.stores (\n  store_id integer NOT NULL\n);"
ALLOWED = {"stores", "demo_sales.stores"}

QUESTIONS = [
    {
        "id": "q001",
        "question": "店舗数は？",
        "gold_sql": "SELECT count(*) FROM demo_sales.stores",
        "tags": ["集計"],
    },
    {
        "id": "q002",
        "question": "店舗コードの一覧は？",
        "gold_sql": "SELECT store_code FROM demo_sales.stores",
        "tags": ["集計", "表記ゆれ"],
    },
]


class FakeExecutor:
    """gold も生成SQLもここを通る。SQL文字列で結果を出し分ける。"""

    def __init__(self, results):
        self.results = results
        self.closed = False

    def run(self, sql):
        from app.nl2sql.core.sql_executor import QueryResult

        if sql in self.results:
            return self.results[sql]
        return QueryResult(ok=True, rows=[(0,)], row_count=1)

    def close(self):
        self.closed = True


def write_questions(data=QUESTIONS):
    path = Path(tempfile.mkdtemp()) / "questions.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class LoadFewShotTests(TestCase):
    def test_zero_returns_empty(self):
        self.assertEqual(services.load_few_shot(0), ())

    def test_missing_file_raises_when_requested(self):
        """評価データからの流用を禁じているので、無ければ落とす。"""
        with self.assertRaises(FileNotFoundError):
            services.load_few_shot(2, Path(tempfile.mkdtemp()) / "nope.json")

    def test_loads_examples(self):
        path = Path(tempfile.mkdtemp()) / "few.json"
        path.write_text(
            json.dumps([{"question": "q1", "sql": "SELECT 1"}, {"question": "q2", "sql": "SELECT 2"}]),
            encoding="utf-8",
        )
        examples = services.load_few_shot(1, path)
        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0].sql, "SELECT 1")

    def test_not_enough_examples_raises(self):
        path = Path(tempfile.mkdtemp()) / "few.json"
        path.write_text(json.dumps([{"question": "q1", "sql": "SELECT 1"}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            services.load_few_shot(3, path)


class LoadHintsTests(TestCase):
    def test_returns_empty_unless_hint_mode(self):
        from app.nl2sql.params import Nl2SqlParams

        self.assertEqual(services.load_hints(Nl2SqlParams.from_dict({})), "")

    def test_missing_file_raises_in_hint_mode(self):
        from app.nl2sql.params import Nl2SqlParams

        params = Nl2SqlParams.from_dict({"schema_desc": "hint"})
        with self.assertRaises(FileNotFoundError):
            services.load_hints(params, Path(tempfile.mkdtemp()) / "nope.md")

    def test_reads_file_in_hint_mode(self):
        from app.nl2sql.params import Nl2SqlParams

        path = Path(tempfile.mkdtemp()) / "hints.md"
        path.write_text("- 注意事項\n", encoding="utf-8")
        params = Nl2SqlParams.from_dict({"schema_desc": "hint"})
        self.assertEqual(services.load_hints(params, path), "- 注意事項")


@override_settings(NL2SQL_LOGS_DIR=tempfile.mkdtemp())
class RunExperimentTests(TestCase):
    """明細の保存先を一時ディレクトリへ逃がす。

    逃がさないと、テストDBで採番された id で実ファイル
    `data/nl2sql/logs/exp_*.json` を書き潰す。
    """

    def _run(self, responses, results=None, params=None, questions=QUESTIONS):
        from app.nl2sql.core.sql_executor import QueryResult

        results = results or {
            "SELECT count(*) FROM demo_sales.stores": QueryResult(
                ok=True, rows=[(40,)], row_count=1
            ),
            "SELECT store_code FROM demo_sales.stores": QueryResult(
                ok=True, rows=[("S001",), ("S002",)], row_count=2
            ),
        }
        executor = FakeExecutor(results)
        path = write_questions(questions)

        with patch.object(services, "build_schema_text", return_value=(SCHEMA_TEXT, ALLOWED)), \
             patch.object(services, "SqlExecutor", return_value=executor), \
             patch.object(services, "get_client") as get_client:
            from app.common.llm_client import FakeLLMClient

            get_client.return_value = FakeLLMClient(model="fake", responses=responses)
            outcome = services.run_experiment(
                "テスト実験", params or {"model": "fake:test"}, questions_path=path
            )
        return outcome, executor

    def test_all_correct_is_recorded(self):
        outcome, executor = self._run(
            [
                "SELECT count(*) FROM demo_sales.stores",
                "SELECT store_code FROM demo_sales.stores",
            ]
        )

        self.assertEqual(outcome["status"], "success")
        experiment = Nl2SqlExperiment.objects.get(id=outcome["experiment_id"])
        self.assertEqual(experiment.name, "テスト実験")
        self.assertEqual(experiment.question_count, 2)
        self.assertEqual(experiment.correct, 2)
        self.assertEqual(experiment.execution_accuracy, 1.0)
        self.assertEqual(experiment.valid_sql_rate, 1.0)
        self.assertTrue(executor.closed)

    def test_parameters_and_schema_are_recorded(self):
        outcome, _ = self._run(["SELECT count(*) FROM demo_sales.stores"])
        experiment = Nl2SqlExperiment.objects.get(id=outcome["experiment_id"])

        self.assertEqual(experiment.parameters["model"], "fake:test")
        self.assertEqual(experiment.schema_snapshot, SCHEMA_TEXT)
        # 代表プロンプトが1本残っている（LLMを呼ばずに組み立てたもの）
        self.assertIn("店舗数は？", experiment.prompt_sample)
        self.assertIn("demo_sales.stores", experiment.prompt_sample)

    def test_tag_breakdown_is_recorded(self):
        outcome, _ = self._run(
            ["SELECT count(*) FROM demo_sales.stores", "SELECT 999"]
        )
        experiment = Nl2SqlExperiment.objects.get(id=outcome["experiment_id"])

        self.assertEqual(experiment.by_tag["集計"], {"correct": 1, "total": 2})
        self.assertEqual(experiment.by_tag["表記ゆれ"], {"correct": 0, "total": 1})

    def test_details_file_is_written(self):
        outcome, _ = self._run(["SELECT count(*) FROM demo_sales.stores"])
        details = store.read_details(outcome["experiment_id"])

        self.assertIsNotNone(details)
        self.assertEqual(len(details["items"]), 2)
        self.assertIn("parameter_line", details["meta"])
        self.assertEqual(details["items"][0]["id"], "q001")

    def test_dataset_version_is_recorded(self):
        """版を残さないと、後から「どの評価データで測ったか」が分からなくなる。"""
        outcome, _ = self._run(["SELECT count(*) FROM demo_sales.stores"])
        experiment = Nl2SqlExperiment.objects.get(id=outcome["experiment_id"])

        self.assertEqual(len(experiment.dataset_digest), 64)
        self.assertEqual(experiment.dataset_short, experiment.dataset_digest[:8])
        self.assertTrue(experiment.dataset_path)
        # 明細ファイル側にも同じ版が残る
        details = store.read_details(outcome["experiment_id"])
        self.assertEqual(details["meta"]["dataset_digest"], experiment.dataset_digest)

    def test_different_questions_get_different_digest(self):
        first, _ = self._run(["SELECT 1"])
        changed = [dict(QUESTIONS[0], gold_sql="SELECT count(1) FROM demo_sales.stores")]
        second, _ = self._run(["SELECT 1"], questions=changed)

        a = Nl2SqlExperiment.objects.get(id=first["experiment_id"]).dataset_digest
        b = Nl2SqlExperiment.objects.get(id=second["experiment_id"]).dataset_digest
        self.assertNotEqual(a, b)

    def test_details_carry_gold_sql(self):
        """明細だけで結果を読めるように、正解SQLも残す。"""
        outcome, _ = self._run(["SELECT count(*) FROM demo_sales.stores"])
        details = store.read_details(outcome["experiment_id"])
        first = details["items"][0]
        self.assertEqual(first["gold_sql"], "SELECT count(*) FROM demo_sales.stores")
        self.assertIn("ordered", first)
        self.assertIn("note", first)

    def test_details_missing_returns_none(self):
        self.assertIsNone(store.read_details(999999))

    def test_unsupported_parameter_is_an_error_not_a_crash(self):
        outcome = services.run_experiment("x", {"value_hint": "true"})
        self.assertEqual(outcome["status"], "error")
        self.assertIn("value_hint", outcome["error"])
        self.assertEqual(Nl2SqlExperiment.objects.count(), 0)

    def test_invalid_parameter_is_an_error(self):
        outcome = services.run_experiment("x", {"schema_mode": "auto"})
        self.assertEqual(outcome["status"], "error")
        self.assertEqual(Nl2SqlExperiment.objects.count(), 0)

    def test_schema_failure_is_reported(self):
        with patch.object(services, "build_schema_text", side_effect=RuntimeError("接続できません")):
            outcome = services.run_experiment("x", {"model": "fake:test"})
        self.assertEqual(outcome["status"], "error")
        self.assertIn("接続できません", outcome["error"])

    def test_broken_gold_is_counted_separately(self):
        from app.nl2sql.core.sql_executor import QueryResult

        results = {
            "SELECT count(*) FROM demo_sales.stores": QueryResult(
                ok=False, error="syntax error", error_kind="syntax"
            ),
            "SELECT store_code FROM demo_sales.stores": QueryResult(
                ok=True, rows=[("S001",), ("S002",)], row_count=2
            ),
        }
        outcome, _ = self._run(["SELECT store_code FROM demo_sales.stores"], results=results)
        experiment = Nl2SqlExperiment.objects.get(id=outcome["experiment_id"])

        self.assertEqual(experiment.question_count, 2)
        self.assertEqual(experiment.scored, 1)
        self.assertEqual(experiment.gold_failed, 1)
        self.assertEqual(experiment.execution_accuracy, 1.0)

    def test_summary_text_is_returned(self):
        outcome, _ = self._run(["SELECT count(*) FROM demo_sales.stores"])
        self.assertIn("実行結果一致率", outcome["text"])


class ModelTests(TestCase):
    def test_total_tokens(self):
        experiment = Nl2SqlExperiment.objects.create(
            prompt_tokens=100, completion_tokens=20
        )
        self.assertEqual(experiment.total_tokens, 120)

    def test_ordering_is_newest_first(self):
        first = Nl2SqlExperiment.objects.create(name="1")
        second = Nl2SqlExperiment.objects.create(name="2")
        self.assertEqual(list(Nl2SqlExperiment.objects.all()), [second, first])
