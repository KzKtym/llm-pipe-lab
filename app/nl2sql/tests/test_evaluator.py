"""nl2sql/core/evaluator。評価データの読み込みと集計。

パイプラインと executor はダミーに差し替える。ここで確かめたいのは
指標の計算と、正解SQLが壊れていた場合・LLMが止まった場合の扱い。
"""
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from app.common.llm_client import LLMFatalError
from app.nl2sql.core.evaluator import (
    EvalQuestion,
    EvalSummary,
    TagStat,
    dataset_digest,
    evaluate,
    format_summary,
    load_questions,
)
from app.nl2sql.core.pipeline import PipelineResult
from app.nl2sql.core.sql_executor import QueryResult


class FakeExecutor:
    """正解SQLの実行役。SQL文字列をキーに結果を返す。"""

    def __init__(self, results: dict):
        self.results = results
        self.calls = []

    def run(self, sql):
        self.calls.append(sql)
        return self.results.get(sql, QueryResult(ok=True, rows=[], row_count=0))


class FakePipeline:
    """質問文をキーに PipelineResult を返す。fatal を指定すると例外を投げる。"""

    def __init__(self, outcomes: dict, fatal_on: str | None = None):
        self.outcomes = outcomes
        self.fatal_on = fatal_on
        self.calls = []

    def run(self, question):
        self.calls.append(question)
        if question == self.fatal_on:
            raise LLMFatalError("401 Unauthorized")
        return self.outcomes[question]


def question(qid, text, gold, tags=(), ordered=False):
    return EvalQuestion(id=qid, question=text, gold_sql=gold, tags=list(tags), ordered=ordered)


def outcome(rows, *, ok=True, sql="SELECT x", error_kind="", attempts=1):
    return PipelineResult(
        question="",
        ok=ok,
        sql=sql,
        result=QueryResult(ok=ok, rows=rows, row_count=len(rows)) if ok else None,
        attempts=attempts,
        error_kind=error_kind,
        prompt_tokens=100,
        completion_tokens=10,
    )


class LoadQuestionsTests(SimpleTestCase):
    def _write(self, data):
        path = Path(tempfile.mkdtemp()) / "questions.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_loads_all_fields(self):
        path = self._write(
            [
                {
                    "id": "q001",
                    "question": "店舗数は？",
                    "gold_sql": "SELECT count(*) FROM stores",
                    "tags": ["集計"],
                    "note": "閉店店舗を含む",
                }
            ]
        )
        loaded = load_questions(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, "q001")
        self.assertEqual(loaded[0].tags, ["集計"])
        self.assertEqual(loaded[0].note, "閉店店舗を含む")

    def test_ordered_defaults_to_false(self):
        path = self._write([{"question": "q", "gold_sql": "SELECT 1 ORDER BY 1"}])
        self.assertFalse(load_questions(path)[0].ordered)

    def test_ordered_is_loaded(self):
        path = self._write([{"question": "q", "gold_sql": "SELECT 1", "ordered": True}])
        self.assertTrue(load_questions(path)[0].ordered)

    def test_order_is_ignored_when_not_declared(self):
        """並び順を問わない質問では、正解SQLに ORDER BY があっても順序を見ない。"""
        from app.nl2sql.core.sql_executor import QueryResult

        questions = [question("q001", "件数は？", "SELECT a FROM t ORDER BY a DESC")]
        executor = FakeExecutor(
            {"SELECT a FROM t ORDER BY a DESC": QueryResult(ok=True, rows=[(2,), (1,)], row_count=2)}
        )
        pipeline = FakePipeline({"件数は？": outcome([(1,), (2,)])})
        summary, _ = evaluate(questions, pipeline, executor)
        self.assertEqual(summary.correct, 1)

    def test_id_is_generated_when_missing(self):
        path = self._write([{"question": "q", "gold_sql": "SELECT 1"}])
        self.assertEqual(load_questions(path)[0].id, "q001")

    def test_unknown_keys_are_ignored(self):
        path = self._write(
            [{"question": "q", "gold_sql": "SELECT 1", "difficulty": "hard"}]
        )
        self.assertEqual(len(load_questions(path)), 1)

    def test_tags_default_to_empty(self):
        path = self._write([{"question": "q", "gold_sql": "SELECT 1"}])
        self.assertEqual(load_questions(path)[0].tags, [])


