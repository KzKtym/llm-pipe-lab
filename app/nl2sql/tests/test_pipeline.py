"""nl2sql/core/pipeline。生成→検査→実行のつなぎと、書き直しの制御。

DBもAPIも使わない。executor はダミーに差し替え、返す QueryResult を並べて
再試行の挙動を見る。ここで確かめたいのは「何回まわすか」と
「失敗の内容が次のプロンプトに渡るか」の2点。
"""
from django.test import SimpleTestCase

from app.common.llm_client import FakeLLMClient, LLMFatalError, LLMTransientError
from app.nl2sql.core.generator import SqlGenerator
from app.nl2sql.core.pipeline import Nl2SqlPipeline
from app.nl2sql.core.sql_executor import QueryResult

SCHEMA = "CREATE TABLE demo_sales.stores (store_id integer);"


class FakeExecutor:
    """並べた QueryResult を順に返す。最後まで来たら最後の1つを返し続ける。"""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def run(self, sql):
        self.calls.append(sql)
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[index]


def ok_result(sql="SELECT 1"):
    return QueryResult(ok=True, sql=sql, columns=["a"], rows=[(1,)], row_count=1)


def ng_result(kind="undefined", error='column "x" does not exist'):
    return QueryResult(ok=False, error=error, error_kind=kind)


class PipelineTests(SimpleTestCase):
    def _pipeline(self, responses, results, **kwargs):
        client = FakeLLMClient(model="fake", responses=responses)
        generator = SqlGenerator(client, schema_text=SCHEMA)
        executor = FakeExecutor(results)
        return Nl2SqlPipeline(generator, executor, **kwargs), client, executor

    def test_success_on_first_attempt(self):
        pipeline, _, executor = self._pipeline(["SELECT 1"], [ok_result()])
        outcome = pipeline.run("店舗数は？")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(outcome.sql, "SELECT 1")
        self.assertEqual(outcome.history, [])
        self.assertEqual(executor.calls, ["SELECT 1"])

    def test_failure_without_self_correct_stops_at_one(self):
        pipeline, client, _ = self._pipeline(["SELECT bad"], [ng_result()])
        outcome = pipeline.run("q")

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(len(outcome.history), 1)
        self.assertEqual(outcome.error_kind, "undefined")
        self.assertEqual(len(client.calls), 1)

    def test_self_correct_retries_and_succeeds(self):
        pipeline, client, executor = self._pipeline(
            ["SELECT bad", "SELECT good"],
            [ng_result(), ok_result("SELECT good")],
            self_correct=True,
        )
        outcome = pipeline.run("q")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(outcome.sql, "SELECT good")
        # 失敗した1回目だけが履歴に残る
        self.assertEqual(len(outcome.history), 1)
        self.assertEqual(outcome.history[0].sql, "SELECT bad")
        self.assertEqual(executor.calls, ["SELECT bad", "SELECT good"])

    def test_failure_is_fed_back_into_the_next_prompt(self):
        pipeline, client, _ = self._pipeline(
            ["SELECT bad", "SELECT good"],
            [ng_result(error='column "bad" does not exist'), ok_result()],
            self_correct=True,
        )
        pipeline.run("q")

        second_prompt = client.calls[1]
        self.assertIn("# 直前の試行", second_prompt)
        self.assertIn("SELECT bad", second_prompt)
        self.assertIn('column "bad" does not exist', second_prompt)

    def test_validation_failure_is_also_retried(self):
        """検査で落ちた理由もモデルへの手がかりになる。実行時エラーと区別しない。"""
        pipeline, client, _ = self._pipeline(
            ["DELETE FROM stores", "SELECT 1"],
            [ng_result(kind="validation", error="禁止されたキーワードを含みます: DELETE"),
             ok_result()],
            self_correct=True,
        )
        outcome = pipeline.run("q")

        self.assertTrue(outcome.ok)
        self.assertIn("DELETE", client.calls[1])

    def test_retry_k_limits_attempts(self):
        pipeline, client, _ = self._pipeline(
            ["a", "b", "c", "d"], [ng_result()], self_correct=True, retry_k=2
        )
        outcome = pipeline.run("q")

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.attempts, 3)  # 初回 + 2回
        self.assertEqual(len(outcome.history), 3)
        self.assertEqual(len(client.calls), 3)

    def test_retry_k_zero_means_no_retry(self):
        pipeline, client, _ = self._pipeline(
            ["a", "b"], [ng_result()], self_correct=True, retry_k=0
        )
        outcome = pipeline.run("q")
        self.assertEqual(outcome.attempts, 1)

    def test_max_attempts_property(self):
        pipeline, _, _ = self._pipeline(["a"], [ok_result()], self_correct=True, retry_k=3)
        self.assertEqual(pipeline.max_attempts, 4)

        pipeline, _, _ = self._pipeline(["a"], [ok_result()], self_correct=False, retry_k=3)
        self.assertEqual(pipeline.max_attempts, 1)

    def test_tokens_accumulate_across_attempts(self):
        class CountingClient(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                response = super().complete(prompt, **kwargs)
                response.prompt_tokens = 100
                response.completion_tokens = 10
                return response

        generator = SqlGenerator(
            CountingClient(model="fake", responses=["a", "b"]), schema_text=SCHEMA
        )
        pipeline = Nl2SqlPipeline(
            generator, FakeExecutor([ng_result()]), self_correct=True, retry_k=1
        )
        outcome = pipeline.run("q")

        self.assertEqual(outcome.prompt_tokens, 200)
        self.assertEqual(outcome.completion_tokens, 20)
        self.assertEqual(outcome.total_tokens, 220)

    def test_transient_llm_error_becomes_a_failed_question(self):
        class Flaky(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                raise LLMTransientError("timeout")

        pipeline = Nl2SqlPipeline(
            SqlGenerator(Flaky(), schema_text=SCHEMA), FakeExecutor([ok_result()])
        )
        outcome = pipeline.run("q")

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "llm")
        self.assertIn("timeout", outcome.error)

    def test_fatal_llm_error_propagates(self):
        """認証切れは次の質問でも同じ。ここで握らず、ランナーに止めさせる。"""

        class Dead(FakeLLMClient):
            def complete(self, prompt, **kwargs):
                raise LLMFatalError("401")

        pipeline = Nl2SqlPipeline(
            SqlGenerator(Dead(), schema_text=SCHEMA), FakeExecutor([ok_result()])
        )
        with self.assertRaises(LLMFatalError):
            pipeline.run("q")

    def test_rows_shortcut(self):
        pipeline, _, _ = self._pipeline(["SELECT 1"], [ok_result()])
        self.assertEqual(pipeline.run("q").rows, [(1,)])
