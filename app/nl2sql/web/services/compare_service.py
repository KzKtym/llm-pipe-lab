"""比較画面の組み立て。**このツールの主目的**。

出すのは4つ。

1. スコア差分
2. パラメータ差分（値が違う行だけハイライト）
3. タグ別差分（差の大きい順）
4. 問題単位の勝敗

価値の重心は4番。全体の一致率が 0.43 → 0.40 と分かっても、
下がった原因は分からない。「A だけ正解した問題」「B だけ正解した問題」を
並べて初めて、パラメータを変えた効果そのものが見える。
"""
from __future__ import annotations

from ...models import Nl2SqlExperiment
from ...store import read_details
from .list_service import (
    dataset_hue,
    dataset_label,
    parameter_line,
    sort_parameter_keys,
    warnings_for,
)

#: 値が片方に無いときの表示
MISSING = "—"


def _fmt(value, digits: int) -> str:
    return f"{value:.{digits}f}" if digits else f"{value:,}"


def _score_row(label, a_value, b_value, *, digits=0, higher_is_better=True, unit="") -> dict:
    """`0.4286 → 0.4000（-0.0286）` の1行を作る。

    良し悪しの色分けは指標ごとに向きが違う（一致率は高いほど良い、
    トークンと所要は低いほど良い）ので、ここで決めて `tone` に落とす。
    """
    # 表示する桁で丸めてから向きを決める。見た目が同じ値に色が付くと読み手が混乱する
    diff = round(b_value - a_value, digits) if digits else b_value - a_value
    tone = "" if diff == 0 else ("good" if (diff > 0) == higher_is_better else "bad")

    sign = "+" if diff > 0 else ""
    diff_text = f"{sign}{_fmt(diff, digits)}"
    return {
        "label": label,
        "a": _fmt(a_value, digits) + unit,
        "b": _fmt(b_value, digits) + unit,
        "diff": diff_text + unit,
        "tone": tone,
        "text": f"{_fmt(a_value, digits)}{unit} → {_fmt(b_value, digits)}{unit}（{diff_text}{unit}）",
    }


def score_rows(exp_a: Nl2SqlExperiment, exp_b: Nl2SqlExperiment) -> list[dict]:
    rows = [
        _score_row("実行結果一致率", exp_a.execution_accuracy, exp_b.execution_accuracy, digits=4),
        _score_row("実行成功率", exp_a.valid_sql_rate, exp_b.valid_sql_rate, digits=4),
        _score_row("トークン", exp_a.total_tokens, exp_b.total_tokens, higher_is_better=False),
        _score_row("所要", exp_a.elapsed_sec, exp_b.elapsed_sec, digits=2,
                   higher_is_better=False, unit="秒"),
    ]
    # 採点対象は差を取っても意味が薄い（母数が違えば一致率の比較自体が怪しい、
    # という警告として読む値）。数字だけ並べる
    rows.insert(2, {
        "label": "採点対象",
        "a": f"{exp_a.scored}/{exp_a.question_count}",
        "b": f"{exp_b.scored}/{exp_b.question_count}",
        "diff": "" if exp_a.scored == exp_b.scored else "母数が違います",
        "tone": "" if exp_a.scored == exp_b.scored else "bad",
        "text": "",
    })
    return rows


def parameter_rows(exp_a: Nl2SqlExperiment, exp_b: Nl2SqlExperiment) -> list[dict]:
    """2実験のパラメータを並べ、値が違う行に印を付ける。

    片方にしか無いキーも差として扱う。既定値で補完すると
    「記録に無い」と「既定値だった」が区別できなくなる。
    """
    params_a = exp_a.parameters or {}
    params_b = exp_b.parameters or {}
    rows = []
    for key in sort_parameter_keys(set(params_a) | set(params_b)):
        value_a = params_a.get(key, MISSING)
        value_b = params_b.get(key, MISSING)
        rows.append({
            "key": key,
            "a": value_a,
            "b": value_b,
            "is_diff": value_a != value_b,
        })
    return rows


def _accuracy(stat: dict | None) -> float | None:
    if not stat or not stat.get("total"):
        return None
    return round(stat.get("correct", 0) / stat["total"], 4)


def tag_rows(exp_a: Nl2SqlExperiment, exp_b: Nl2SqlExperiment) -> list[dict]:
    """タグごとの A/B と差。**差の大きい順**（絶対値）に並べる。"""
    by_tag_a = exp_a.by_tag or {}
    by_tag_b = exp_b.by_tag or {}
    rows = []
    for tag in set(by_tag_a) | set(by_tag_b):
        stat_a = by_tag_a.get(tag)
        stat_b = by_tag_b.get(tag)
        acc_a = _accuracy(stat_a)
        acc_b = _accuracy(stat_b)
        diff = None if acc_a is None or acc_b is None else round(acc_b - acc_a, 4)
        rows.append({
            "tag": tag,
            "a": stat_a,
            "b": stat_b,
            "a_accuracy": acc_a,
            "b_accuracy": acc_b,
            # バーの幅（%）。**片方に無いタグは None のまま**にする。
            # 0 にするとバーが「正答率0の弱点」に見えるが、実際は
            # 「そのタグが存在しない」で意味がまったく違う
            "a_percent": None if acc_a is None else round(acc_a * 100, 1),
            "b_percent": None if acc_b is None else round(acc_b * 100, 1),
            "diff": diff,
            "diff_text": MISSING if diff is None else f"{'+' if diff > 0 else ''}{diff:.4f}",
            "tone": "" if not diff else ("good" if diff > 0 else "bad"),
        })
    # 片方にしか無いタグは差が出せない。並べ替えでは最後に回す
    rows.sort(key=lambda row: (row["diff"] is None, -abs(row["diff"] or 0), row["tag"]))
    return rows