class DatasetDigestTests(SimpleTestCase):
    """評価データの版。**版が違う実験のスコアは比較できない**ので、確実に変わること。"""

    def _base(self):
        return [
            question("q001", "店舗数は？", "SELECT count(*) FROM stores", ["集計"]),
            question("q002", "商品数は？", "SELECT count(*) FROM products", ["集計"]),
        ]

    def test_same_content_same_digest(self):
        self.assertEqual(dataset_digest(self._base()), dataset_digest(self._base()))

    def test_order_of_questions_does_not_matter(self):
        reversed_order = list(reversed(self._base()))
        self.assertEqual(dataset_digest(self._base()), dataset_digest(reversed_order))

    def test_gold_sql_change_changes_digest(self):
        changed = self._base()
        changed[0].gold_sql = "SELECT count(1) FROM stores"
        self.assertNotEqual(dataset_digest(self._base()), dataset_digest(changed))

    def test_question_text_change_changes_digest(self):
        changed = self._base()
        changed[0].question = "店舗は何店ある？"
        self.assertNotEqual(dataset_digest(self._base()), dataset_digest(changed))

    def test_ordered_change_changes_digest(self):
        changed = self._base()
        changed[0].ordered = True
        self.assertNotEqual(dataset_digest(self._base()), dataset_digest(changed))

    def test_tag_change_changes_digest(self):
        """タグが変わるとタグ別の内訳が比較できなくなるので、版として扱う。"""
        changed = self._base()
        changed[0].tags = ["集計", "結合"]
        self.assertNotEqual(dataset_digest(self._base()), dataset_digest(changed))

    def test_tag_order_does_not_matter(self):
        a = self._base()
        a[0].tags = ["集計", "結合"]
        b = self._base()
        b[0].tags = ["結合", "集計"]
        self.assertEqual(dataset_digest(a), dataset_digest(b))

    def test_note_change_does_not_change_digest(self):
        """note は解釈の覚え書きで採点に効かない。版を変えない。"""
        changed = self._base()
        changed[0].note = "追記した覚え書き"
        self.assertEqual(dataset_digest(self._base()), dataset_digest(changed))

    def test_adding_a_question_changes_digest(self):
        more = self._base() + [question("q003", "会員数は？", "SELECT count(*) FROM members")]
        self.assertNotEqual(dataset_digest(self._base()), dataset_digest(more))

    def test_empty_list(self):
        self.assertTrue(dataset_digest([]))


