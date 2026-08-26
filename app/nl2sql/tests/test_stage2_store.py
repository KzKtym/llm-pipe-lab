"""nl2sql/stage2_store。段階2明細の保存・読込・第2軸の再集計。"""
import tempfile

from django.test import SimpleTestCase, override_settings

from app.nl2sql import stage2_store
from app.nl2sql.core.stage2_evaluator import Stage2Item


@override_settings(NL2SQL_STAGE2_LOGS_DIR=tempfile.mkdtemp())
class SaveReadDetailsTests(SimpleTestCase):
    def test_round_trip(self):
        items = [
            Stage2Item(id="s001", question="q", gold_is_ambiguous=True, confusion="TP"),
        ]
        stage2_store.save_details(1, items, meta={"dataset_digest": "abc"})

        payload = stage2_store.read_details(1)
        self.assertEqual(payload["meta"]["dataset_digest"], "abc")
        self.assertEqual(payload["items"][0]["id"], "s001")

    def test_missing_returns_none(self):
        self.assertIsNone(stage2_store.read_details(999999))

    def test_logs_dir_is_separate_from_stage1(self):
        """段階1の NL2SQL_LOGS_DIR とは別の設定キーを見る。"""
        from app.nl2sql import store as stage1_store

        self.assertNotEqual(stage2_store.get_logs_dir(), stage1_store.get_logs_dir())


@override_settings(NL2SQL_STAGE2_LOGS_DIR=tempfile.mkdtemp())
class RecomputeAxis2Tests(SimpleTestCase):
    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            stage2_store.recompute_axis2(999999)

    def test_counts_only_tp_items(self):
        items = [
            Stage2Item(id="s001", question="q", gold_is_ambiguous=True, confusion="TP",
                       keyword_match=True, human_correct=True),
            Stage2Item(id="s002", question="q", gold_is_ambiguous=True, confusion="FN"),
            Stage2Item(id="c001", question="q", gold_is_ambiguous=False, confusion="TN"),
        ]
        stage2_store.save_details(2, items)

        result = stage2_store.recompute_axis2(2)
        self.assertEqual(result["axis2_human_reviewed"], 1)
        self.assertEqual(result["axis2_human_correct"], 1)
        self.assertEqual(result["axis2_disagreement"], 0)

    def test_unreviewed_items_are_not_counted(self):
        """human_correct が None（未判定）の問題は reviewed に数えない。"""
        items = [
            Stage2Item(id="s001", question="q", gold_is_ambiguous=True, confusion="TP",
                       keyword_match=True, human_correct=None),
        ]
        stage2_store.save_details(3, items)

        result = stage2_store.recompute_axis2(3)
        self.assertEqual(result["axis2_human_reviewed"], 0)
        self.assertEqual(result["axis2_disagreement"], 0)

    def test_disagreement_between_keyword_and_human(self):
        """キーワード一致と人の判定が食い違った件数を数える。"""
        items = [
            Stage2Item(id="s001", question="q", gold_is_ambiguous=True, confusion="TP",
                       keyword_match=True, human_correct=False),
            Stage2Item(id="s002", question="q", gold_is_ambiguous=True, confusion="TP",
                       keyword_match=False, human_correct=True),
            Stage2Item(id="s003", question="q", gold_is_ambiguous=True, confusion="TP",
                       keyword_match=True, human_correct=True),
        ]
        stage2_store.save_details(4, items)

        result = stage2_store.recompute_axis2(4)
        self.assertEqual(result["axis2_human_reviewed"], 3)
        self.assertEqual(result["axis2_human_correct"], 2)
        self.assertEqual(result["axis2_disagreement"], 2)