def dataset_warning(exp_a: Nl2SqlExperiment, exp_b: Nl2SqlExperiment) -> str:
    """評価データの版が揃っていなければ、その旨を返す。揃っていれば空文字。

    **この画面で最も強い警告。** 版が違えばスコアの差は評価データの差であって、
    モデルやパラメータの効果ではない。実際、同じ `schema_desc: comment` でも
    初版 0.4286 / 確定版 0.8857 と 0.46 動いている。気付かずに並べると、
    やっていない変更の効果を読み取ってしまう。

    片方でも版が空なら同じ扱いにする。同じ版かどうかを**判断できない**ためで、
    「たぶん同じだろう」で通すと上と同じ誤読が起きる。
    """
    digest_a = exp_a.dataset_digest
    digest_b = exp_b.dataset_digest
    label_a = dataset_label(exp_a)
    label_b = dataset_label(exp_b)

    if not digest_a or not digest_b:
        return (
            f"評価データの版が不明です（A: {label_a} / B: {label_b}）。"
            "同じ版で測られたか判断できないため、スコアの比較は成立しません。"
        )
    if digest_a != digest_b:
        return (
            f"評価データの版が異なります（A: {label_a} / B: {label_b}）。"
            "スコアの差はモデルやパラメータの違いではなく、評価データの違いに"
            "よるものです。比較には使えません。"
        )
    return ""


def _items_by_id(payload: dict | None) -> dict:
    return {str(item.get("id")): item for item in (payload or {}).get("items", [])}


def verdict_groups(payload_a: dict | None, payload_b: dict | None) -> dict:
    """問題単位の勝敗。

    採点対象外（どちらかで正解SQLが流せなかった問題）は4区分に混ぜない。
    モデルの成否ではないため、「両方不正解」に入れると読み違える。

    片方の明細にしか無い問題も別扱いにする。評価データが実験の間に
    増減した場合に起きるが、勝敗としては比較できない。
    """
    by_id_a = _items_by_id(payload_a)
    by_id_b = _items_by_id(payload_b)

    groups = {
        "both_correct": [],
        "only_a": [],
        "only_b": [],
        "both_wrong": [],
        "excluded": [],
        "unpaired": [],
    }

    # 並びは A の明細順（評価データの順）。A に無いものは後ろへ足す
    ordered_ids = list(by_id_a) + [key for key in by_id_b if key not in by_id_a]
    for item_id in ordered_ids:
        item_a = by_id_a.get(item_id)
        item_b = by_id_b.get(item_id)
        source = item_a or item_b
        entry = {
            "id": item_id,
            "question": source.get("question", ""),
            "tags": source.get("tags") or [],
            "tag_text": ", ".join(source.get("tags") or []),
            "a": item_a,
            "b": item_b,
        }

        if item_a is None or item_b is None:
            groups["unpaired"].append(entry)
        elif item_a.get("gold_failed") or item_b.get("gold_failed"):
            groups["excluded"].append(entry)
        elif item_a.get("match") and item_b.get("match"):
            groups["both_correct"].append(entry)
        elif item_a.get("match"):
            groups["only_a"].append(entry)
        elif item_b.get("match"):
            groups["only_b"].append(entry)
        else:
            groups["both_wrong"].append(entry)

    return groups


#: 表示順と見出し。`only_a` / `only_b` が主役なので先に置く
GROUP_ORDER = [
    ("only_a", "A のみ正解", "table-success", True),
    ("only_b", "B のみ正解", "table-info", True),
    ("both_correct", "両方正解", "", False),
    ("both_wrong", "両方不正解", "", False),
    ("excluded", "採点対象外（正解SQLが実行できなかった問題）", "table-secondary", False),
    ("unpaired", "片方の明細にしか無い問題", "table-secondary", False),
]


def build_compare_context(id_a: int, id_b: int) -> dict | None:
    """比較画面のコンテキスト。どちらかの実験が無ければ `None`。"""
    exp_a = Nl2SqlExperiment.objects.filter(id=id_a).first()
    exp_b = Nl2SqlExperiment.objects.filter(id=id_b).first()
    if exp_a is None or exp_b is None:
        return None

    payload_a = read_details(exp_a.id)
    payload_b = read_details(exp_b.id)
    details_missing = payload_a is None or payload_b is None

    groups = verdict_groups(payload_a, payload_b)
    group_rows = [
        {
            "key": key,
            "label": label,
            "row_class": row_class,
            "highlight": highlight,
            "entries": groups[key],
            "count": len(groups[key]),
        }
        for key, label, row_class, highlight in GROUP_ORDER
    ]

    missing_labels = [
        f"ID {experiment.id}"
        for experiment, payload in ((exp_a, payload_a), (exp_b, payload_b))
        if payload is None
    ]

    return {
        "exp_a": exp_a,
        "exp_b": exp_b,
        "parameter_line_a": parameter_line(exp_a.parameters),
        "parameter_line_b": parameter_line(exp_b.parameters),
        "warnings_a": warnings_for(exp_a),
        "warnings_b": warnings_for(exp_b),
        "dataset_warning": dataset_warning(exp_a, exp_b),
        "dataset_label_a": dataset_label(exp_a),
        "dataset_label_b": dataset_label(exp_b),
        "dataset_hue_a": dataset_hue(exp_a.dataset_digest),
        "dataset_hue_b": dataset_hue(exp_b.dataset_digest),
        "score_rows": score_rows(exp_a, exp_b),
        "parameter_rows": parameter_rows(exp_a, exp_b),
        "tag_rows": tag_rows(exp_a, exp_b),
        "group_rows": [] if details_missing else group_rows,
        "details_missing": details_missing,
        "details_missing_labels": missing_labels,
    }