class EvaluateTests(SimpleTestCase):
    def test_all_correct(self):
        questions = [
            question("q001", "店舗数は？", "GOLD1", ["集計"]),
            question("q002", "商品数は？", "GOLD2", ["集計"]),
        ]
        executor = FakeExecutor(
            {
                "GOLD1": QueryResult(ok=True, rows=[(40,)], row_count=1),
                "GOLD2": QueryResult(ok=True, rows=[(500,)], row_count=1),
            }
        )
        pipeline = FakePipeline(
            {"店舗数は？": outcome([(40,)]), "商品数は？": outcome([(500,)])}
        )

        summary, items = evaluate(questions, pipeline, executor)

        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.scored, 2)
        self.assertEqual(summary.correct, 2)
        self.assertEqual(summary.execution_accuracy, 1.0)
        self.assertEqual(summary.valid_sql_rate, 1.0)
        self.assertTrue(all(item.match for item in items))

    def test_wrong_value_counts_as_executed_but_not_correct(self):
        questions = [question("q001", "店舗数は？", "GOLD1")]
        executor = FakeExecutor({"GOLD1": QueryResult(ok=True, rows=[(40,)], row_count=1)})
        pipeline = FakePipeline({"店舗数は？": outcome([(39,)])})

        summary, items = evaluate(questions, pipeline, executor)

        self.assertEqual(summary.executed, 1)
        self.assertEqual(summary.correct, 0)
        self.assertEqual(summary.execution_accuracy, 0.0)
        self.assertEqual(summary.valid_sql_rate, 1.0)
        self.assertEqual(items[0].reason, "値が違う")

    def test_unexecutable_sql(self):
        questions = [question("q001", "店舗数は？", "GOLD1")]
        executor = FakeExecutor({"GOLD1": QueryResult(ok=True, rows=[(40,)], row_count=1)})
        pipeline = FakePipeline(
            {"店舗数は？": outcome([], ok=False, error_kind="syntax")}
        )

        summary, items = evaluate(questions, pipeline, executor)

        self.assertEqual(summary.scored, 1)
        self.assertEqual(summary.executed, 0)
        self.assertEqual(summary.valid_sql_rate, 0.0)
        self.assertEqual(summary.by_error_kind, {"syntax": 1})
        self.assertFalse(items[0].match)

    def test_broken_gold_is_excluded_from_scoring(self):
        """正解SQLが流せないのはモデルの責任ではない。分母から外し、件数だけ残す。"""
        questions = [
            question("q001", "壊れた問題", "BROKEN"),
            question("q002", "正しい問題", "GOLD2"),
        ]
        executor = FakeExecutor(
            {
                "BROKEN": QueryResult(ok=False, error="syntax error", error_kind="syntax"),
                "GOLD2": QueryResult(ok=True, rows=[(1,)], row_count=1),
            }
        )
        pipeline = FakePipeline({"正しい問題": outcome([(1,)])})

        summary, items = evaluate(questions, pipeline, executor)

        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.scored, 1)
        self.assertEqual(summary.gold_failed, 1)
        self.assertEqual(summary.execution_accuracy, 1.0)
        self.assertTrue(items[0].gold_failed)
        # 壊れた問題ではパイプラインを動かさない（トークンを無駄にしない）
        self.assertEqual(pipeline.calls, ["正しい問題"])

    def test_tag_breakdown(self):
        questions = [
            question("q001", "a", "G1", ["集計", "表記ゆれ"]),
            question("q002", "b", "G2", ["表記ゆれ"]),
        ]
        executor = FakeExecutor(
            {
                "G1": QueryResult(ok=True, rows=[(1,)], row_count=1),
                "G2": QueryResult(ok=True, rows=[(2,)], row_count=1),
            }
        )
        pipeline = FakePipeline({"a": outcome([(1,)]), "b": outcome([(99,)])})

        summary, _ = evaluate(questions, pipeline, executor)

        self.assertEqual(summary.by_tag["集計"].accuracy, 1.0)
        self.assertEqual(summary.by_tag["表記ゆれ"].accuracy, 0.5)
        self.assertEqual(summary.by_tag["表記ゆれ"].total, 2)

    def test_order_is_compared_only_when_declared(self):
        """正解SQLの ORDER BY ではなく、評価データの ordered で決める。"""
        questions = [question("q001", "多い順に", "SELECT a FROM t ORDER BY a DESC", ordered=True)]
        executor = FakeExecutor(
            {
                "SELECT a FROM t ORDER BY a DESC": QueryResult(
                    ok=True, rows=[(2,), (1,)], row_count=2
                )
            }
        )
        pipeline = FakePipeline({"多い順に": outcome([(1,), (2,)])})

        summary, items = evaluate(questions, pipeline, executor)

        self.assertEqual(summary.correct, 0)
        self.assertEqual(items[0].reason, "行の並び順が違う")

    def test_fatal_error_aborts_and_returns_partial(self):
        questions = [
            question("q001", "最初", "G1"),
            question("q002", "止まる", "G2"),
            question("q003", "届かない", "G3"),
        ]
        executor = FakeExecutor(
            {
                "G1": QueryResult(ok=True, rows=[(1,)], row_count=1),
                "G2": QueryResult(ok=True, rows=[(2,)], row_count=1),
                "G3": QueryResult(ok=True, rows=[(3,)], row_count=1),
            }
        )
        pipeline = FakePipeline({"最初": outcome([(1,)])}, fatal_on="止まる")

        summary, items = evaluate(questions, pipeline, executor)

        self.assertTrue(summary.aborted)
        self.assertIn("401", summary.abort_reason)
        self.assertEqual(summary.scored, 1)
        self.assertEqual(summary.correct, 1)
        self.assertEqual(len(items), 1)
        self.assertNotIn("届かない", pipeline.calls)

    def test_tokens_are_summed(self):
        questions = [question("q001", "a", "G1"), question("q002", "b", "G2")]
        executor = FakeExecutor(
            {
                "G1": QueryResult(ok=True, rows=[(1,)], row_count=1),
                "G2": QueryResult(ok=True, rows=[(2,)], row_count=1),
            }
        )
        pipeline = FakePipeline({"a": outcome([(1,)]), "b": outcome([(2,)])})

        summary, _ = evaluate(questions, pipeline, executor)

        self.assertEqual(summary.prompt_tokens, 200)
        self.assertEqual(summary.total_tokens, 220)

    def test_pause_sec_waits_between_questions(self):
        """1問目では待たず、2問目以降だけ待つ。"""
        from unittest.mock import patch

        questions = [question("q001", "a", "G1"), question("q002", "b", "G2")]
        executor = FakeExecutor(
            {
                "G1": QueryResult(ok=True, rows=[(1,)], row_count=1),
                "G2": QueryResult(ok=True, rows=[(2,)], row_count=1),
            }
        )
        pipeline = FakePipeline({"a": outcome([(1,)]), "b": outcome([(2,)])})

        with patch("app.nl2sql.core.evaluator.time.sleep") as sleep:
            evaluate(questions, pipeline, executor, pause_sec=1.5)

        sleep.assert_called_once_with(1.5)

    def test_no_pause_by_default(self):
        from unittest.mock import patch

        questions = [question("q001", "a", "G1"), question("q002", "b", "G2")]
        executor = FakeExecutor({"G1": QueryResult(ok=True, rows=[], row_count=0),
                                 "G2": QueryResult(ok=True, rows=[], row_count=0)})
        pipeline = FakePipeline({"a": outcome([]), "b": outcome([])})

        with patch("app.nl2sql.core.evaluator.time.sleep") as sleep:
            evaluate(questions, pipeline, executor)

        sleep.assert_not_called()

    def test_empty_question_list(self):
        summary, items = evaluate([], FakePipeline({}), FakeExecutor({}))
        self.assertEqual(summary.execution_accuracy, 0.0)
        self.assertEqual(items, [])


class FormatSummaryTests(SimpleTestCase):
    def test_includes_main_metrics(self):
        summary = EvalSummary(total=10, scored=10, executed=9, correct=6)
        text = format_summary(summary)
        self.assertIn("実行結果一致率: 0.6", text)
        self.assertIn("実行成功率: 0.9", text)

    def test_tag_breakdown_is_listed(self):
        summary = EvalSummary(scored=2, executed=2, correct=1)
        summary.by_tag = {"表記ゆれ": TagStat(correct=0, total=1), "集計": TagStat(1, 1)}
        text = format_summary(summary)
        self.assertIn("タグ別:", text)
        self.assertIn("表記ゆれ: 0.0", text)

    def test_gold_failure_is_surfaced(self):
        summary = EvalSummary(total=10, scored=9, gold_failed=1)
        self.assertIn("評価データの不備", format_summary(summary))

    def test_abort_is_surfaced(self):
        summary = EvalSummary(aborted=True, abort_reason="401")
        self.assertIn("中断", format_summary(summary))
