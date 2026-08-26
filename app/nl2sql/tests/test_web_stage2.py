"""層: web（段階2の振る舞いページ）。issue_v005。

確かめたいのは2点。

- **`disclosure_text` を1文字も変えずに出していること。** 列名の混入
  （`stores.close_date` 等）は工程②で直すもので、画面側で隠すと直ったかどうかが
  画面から分からなくなる
- **見せる問いを番号で選んでいないこと。** 種類ごとに先頭から2件ずつ、という
  規則だけで決まる

実行:

    TEST_DB_NAME=test_llm_pipe_lab_v .venv/bin/python manage.py test app.nl2sql
"""
import json
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from app.nl2sql import stage2_rule_store
from app.nl2sql.models import Nl2SqlStage2RuleExperiment
from app.nl2sql.web.services import stage2_service

#: 明細は実ファイルを読む。実データの `data/nl2sql_stage2_rule/logs/` を
#: 覗きに行かないようテンポラリへ逃がす
LOGS_DIR = tempfile.mkdtemp(prefix="nl2sql-test-stage2-logs-")

#: 実データ（exp_3 / s201）と同じ形。列名の混入と、行末の空白＋改行を含む
RAW_DISCLOSURE = (
    "開示する既定として、stores.close_dateは「全部含める」を適用しています。  \n"
    "店舗の母集合（結合欠落）について、質問文が確定していないため、"
    "詳細を教えていただけますか。"
)


def make_experiment(**kwargs):
    defaults = {
        "name": "段階2ルール判定",
        "parameters": {"model": "openai:gpt-4.1-mini"},
        "dataset_digest": "f8d9e34b" + "0" * 56,
        "dataset_path": "sample/sales/rule_validation_questions.json",
        "question_count": 3,
        "judged": 3,
    }
    return Nl2SqlStage2RuleExperiment.objects.create(**{**defaults, **kwargs})


def item(item_id, *, question=None, disclosure="", needs_clarification=False):
    """明細1件。内部の分類（axes / aggregation）も実データどおり持たせる。

    この画面に出してはいけない値なので、漏れていないことを確かめるために入れておく。
    """
    return {
        "id": item_id,
        "question": question or f"{item_id} の質問です。",
        "aggregation": "sum(sales_amount)",
        "group_by": ["store_id"],
        "axes": [{"name": "店舗の母集合", "type": 1, "resolved": False}],
        "needs_clarification": needs_clarification,
        "disclosure_text": disclosure,
        "raw_extraction_response": "{...}",
        "raw_phrasing_response": "...",
        "error": "",
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "latency_ms": 900,
    }


def write_details(experiment_id, items):
    path = stage2_rule_store.details_path(experiment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"meta": {}, "items": items}, ensure_ascii=False), encoding="utf-8"
    )


def sample_items(prefix="s"):
    """種類ごとに上限（2件）を超える並び。

    聞き返し3・補う前提なし1・開示あり3。既定では各種2件までなので
    `s03` と `s07` が落ちる。
    """
    return [
        item(f"{prefix}01", needs_clarification=True, disclosure=RAW_DISCLOSURE),
        item(f"{prefix}02", needs_clarification=True, disclosure="別の聞き返し。"),
        item(f"{prefix}03", needs_clarification=True, disclosure="3件目の聞き返し。"),
        item(f"{prefix}04"),
        item(f"{prefix}05", disclosure="channelの表記ゆれは「統合する」を適用しています。"),
        item(f"{prefix}06", disclosure="2件目の開示。"),
        item(f"{prefix}07", disclosure="3件目の開示。"),
    ]


class WalkthroughSliceTest(TestCase):
    """見せる問いの選び方。**番号で指定せず、種類ごとに2件ずつ切る。**"""

    def test_takes_two_of_each_kind(self):
        taken = stage2_service.walkthrough_slice(sample_items())
        kinds = [stage2_service.kind_of(row) for row in taken]
        for kind in stage2_service.ALL_KINDS:
            self.assertLessEqual(kinds.count(kind), stage2_service.PER_KIND)
        self.assertEqual(kinds.count(stage2_service.KIND_CLARIFICATION), 2)
        self.assertEqual(kinds.count(stage2_service.KIND_DISCLOSED), 2)

    def test_drops_the_third_of_a_kind(self):
        # 並び順の偏りで1種類に埋まらないこと。s03（3件目の聞き返し）は落ちる
        taken = stage2_service.walkthrough_slice(sample_items())
        self.assertEqual([row["id"] for row in taken], ["s01", "s02", "s04", "s05", "s06"])

    def test_keeps_the_original_order(self):
        # 種類でまとめ直さない。明細の並びのまま
        taken = stage2_service.walkthrough_slice(sample_items())
        ids = [row["id"] for row in taken]
        self.assertEqual(ids, sorted(ids))

    def test_takes_everything_below_the_cap(self):
        items = [item("s01", needs_clarification=True), item("s02")]
        self.assertEqual(len(stage2_service.walkthrough_slice(items)), 2)

    def test_returns_everything_when_a_kind_never_appears(self):
        # 足りないものは足せない。切らずに全部返す
        items = [item("s01", needs_clarification=True), item("s02")]
        self.assertEqual(len(stage2_service.walkthrough_slice(items)), 2)

    def test_empty_items(self):
        self.assertEqual(stage2_service.walkthrough_slice([]), [])

    def test_kind_of_each_case(self):
        self.assertEqual(
            stage2_service.kind_of(item("a", needs_clarification=True)),
            stage2_service.KIND_CLARIFICATION,
        )
        self.assertEqual(
            stage2_service.kind_of(item("b", disclosure="既定を適用しました。")),
            stage2_service.KIND_DISCLOSED,
        )
        self.assertEqual(
            stage2_service.kind_of(item("c")), stage2_service.KIND_NO_ASSUMPTION
        )

    def test_whitespace_only_disclosure_is_not_a_disclosure(self):
        self.assertEqual(
            stage2_service.kind_of(item("d", disclosure="   \n  ")),
            stage2_service.KIND_NO_ASSUMPTION,
        )


