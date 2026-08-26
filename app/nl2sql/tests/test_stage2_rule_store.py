"""nl2sql/stage2_rule_store。段階2ルール判定版の明細保存・読込。"""
import tempfile

from django.test import SimpleTestCase, override_settings

from app.nl2sql import stage2_rule_store
from app.nl2sql.core.stage2_rule_evaluator import RuleEvalItem


@override_settings(NL2SQL_STAGE2_RULE_LOGS_DIR=tempfile.mkdtemp())
class SaveReadDetailsTests(SimpleTestCase):
    def test_round_trip(self):
        items = [RuleEvalItem(id="q1", question="q", needs_clarification=True)]
        stage2_rule_store.save_details(1, items, meta={"dataset_digest": "abc"})

        payload = stage2_rule_store.read_details(1)
        self.assertEqual(payload["meta"]["dataset_digest"], "abc")
        self.assertEqual(payload["items"][0]["id"], "q1")

    def test_missing_returns_none(self):
        self.assertIsNone(stage2_rule_store.read_details(999999))

    def test_logs_dir_is_separate_from_other_stage2_logs(self):
        from app.nl2sql import stage2_store

        self.assertNotEqual(stage2_rule_store.get_logs_dir(), stage2_store.get_logs_dir())
