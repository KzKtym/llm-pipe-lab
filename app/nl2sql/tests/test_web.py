"""層: web（views / web.services）。閲覧のみの3画面。

明細はファイルにあるので `read_details` を差し替えて回す。ここで確かめたいのは、
**明細が無くても落ちないこと**と、**比較の勝敗が正しく4区分に分かれること**。
後者がこのツールの主目的で、区分を間違えると「パラメータを変えた効果」を
逆に読むことになる。

素の設定で回す。外部APIもベクタ索引も要らない。

    .venv/bin/python manage.py test app.nl2sql
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from app.nl2sql.models import Nl2SqlExperiment
from app.nl2sql.web.services import compare_service, detail_service, list_service

#: 一覧は明細ファイルの**有無**を実際に見る（`store.details_path().exists()`）。
#: 実データの `data/nl2sql/logs/` を覗きに行かないよう、テンポラリへ逃がす
LOGS_DIR = tempfile.mkdtemp(prefix="nl2sql-test-web-logs-")

PARAMS = {
    "schema": "demo_sales",
    "schema_mode": "full",
    "schema_desc": "comment",
    "few_shot": 0,
    "model": "openai:gpt-4.1-mini",
    "temperature": 0.0,
}


#: 評価データの版。実データと同じく先頭8桁で見分ける（sha256 の64桁）
DIGEST_A = "d7402f21" + "0" * 56
DIGEST_B = "ef3b87a5" + "1" * 56


def make_experiment(**kwargs):
    defaults = {
        "name": "実験",
        "parameters": dict(PARAMS),
        "dataset_digest": DIGEST_A,
        "dataset_path": "sample/sales/evaluation_questions.json",
        "schema_snapshot": "CREATE TABLE demo_sales.stores (...)",
        "prompt_sample": "スキーマ:\n...",
        "question_count": 4,
        "scored": 4,
        "executed": 4,
        "correct": 2,
        "execution_accuracy": 0.5,
        "valid_sql_rate": 1.0,
        "by_tag": {"集計": {"correct": 2, "total": 2}, "結合": {"correct": 0, "total": 2}},
        "by_error_kind": {"undefined": 1},
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "elapsed_sec": 12.5,
    }
    return Nl2SqlExperiment.objects.create(**{**defaults, **kwargs})


def item(item_id, *, match=False, executed=True, gold_failed=False, **kwargs):
    base = {
        "id": item_id,
        "question": f"{item_id} の質問",
        "tags": ["集計"],
        "gold_sql": "SELECT 1",
        "ordered": False,
        "note": "",
        "generated_sql": f"SELECT '{item_id}'",
        "executed": executed,
        "match": match,
        "reason": "値が違う" if not match else "",
        "error": "",
        "error_kind": "",
        "attempts": 1,
        "gold_failed": gold_failed,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "latency_ms": 900,
    }
    base.update(kwargs)
    return base


def payload(*items):
    return {"meta": {}, "items": list(items)}


def patch_details(mapping):
    """実験IDごとの明細を返す差し替え。`mapping` に無いIDは None（ファイル無し）。"""
    return patch.object(
        detail_service, "read_details", side_effect=lambda exp_id: mapping.get(exp_id)
    ), patch.object(
        compare_service, "read_details", side_effect=lambda exp_id: mapping.get(exp_id)
    )


@override_settings(NL2SQL_LOGS_DIR=LOGS_DIR)
class ListViewTest(TestCase):
    def setUp(self):
        for path in Path(LOGS_DIR).glob("*.json"):
            path.unlink()

    def write_details(self, experiment_id):
        """明細ファイルを置く。一覧が見るのは有無だけなので中身は問わない。"""
        (Path(LOGS_DIR) / f"exp_{experiment_id}.json").write_text("{}", encoding="utf-8")

    def test_renders(self):
        make_experiment(name="一覧の実験")
        res = self.client.get(reverse("nl2sql_list"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "一覧の実験")

    def test_empty_list_does_not_break(self):
        res = self.client.get(reverse("nl2sql_list"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "実験がまだありません")

    def test_shows_parameter_line(self):
        make_experiment()
        res = self.client.get(reverse("nl2sql_list"))
        self.assertContains(res, "schema_mode: full")

    def test_shows_scored_over_question_count(self):
        make_experiment(scored=3, question_count=4)
        self.assertContains(self.client.get(reverse("nl2sql_list")), "3/4")

    def test_gold_failed_warning_is_shown(self):
        make_experiment(gold_failed=2)
        res = self.client.get(reverse("nl2sql_list"))
        self.assertContains(res, "正解SQLが実行できなかった問題が 2 件あります")

    def test_aborted_warning_is_shown(self):
        make_experiment(aborted=True, abort_reason="401 Unauthorized")
        res = self.client.get(reverse("nl2sql_list"))
        self.assertContains(res, "途中で打ち切られました")
        self.assertContains(res, "401 Unauthorized")

    def test_no_warning_when_clean(self):
        make_experiment()
        self.assertNotContains(self.client.get(reverse("nl2sql_list")), "⚠")

    def test_star_is_not_displayed(self):
        # 操作できないものを見せると紛らわしいため出さない。フィールドは残っている
        make_experiment(is_starred=True)
        self.assertNotContains(self.client.get(reverse("nl2sql_list")), "★")

    def test_newest_first(self):
        old = make_experiment(name="古い")
        new = make_experiment(name="新しい")
        rows = self.client.get(reverse("nl2sql_list")).context["rows"]
        self.assertEqual([row.experiment.id for row in rows], [new.id, old.id])

    def test_dataset_version_is_shown(self):
        make_experiment(dataset_digest=DIGEST_B)
        self.assertContains(self.client.get(reverse("nl2sql_list")), "ef3b87a5")

    def test_unrecorded_dataset_is_labelled(self):
        # ID 3〜8 は記録を始める前の実験。遡って特定する手段が無い
        make_experiment(dataset_digest="")
        self.assertContains(self.client.get(reverse("nl2sql_list")), "(記録前)")

    def test_same_version_gets_the_same_colour(self):
        make_experiment(dataset_digest=DIGEST_A)
        make_experiment(dataset_digest=DIGEST_A)
        make_experiment(dataset_digest=DIGEST_B)
        hues = {}
        for row in self.client.get(reverse("nl2sql_list")).context["rows"]:
            hues.setdefault(row.experiment.dataset_digest, set()).add(row.dataset_hue)
        self.assertEqual(len(hues[DIGEST_A]), 1)
        self.assertNotEqual(hues[DIGEST_A], hues[DIGEST_B])

    def test_unrecorded_dataset_has_no_colour(self):
        make_experiment(dataset_digest="")
        row = self.client.get(reverse("nl2sql_list")).context["rows"][0]
        self.assertIsNone(row.dataset_hue)

    def test_details_presence_per_row(self):
        # 明細が無い実験は比較で勝敗が出せない。選ぶ前に分かること
        with_file = make_experiment(name="明細あり")
        without_file = make_experiment(name="明細なし")
        self.write_details(with_file.id)

        rows = {
            row.experiment.id: row.has_details
            for row in self.client.get(reverse("nl2sql_list")).context["rows"]
        }
        self.assertTrue(rows[with_file.id])
        self.assertFalse(rows[without_file.id])

    def test_missing_details_are_counted_and_announced(self):
        make_experiment()
        res = self.client.get(reverse("nl2sql_list"))
        self.assertEqual(res.context["missing_details_count"], 1)
        self.assertContains(res, "明細ファイルが無い実験が 1 件あります")

    def test_no_notice_when_all_have_details(self):
        exp = make_experiment()
        self.write_details(exp.id)
        res = self.client.get(reverse("nl2sql_list"))
        self.assertEqual(res.context["missing_details_count"], 0)
        self.assertNotContains(res, "明細ファイルが無い実験が")


class ParameterLineTest(TestCase):
    def test_uses_params_summary_line(self):
        self.assertIn("few_shot: 0", list_service.parameter_line(PARAMS))

    def test_broken_parameters_fall_back_to_raw(self):
        # 仕様変更前の記録や手で書き換えた記録が混じっても一覧を落とさない
        line = list_service.parameter_line({"schema_desc": "存在しない値", "few_shot": 1})
        self.assertIn("schema_desc: 存在しない値", line)

    def test_empty_parameters(self):
        self.assertIsInstance(list_service.parameter_line({}), str)


class VaryingParameterTest(TestCase):
    """一覧のパラメータ列は、実験間で差のあるキーだけを前に出す。

    どのキーを隠すかは決め打ちにしない。何を振るかは実験のたびに変わるため。
    """

    def test_only_differing_keys(self):
        keys = list_service.varying_parameter_keys([
            {"schema_desc": "comment", "temperature": 0.0},
            {"schema_desc": "none", "temperature": 0.0},
        ])
        self.assertEqual(keys, {"schema_desc"})

    def test_single_experiment_folds_nothing(self):
        # 比べる相手がいなければ畳まない（全部見せる）
        self.assertEqual(list_service.varying_parameter_keys([{"schema_desc": "none"}]), set())

    def test_missing_key_counts_as_difference(self):
        keys = list_service.varying_parameter_keys([{"few_shot": 0}, {}])
        self.assertEqual(keys, {"few_shot"})

    def test_line_uses_params_field_order(self):
        line = list_service.varying_parameter_line(
            {"few_shot": 3, "schema_desc": "hint"}, {"few_shot", "schema_desc"}
        )
        self.assertEqual(line, "schema_desc: hint, few_shot: 3")

    def test_line_marks_unrecorded_key(self):
        line = list_service.varying_parameter_line({}, {"few_shot"})
        self.assertEqual(line, f"few_shot: {list_service.UNRECORDED}")

    def test_line_is_empty_without_differences(self):
        self.assertEqual(list_service.varying_parameter_line({"few_shot": 0}, set()), "")


@override_settings(NL2SQL_LOGS_DIR=LOGS_DIR)
class TemplateCommentTest(TestCase):
    """テンプレートコメントが画面へ漏れないこと（issue_v002）。

    Django の `{# #}` は**1行専用**で、複数行に書くとコメントにならずそのまま出る。
    実際に一覧の名前の列へ漏れていた。
    """

    def test_short_comments_close_on_the_same_line(self):
        # 描画されない分岐も含めて、記述そのものを検査する
        for path in sorted(Path(settings.BASE_DIR, "templates", "nl2sql").glob("*.html")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "{#" in line:
                    self.assertIn("#}", line, f"{path.name}:{number} の {{# #}} が閉じていません")

    def test_no_leak_on_any_screen(self):
        exp_a = make_experiment()
        exp_b = make_experiment()
        with patch.object(detail_service, "read_details", return_value=payload(item("q1"))), \
             patch.object(compare_service, "read_details", return_value=payload(item("q1"))):
            bodies = [
                self.client.get(reverse("nl2sql_list")).content.decode(),
                self.client.get(reverse("nl2sql_detail", args=[exp_a.id])).content.decode(),
                self.client.get(reverse("nl2sql_compare"),
                                {"a": exp_a.id, "b": exp_b.id}).content.decode(),
            ]
        for body in bodies:
            self.assertNotIn("{#", body)
            self.assertNotIn("#}", body)


class DetailViewTest(TestCase):
    def setUp(self):
        self.exp = make_experiment()

    def test_renders(self):
        with patch.object(detail_service, "read_details",
                          return_value=payload(item("q001", match=True))):
            res = self.client.get(reverse("nl2sql_detail", args=[self.exp.id]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "q001 の質問")

    def test_missing_experiment_returns_404(self):
        with patch.object(detail_service, "read_details", return_value=None):
            res = self.client.get(reverse("nl2sql_detail", args=[9999]))
        self.assertEqual(res.status_code, 404)

    def test_missing_details_file_still_renders_summary(self):
        with patch.object(detail_service, "read_details", return_value=None):
            res = self.client.get(reverse("nl2sql_detail", args=[self.exp.id]))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.context["details_missing"])
        self.assertContains(res, "明細ファイルがありません")

    def test_gold_failed_warning_is_shown(self):
        self.exp.gold_failed = 1
        self.exp.save()
        with patch.object(detail_service, "read_details", return_value=None):
            res = self.client.get(reverse("nl2sql_detail", args=[self.exp.id]))
        self.assertContains(res, "正解SQLが実行できなかった問題が 1 件あります")

    def test_dataset_version_and_path_are_shown(self):
        self.exp.dataset_digest = DIGEST_B
        self.exp.dataset_path = "sample/sales/evaluation_questions.json"
        self.exp.save()
        with patch.object(detail_service, "read_details", return_value=None):
            res = self.client.get(reverse("nl2sql_detail", args=[self.exp.id]))
        self.assertContains(res, "ef3b87a5")
        self.assertContains(res, "sample/sales/evaluation_questions.json")

    def test_unrecorded_dataset_is_labelled(self):
        self.exp.dataset_digest = ""
        self.exp.save()
        with patch.object(detail_service, "read_details", return_value=None):
            res = self.client.get(reverse("nl2sql_detail", args=[self.exp.id]))
        self.assertContains(res, "(記録前)")

    def test_schema_snapshot_is_collapsed_not_omitted(self):
        with patch.object(detail_service, "read_details", return_value=None):
            res = self.client.get(reverse("nl2sql_detail", args=[self.exp.id]))
        body = res.content.decode()
        self.assertIn("CREATE TABLE demo_sales.stores", body)
        # 既定で閉じておく（数千文字あるため）
        self.assertNotIn("<details open", body)


class DetailServiceTest(TestCase):
    def test_tag_rows_sorted_by_accuracy_ascending(self):
        rows = detail_service.tag_rows({
            "高い": {"correct": 4, "total": 4},
            "低い": {"correct": 1, "total": 4},
            "中": {"correct": 2, "total": 4},
        })
        self.assertEqual([row["tag"] for row in rows], ["低い", "中", "高い"])
        self.assertAlmostEqual(rows[0]["accuracy"], 0.25)

    def test_tag_rows_handles_zero_total(self):
        rows = detail_service.tag_rows({"空": {"correct": 0, "total": 0}})
        self.assertEqual(rows[0]["accuracy"], 0.0)

    def test_tag_rows_carry_bar_width(self):
        rows = detail_service.tag_rows({"半分": {"correct": 1, "total": 2}})
        self.assertEqual(rows[0]["percent"], 50.0)

    def test_zero_accuracy_has_zero_width(self):
        # バーは消えるが、トラック（枠）と併記した数値で 0 と読める
        rows = detail_service.tag_rows({"全滅": {"correct": 0, "total": 5}})
        self.assertEqual(rows[0]["percent"], 0.0)

    def test_full_accuracy_fills_the_bar(self):
        rows = detail_service.tag_rows({"満点": {"correct": 4, "total": 4}})
        self.assertEqual(rows[0]["percent"], 100.0)

    def test_bands_follow_accuracy(self):
        self.assertEqual(detail_service.accuracy_band(0.0), "low")
        self.assertEqual(detail_service.accuracy_band(0.49), "low")
        self.assertEqual(detail_service.accuracy_band(0.5), "mid")
        self.assertEqual(detail_service.accuracy_band(0.79), "mid")
        self.assertEqual(detail_service.accuracy_band(0.8), "high")
        self.assertEqual(detail_service.accuracy_band(1.0), "high")

    def test_error_rows_sorted_by_count_desc(self):
        rows = detail_service.error_rows({"a": 1, "b": 5})
        self.assertEqual([row["kind"] for row in rows], ["b", "a"])

    def test_parameter_rows_follow_params_field_order(self):
        rows = detail_service.parameter_rows({"few_shot": 1, "schema_mode": "full"})
        # 定義順（schema 系 → few_shot）で並ぶ
        self.assertEqual([row["key"] for row in rows], ["schema_mode", "few_shot"])

    def test_unknown_keys_go_last(self):
        rows = detail_service.parameter_rows({"謎": 1, "schema_mode": "full"})
        self.assertEqual([row["key"] for row in rows], ["schema_mode", "謎"])

    def test_judge_correct(self):
        rows = detail_service.item_rows(payload(item("q1", match=True)))
        self.assertEqual(rows[0]["verdict"], "○")

    def test_judge_mismatch_is_distinct_from_error(self):
        rows = detail_service.item_rows(payload(
            item("q1", match=False, executed=True),
            item("q2", match=False, executed=False, error_kind="undefined"),
        ))
        self.assertNotEqual(rows[0]["row_class"], rows[1]["row_class"])

    def test_gold_failed_is_out_of_scope(self):
        rows = detail_service.item_rows(payload(item("q1", gold_failed=True)))
        self.assertEqual(rows[0]["verdict"], "対象外")

    def test_missing_payload_yields_no_rows(self):
        self.assertEqual(detail_service.item_rows(None), [])


class CompareViewTest(TestCase):
    def setUp(self):
        self.a = make_experiment(name="A実験", execution_accuracy=0.4286)
        self.b = make_experiment(name="B実験", execution_accuracy=0.4000)

    def _get(self, mapping, **params):
        query = {"a": self.a.id, "b": self.b.id}
        query.update(params)
        patch_detail, patch_compare = patch_details(mapping)
        with patch_detail, patch_compare:
            return self.client.get(reverse("nl2sql_compare"), query)

    def test_renders(self):
        res = self._get({self.a.id: payload(item("q1", match=True)),
                         self.b.id: payload(item("q1"))})
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "A実験")
        self.assertContains(res, "B実験")

    def test_requires_two_ids(self):
        res = self.client.get(reverse("nl2sql_compare"))
        self.assertRedirects(res, reverse("nl2sql_list"))

    def test_single_id_redirects(self):
        res = self.client.get(reverse("nl2sql_compare"), {"a": self.a.id})
        self.assertRedirects(res, reverse("nl2sql_list"))

    def test_non_numeric_id_redirects(self):
        res = self.client.get(reverse("nl2sql_compare"), {"a": "x", "b": "y"})
        self.assertRedirects(res, reverse("nl2sql_list"))

    def test_same_id_redirects(self):
        res = self.client.get(reverse("nl2sql_compare"), {"a": self.a.id, "b": self.a.id})
        self.assertRedirects(res, reverse("nl2sql_list"))

    def test_missing_experiment_redirects_with_message(self):
        res = self.client.get(reverse("nl2sql_compare"),
                              {"a": self.a.id, "b": 9999}, follow=True)
        self.assertContains(res, "実験が見つかりません")

    def test_missing_details_hides_verdicts_only(self):
        res = self._get({self.a.id: payload(item("q1", match=True))})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.context["details_missing"])
        self.assertContains(res, "明細ファイルが無いため比較できません")
        # 1〜3（スコア・パラメータ・タグ）は出る
        self.assertContains(res, "スコア差分")
        self.assertContains(res, "パラメータ差分")
        self.assertContains(res, "タグ別差分")

    def test_missing_details_labels_the_missing_side(self):
        res = self._get({self.a.id: payload(item("q1"))})
        self.assertEqual(res.context["details_missing_labels"], [f"ID {self.b.id}"])

    def test_renders_tag_present_on_one_side_only(self):
        # 片方にしか無いタグは差が出せない。描画で落ちないこと
        self.b.by_tag = {"集計": {"correct": 1, "total": 2}}
        self.b.save()
        res = self._get({self.a.id: payload(item("q1")), self.b.id: payload(item("q1"))})
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "結合")


class DatasetWarningTest(TestCase):
    """評価データの版が揃わない比較への警告（issue_v003 の主目的）。

    版が違えばスコアの差は評価データの差であって、モデルやパラメータの効果ではない。
    同じ `schema_desc: comment` でも初版 0.4286 / 確定版 0.8857 と 0.46 動いている。
    """

    def _render(self, digest_a, digest_b):
        exp_a = make_experiment(name="A実験", dataset_digest=digest_a)
        exp_b = make_experiment(name="B実験", dataset_digest=digest_b)
        with patch.object(compare_service, "read_details", return_value=None):
            return self.client.get(reverse("nl2sql_compare"), {"a": exp_a.id, "b": exp_b.id})

    def test_same_version_has_no_warning(self):
        res = self._render(DIGEST_A, DIGEST_A)
        self.assertEqual(res.context["dataset_warning"], "")
        self.assertNotContains(res, "評価データの版")

    def test_different_version_warns(self):
        res = self._render(DIGEST_A, DIGEST_B)
        self.assertContains(res, "評価データの版が異なります（A: d7402f21 / B: ef3b87a5）")
        self.assertContains(res, "比較には使えません")

    def test_missing_version_on_side_a_warns(self):
        # 版が不明なら、同じかどうかを判断できない。「たぶん同じ」で通さない
        res = self._render("", DIGEST_B)
        self.assertContains(res, "評価データの版が不明です（A: (記録前) / B: ef3b87a5）")

    def test_missing_version_on_side_b_warns(self):
        res = self._render(DIGEST_A, "")
        self.assertContains(res, "評価データの版が不明です（A: d7402f21 / B: (記録前)）")

    def test_both_missing_warns(self):
        self.assertNotEqual(self._render("", "").context["dataset_warning"], "")

    def test_warning_does_not_hide_the_screen(self):
        # 警告までにとどめる。明細を見比べる用途があるため表示は止めない
        res = self._render(DIGEST_A, DIGEST_B)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "スコア差分")
        self.assertContains(res, "パラメータ差分")

    def test_warning_precedes_the_score_table(self):
        # 差を読んだあとで注意書きに気付いても遅い
        body = self._render(DIGEST_A, DIGEST_B).content.decode()
        self.assertLess(body.index("評価データの版が異なります"), body.index("スコア差分"))


class CompareScoreTest(TestCase):
    def setUp(self):
        self.a = make_experiment(execution_accuracy=0.4286, valid_sql_rate=1.0,
                                 prompt_tokens=1000, completion_tokens=0, elapsed_sec=10.0)
        self.b = make_experiment(execution_accuracy=0.4000, valid_sql_rate=1.0,
                                 prompt_tokens=1200, completion_tokens=0, elapsed_sec=12.0)

    def _row(self, label):
        rows = compare_service.score_rows(self.a, self.b)
        return next(row for row in rows if row["label"] == label)

    def test_accuracy_diff_text(self):
        row = self._row("実行結果一致率")
        self.assertEqual(row["text"], "0.4286 → 0.4000（-0.0286）")
        self.assertEqual(row["tone"], "bad")

    def test_no_change_has_no_tone(self):
        self.assertEqual(self._row("実行成功率")["tone"], "")

    def test_more_tokens_is_bad(self):
        # トークンと所要は少ないほど良い。一致率と逆向き
        self.assertEqual(self._row("トークン")["tone"], "bad")

    def test_less_time_is_good(self):
        self.b.elapsed_sec = 8.0
        self.assertEqual(self._row("所要")["tone"], "good")

    def test_scored_mismatch_is_flagged(self):
        self.b.scored = 3
        self.assertEqual(self._row("採点対象")["diff"], "母数が違います")


class CompareParameterTest(TestCase):
    def test_differing_value_is_flagged(self):
        a = make_experiment(parameters={"few_shot": 0, "model": "m"})
        b = make_experiment(parameters={"few_shot": 3, "model": "m"})
        rows = {row["key"]: row for row in compare_service.parameter_rows(a, b)}
        self.assertTrue(rows["few_shot"]["is_diff"])
        self.assertFalse(rows["model"]["is_diff"])

    def test_key_present_on_one_side_only_is_a_diff(self):
        a = make_experiment(parameters={"few_shot": 0})
        b = make_experiment(parameters={})
        row = compare_service.parameter_rows(a, b)[0]
        self.assertTrue(row["is_diff"])
        self.assertEqual(row["b"], compare_service.MISSING)


class CompareTagTest(TestCase):
    def test_sorted_by_absolute_difference(self):
        a = make_experiment(by_tag={"小": {"correct": 5, "total": 10},
                                    "大": {"correct": 9, "total": 10}})
        b = make_experiment(by_tag={"小": {"correct": 6, "total": 10},
                                    "大": {"correct": 1, "total": 10}})
        rows = compare_service.tag_rows(a, b)
        self.assertEqual([row["tag"] for row in rows], ["大", "小"])
        self.assertAlmostEqual(rows[0]["diff"], -0.8)

    def test_tag_on_one_side_only_has_no_diff_and_sorts_last(self):
        a = make_experiment(by_tag={"両方": {"correct": 1, "total": 2},
                                    "片方": {"correct": 1, "total": 2}})
        b = make_experiment(by_tag={"両方": {"correct": 2, "total": 2}})
        rows = compare_service.tag_rows(a, b)
        self.assertEqual(rows[-1]["tag"], "片方")
        self.assertIsNone(rows[-1]["diff"])

    def test_bar_widths_are_carried(self):
        a = make_experiment(by_tag={"半分": {"correct": 1, "total": 2}})
        b = make_experiment(by_tag={"半分": {"correct": 4, "total": 5}})
        row = compare_service.tag_rows(a, b)[0]
        self.assertEqual(row["a_percent"], 50.0)
        self.assertEqual(row["b_percent"], 80.0)

    def test_absent_tag_has_no_bar_width(self):
        # **0 にしてはいけない。** 0 のバーは「正答率0の弱点」に見えるが、
        # 実際は「そのタグが存在しない」で意味がまったく違う
        a = make_experiment(by_tag={"片方": {"correct": 1, "total": 2}})
        b = make_experiment(by_tag={})
        row = compare_service.tag_rows(a, b)[0]
        self.assertEqual(row["a_percent"], 50.0)
        self.assertIsNone(row["b_percent"])


@override_settings(NL2SQL_LOGS_DIR=LOGS_DIR)
class TagChartTest(TestCase):
    """タグ別のグラフ（issue_v004）。

    「要素がある」だけでは足りない。バーの幅が0のまま何も見えない、という
    壊れ方をするので、**幅が数値どおりに出ているか**まで見る。
    """

    def test_detail_bar_width_and_numbers(self):
        exp = make_experiment(by_tag={"表記ゆれ": {"correct": 1, "total": 5}})
        with patch.object(detail_service, "read_details", return_value=None):
            res = self.client.get(reverse("nl2sql_detail", args=[exp.id]))
        self.assertContains(res, "tag-bar-fill")
        self.assertContains(res, "width: 20.0%")
        # グラフだけにしない。数値を必ず併記する
        self.assertContains(res, "0.20")
        self.assertContains(res, "(1/5)")

    def test_detail_chart_renders_without_details_file(self):
        # `by_tag` はDB側の集計なので、明細ファイルが無くても描ける
        exp = make_experiment(by_tag={"集計": {"correct": 3, "total": 4}})
        with patch.object(detail_service, "read_details", return_value=None):
            res = self.client.get(reverse("nl2sql_detail", args=[exp.id]))
        self.assertTrue(res.context["details_missing"])
        self.assertContains(res, "width: 75.0%")

    def test_detail_zero_accuracy_keeps_the_track(self):
        # バーは消えるが、枠と併記した数値で 0 と読める
        exp = make_experiment(by_tag={"全滅": {"correct": 0, "total": 5}})
        with patch.object(detail_service, "read_details", return_value=None):
            res = self.client.get(reverse("nl2sql_detail", args=[exp.id]))
        self.assertContains(res, "tag-bar-track")
        self.assertContains(res, "(0/5)")

    def test_detail_weakest_tag_comes_first(self):
        exp = make_experiment(by_tag={"強い": {"correct": 4, "total": 4},
                                      "弱い": {"correct": 1, "total": 4}})
        with patch.object(detail_service, "read_details", return_value=None):
            body = self.client.get(reverse("nl2sql_detail", args=[exp.id])).content.decode()
        self.assertLess(body.index("弱い"), body.index("強い"))

    def _compare(self, by_tag_a, by_tag_b):
        exp_a = make_experiment(by_tag=by_tag_a)
        exp_b = make_experiment(by_tag=by_tag_b)
        with patch.object(compare_service, "read_details", return_value=None):
            return self.client.get(reverse("nl2sql_compare"), {"a": exp_a.id, "b": exp_b.id})

    def test_compare_draws_both_sides(self):
        res = self._compare({"集計": {"correct": 1, "total": 2}},
                            {"集計": {"correct": 4, "total": 5}})
        self.assertContains(res, "width: 50.0%")
        self.assertContains(res, "width: 80.0%")
        self.assertContains(res, "(1/2)")
        self.assertContains(res, "(4/5)")

    def test_compare_absent_tag_is_not_drawn_as_zero(self):
        res = self._compare(
            {"両方": {"correct": 1, "total": 2}, "片方": {"correct": 1, "total": 2}},
            {"両方": {"correct": 2, "total": 2}},
        )
        body = res.content.decode()
        # B 側に無いタグは「データなし」。0 のバーは描かない
        self.assertEqual(body.count("このタグはデータなし"), 1)
        # 両方(A/B) + 片方(Aのみ) の3本
        self.assertEqual(body.count("tag-bar-fill"), 3)

    def test_compare_absent_tag_shows_no_number_either(self):
        res = self._compare({"片方": {"correct": 1, "total": 2}}, {})
        row = res.context["tag_rows"][0]
        self.assertIsNone(row["b_percent"])
        self.assertIsNone(row["b_accuracy"])


class VerdictGroupTest(TestCase):
    """問題単位の勝敗。**この画面の主目的**なので区分を細かく確かめる。"""

    def _groups(self, items_a, items_b):
        return compare_service.verdict_groups(payload(*items_a), payload(*items_b))

    def test_four_buckets(self):
        groups = self._groups(
            [item("q1", match=True), item("q2", match=True),
             item("q3", match=False), item("q4", match=False)],
            [item("q1", match=True), item("q2", match=False),
             item("q3", match=True), item("q4", match=False)],
        )
        self.assertEqual([entry["id"] for entry in groups["both_correct"]], ["q1"])
        self.assertEqual([entry["id"] for entry in groups["only_a"]], ["q2"])
        self.assertEqual([entry["id"] for entry in groups["only_b"]], ["q3"])
        self.assertEqual([entry["id"] for entry in groups["both_wrong"]], ["q4"])

    def test_gold_failed_is_excluded_from_the_four_buckets(self):
        # モデルの成否ではないので「両方不正解」に混ぜてはいけない
        groups = self._groups(
            [item("q1", gold_failed=True)],
            [item("q1", gold_failed=True)],
        )
        self.assertEqual([entry["id"] for entry in groups["excluded"]], ["q1"])
        self.assertEqual(groups["both_wrong"], [])

    def test_gold_failed_on_one_side_only_is_still_excluded(self):
        groups = self._groups(
            [item("q1", match=True)],
            [item("q1", gold_failed=True)],
        )
        self.assertEqual([entry["id"] for entry in groups["excluded"]], ["q1"])
        self.assertEqual(groups["only_a"], [])

    def test_item_present_on_one_side_only_is_unpaired(self):
        groups = self._groups([item("q1", match=True)], [item("q2", match=True)])
        self.assertEqual({entry["id"] for entry in groups["unpaired"]}, {"q1", "q2"})
        self.assertEqual(groups["both_correct"], [])

    def test_entry_carries_both_generated_sql(self):
        groups = self._groups(
            [item("q1", match=True, generated_sql="SELECT 'a'")],
            [item("q1", match=False, generated_sql="SELECT 'b'")],
        )
        entry = groups["only_a"][0]
        self.assertEqual(entry["a"]["generated_sql"], "SELECT 'a'")
        self.assertEqual(entry["b"]["generated_sql"], "SELECT 'b'")
        self.assertEqual(entry["tag_text"], "集計")

    def test_order_follows_side_a(self):
        groups = self._groups(
            [item("q9", match=False), item("q1", match=False)],
            [item("q1", match=False), item("q9", match=False)],
        )
        self.assertEqual([entry["id"] for entry in groups["both_wrong"]], ["q9", "q1"])

    def test_missing_payload_yields_empty_groups(self):
        groups = compare_service.verdict_groups(None, None)
        self.assertEqual(sum(len(entries) for entries in groups.values()), 0)


class CompareVerdictRenderTest(TestCase):
    def test_only_a_section_lists_the_question(self):
        a = make_experiment(name="A実験")
        b = make_experiment(name="B実験")
        mapping = {
            a.id: payload(item("q1", match=True)),
            b.id: payload(item("q1", match=False)),
        }
        patch_detail, patch_compare = patch_details(mapping)
        with patch_detail, patch_compare:
            res = self.client.get(reverse("nl2sql_compare"), {"a": a.id, "b": b.id})
        self.assertContains(res, "A のみ正解")
        self.assertContains(res, "q1 の質問")
        groups = {group["key"]: group for group in res.context["group_rows"]}
        self.assertEqual(groups["only_a"]["count"], 1)
        self.assertEqual(groups["only_b"]["count"], 0)


@override_settings(NL2SQL_LOGS_DIR=LOGS_DIR)
class ReadOnlyTest(TestCase):
    """書き込み操作が1つも無いこと（完了条件3）。"""

    def setUp(self):
        self.exp = make_experiment(name="変更されないこと", is_starred=False)

    def test_post_does_not_change_anything(self):
        urls = [
            reverse("nl2sql_list"),
            reverse("nl2sql_detail", args=[self.exp.id]),
            reverse("nl2sql_compare"),
        ]
        with patch.object(detail_service, "read_details", return_value=None):
            for url in urls:
                self.client.post(url, {"name": "書き換え", "is_starred": True})

        self.exp.refresh_from_db()
        self.assertEqual(self.exp.name, "変更されないこと")
        self.assertFalse(self.exp.is_starred)
        self.assertEqual(Nl2SqlExperiment.objects.count(), 1)

    def test_views_never_read_request_post(self):
        # POST を受けるビューを作らない方針。実装が増えたときに気付けるようにしておく
        from pathlib import Path

        from app.nl2sql.web import views

        source = Path(views.__file__).read_text(encoding="utf-8")
        self.assertNotIn("request.POST", source)
        self.assertNotIn("request.method", source)