@override_settings(NL2SQL_STAGE2_RULE_LOGS_DIR=LOGS_DIR)
class Stage2PageTest(TestCase):
    def setUp(self):
        for path in Path(LOGS_DIR).glob("*.json"):
            path.unlink()
        self.exp = make_experiment(name="第3回")
        write_details(self.exp.id, sample_items())

    def test_default_url_opens_with_the_latest_experiment(self):
        latest = make_experiment(name="もっと新しい")
        write_details(latest.id, sample_items())
        res = self.client.get(reverse("nl2sql_stage2"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context["experiment"].id, latest.id)

    def test_experiment_is_selectable_by_url(self):
        make_experiment(name="もっと新しい")
        res = self.client.get(reverse("nl2sql_stage2_experiment", args=[self.exp.id]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context["experiment"].id, self.exp.id)
        self.assertContains(res, "第3回")

    def test_missing_experiment_returns_404(self):
        res = self.client.get(reverse("nl2sql_stage2_experiment", args=[9999]))
        self.assertEqual(res.status_code, 404)

    def test_no_experiment_at_all_does_not_break(self):
        Nl2SqlStage2RuleExperiment.objects.all().delete()
        res = self.client.get(reverse("nl2sql_stage2"))
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.context["experiment"])
        self.assertContains(res, "実験がまだありません")

    def test_missing_details_file_does_not_break(self):
        stage2_rule_store.details_path(self.exp.id).unlink()
        res = self.client.get(reverse("nl2sql_stage2_experiment", args=[self.exp.id]))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.context["details_missing"])
        self.assertContains(res, "明細ファイルがありません")

    def test_shows_both_clarification_and_disclosure(self):
        res = self.client.get(reverse("nl2sql_stage2_experiment", args=[self.exp.id]))
        self.assertContains(res, "聞き返しが必要")
        self.assertContains(res, "前提を開示して回答")

    def test_shows_a_question_without_any_assumption(self):
        # 何も出ないと壊れて見えるので、空であることが分かる表示にする
        res = self.client.get(reverse("nl2sql_stage2_experiment", args=[self.exp.id]))
        self.assertContains(res, "補った前提はありません")

    def test_default_view_shows_two_of_each_kind(self):
        res = self.client.get(reverse("nl2sql_stage2_experiment", args=[self.exp.id]))
        self.assertEqual(res.context["shown_count"], 5)
        self.assertEqual(res.context["total_count"], 7)
        self.assertNotContains(res, "3件目の聞き返し")
        self.assertNotContains(res, "3件目の開示")

    def test_all_query_shows_every_question(self):
        res = self.client.get(
            reverse("nl2sql_stage2_experiment", args=[self.exp.id]), {"all": "1"}
        )
        self.assertEqual(res.context["shown_count"], 7)
        self.assertContains(res, "3件目の開示")

    def test_other_query_values_do_not_expand(self):
        res = self.client.get(
            reverse("nl2sql_stage2_experiment", args=[self.exp.id]), {"all": "0"}
        )
        self.assertEqual(res.context["shown_count"], 5)


@override_settings(NL2SQL_STAGE2_RULE_LOGS_DIR=LOGS_DIR)
class DisclosureIsUntouchedTest(TestCase):
    """**開示文に手を入れないこと**（issue_v005 の最重要制約）。"""

    def setUp(self):
        for path in Path(LOGS_DIR).glob("*.json"):
            path.unlink()
        self.exp = make_experiment()
        write_details(self.exp.id, sample_items())

    def _rows(self):
        res = self.client.get(reverse("nl2sql_stage2_experiment", args=[self.exp.id]))
        return res, res.context["rows"]

    def test_service_passes_the_string_through_unchanged(self):
        _, rows = self._rows()
        self.assertEqual(rows[0]["disclosure_text"], RAW_DISCLOSURE)

    def test_column_names_are_not_hidden(self):
        # issue_c018 で直った結果がそのまま画面に出る形にする。画面側では隠さない
        res, _ = self._rows()
        self.assertContains(res, "stores.close_date")

    def test_rendered_page_contains_the_string_verbatim(self):
        # 行末の空白と改行まで含めてそのまま。分割も整形もしていない
        res, _ = self._rows()
        self.assertContains(res, RAW_DISCLOSURE)

    def test_newlines_are_not_turned_into_markup(self):
        # linebreaks フィルタを通すと画面側の整形になる。改行は CSS で見せる
        res, _ = self._rows()
        self.assertNotContains(res, "<br")

    def test_internal_classification_is_not_shown(self):
        # 型・軸・集計は開発側が見るもの。この画面には出さない（nl2sql_detail の話）
        # なお開示文そのものに分類語が混ざっている分は素通しする（issue_c018 の担当）。
        # ここで見るのは、画面が明細の内部フィールドを出していないこと
        res, _ = self._rows()
        for leaked in ("aggregation", "group_by", "raw_extraction_response", "resolved"):
            self.assertNotContains(res, leaked)
