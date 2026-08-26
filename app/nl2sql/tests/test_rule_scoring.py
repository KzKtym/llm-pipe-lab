"""nl2sql/core/rule_scoring。`issue_c017`：採点がコードで再現できることを確かめる。

`population_rules.py` / `axis_extractor.py` には触れない。フェイクの `RuleEvalItem` で
突き合わせの算術（完全一致・聞き返すか否か・見逃し／過検出の型別集計・除外）だけを確認する。
"""
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from app.nl2sql.core.rule_scoring import (
    GoldLabel,
    detected_types,
    format_score,
    items_from_details,
    load_gold,
    score,
)
from app.nl2sql.core.stage2_rule_evaluator import RuleEvalItem


def item(id_, question="", axes=()):
    return RuleEvalItem(id=id_, question=question, axes=list(axes))


def axis(kind, resolved):
    return {"name": "x", "kind": kind, "resolved": resolved, "default": ""}


class DetectedTypesTests(SimpleTestCase):
    def test_only_unresolved_axes_are_detected(self):
        i = item("q1", axes=[axis(1, False), axis(2, True), axis(3, False)])
        self.assertEqual(detected_types(i), frozenset({1, 3}))

    def test_no_axes_means_empty(self):
        self.assertEqual(detected_types(item("q1")), frozenset())


class ScoreTests(SimpleTestCase):
    def setUp(self):
        self.gold = {
            "q1": GoldLabel(id="q1", unique=False, types=frozenset({1, 2})),
            "q2": GoldLabel(id="q2", unique=True, types=frozenset()),
            "q3": GoldLabel(id="q3", unique=False, types=frozenset({1})),
        }

    def test_exact_match(self):
        items = [item("q1", axes=[axis(1, False), axis(2, False)])]
        result = score(items, self.gold)
        self.assertEqual(result.total, 1)
        self.assertEqual(result.exact_match, 1)
        self.assertEqual(result.ask_or_not_match, 1)
        self.assertEqual(result.missed_by_type, {1: 0, 2: 0, 3: 0, 4: 0})
        self.assertEqual(result.overdetected_by_type, {1: 0, 2: 0, 3: 0, 4: 0})
        self.assertEqual(result.mismatches, [])

    def test_miss_is_counted_by_type(self):
        # gold={1,2} だが型2しか検出できなかった → 型1の見逃し
        items = [item("q1", axes=[axis(2, False)])]
        result = score(items, self.gold)
        self.assertEqual(result.exact_match, 0)
        self.assertFalse(result.ask_or_not_match)  # gold は型1あり、判定は型1なし
        self.assertEqual(result.missed_by_type[1], 1)
        self.assertEqual(result.missed_by_type[2], 0)
        self.assertEqual(result.overdetected_by_type, {1: 0, 2: 0, 3: 0, 4: 0})
        self.assertEqual(len(result.mismatches), 1)
        self.assertEqual(result.mismatches[0].missed, frozenset({1}))

    def test_overdetection_is_counted_by_type(self):
        # gold=[]（q2）だが型3を検出した → 型3の過検出
        items = [item("q2", axes=[axis(3, False)])]
        result = score(items, self.gold)
        self.assertEqual(result.exact_match, 0)
        self.assertTrue(result.ask_or_not_match)  # どちらも型1は無い
        self.assertEqual(result.overdetected_by_type[3], 1)
        self.assertEqual(result.missed_by_type, {1: 0, 2: 0, 3: 0, 4: 0})

    def test_ask_or_not_mismatch(self):
        # gold は型1あり(q3)、判定は型1なし
        items = [item("q3", axes=[axis(2, False)])]
        result = score(items, self.gold)
        self.assertFalse(result.ask_or_not_match)
        self.assertEqual(result.missed_by_type[1], 1)
        self.assertEqual(result.overdetected_by_type[2], 1)

    def test_exclude_ids_removes_item_from_all_counts(self):
        items = [
            item("q1", axes=[axis(1, False), axis(2, False)]),
            item("q3", axes=[axis(2, False)]),  # 見逃し1件を含む
        ]
        full = score(items, self.gold)
        excluded = score(items, self.gold, exclude_ids=frozenset({"q3"}))
        self.assertEqual(full.total, 2)
        self.assertEqual(excluded.total, 1)
        self.assertEqual(excluded.exact_match, 1)
        self.assertEqual(excluded.missed_by_type[1], 0)

    def test_id_not_in_gold_is_reported_not_silently_dropped(self):
        items = [item("unknown", axes=[])]
        result = score(items, self.gold)
        self.assertEqual(result.total, 0)
        self.assertEqual(result.unscored_ids, ["unknown"])

    def test_same_input_gives_same_numbers(self):
        items = [
            item("q1", axes=[axis(1, False), axis(2, True)]),
            item("q3", axes=[axis(1, True)]),
        ]
        first = score(items, self.gold)
        second = score(items, self.gold)
        self.assertEqual(first.exact_match, second.exact_match)
        self.assertEqual(first.missed_by_type, second.missed_by_type)
        self.assertEqual(first.overdetected_by_type, second.overdetected_by_type)


class LoadGoldTests(SimpleTestCase):
    def test_reads_id_unique_types(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gold.json"
            path.write_text(
                '[{"id": "s201", "unique": false, "types": [1, 2], "basis": "x"}]',
                encoding="utf-8",
            )
            gold = load_gold(path)
        self.assertEqual(set(gold), {"s201"})
        self.assertFalse(gold["s201"].unique)
        self.assertEqual(gold["s201"].types, frozenset({1, 2}))


class ItemsFromDetailsTests(SimpleTestCase):
    def test_round_trips_saved_details(self):
        from dataclasses import asdict

        original = [item("q1", question="text", axes=[axis(1, False)])]
        details = {"meta": {}, "items": [asdict(i) for i in original]}
        restored = items_from_details(details)
        self.assertEqual(restored, original)


class FormatScoreTests(SimpleTestCase):
    def test_includes_counts(self):
        summary = score(
            [item("q1", axes=[axis(1, False), axis(2, False)])],
            {"q1": GoldLabel(id="q1", unique=False, types=frozenset({1, 2}))},
        )
        text = format_score(summary, label="22件・")
        self.assertIn("22件・件数: 1", text)
        self.assertIn("型の完全一致: 1/1", text)
