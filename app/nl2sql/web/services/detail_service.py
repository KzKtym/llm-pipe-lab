"""詳細画面の組み立て。

サマリはDBだけで足りる。1問ごとの明細はファイル（`store.read_details`）にある。
**明細が無いことは異常ではない**（別環境で走らせた実験など）ので、
その場合はサマリまでを出して欠落を明示する。
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

#: 判定の表示と、行の色。不一致とエラーを別の色にして拾いやすくする
JUDGE_CORRECT = ("○", "")
JUDGE_MISMATCH = ("×", "table-warning")
JUDGE_ERROR = ("×", "table-danger")
JUDGE_SKIPPED = ("対象外", "table-secondary")


def parameter_rows(parameters: dict | None) -> list[dict]:
    """`parameters` を表に流せる形へ。記録されているキーを全件出す。"""
    parameters = parameters or {}
    return [
        {"key": key, "value": parameters[key]}
        for key in sort_parameter_keys(parameters)
    ]


def accuracy_band(accuracy: float) -> str:
    """バーの色帯。**色は補助**で、弱点は並び順と数値で読めること。

    赤緑が判別できない見え方があるため、色だけに意味を持たせない
    （低い順に並んでいることと、併記した数値が本体）。
    """
    if accuracy < 0.5:
        return "low"
    if accuracy < 0.8:
        return "mid"
    return "high"


def tag_rows(by_tag: dict | None) -> list[dict]:
    """タグ別の正答率。**低い順**に並べる（弱点が上に来るように）。

    `percent` はバーの幅（%）。集計そのものは変えず、描くための値だけ足す。
    """
    rows = []
    for tag, stat in (by_tag or {}).items():
        total = stat.get("total", 0)
        correct = stat.get("correct", 0)
        accuracy = round(correct / total, 4) if total else 0.0
        rows.append({
            "tag": tag,
            "correct": correct,
            "total": total,
            "accuracy": accuracy,
            "percent": round(accuracy * 100, 1),
            "band": accuracy_band(accuracy),
        })
    rows.sort(key=lambda row: (row["accuracy"], -row["total"], row["tag"]))
    return rows


def error_rows(by_error_kind: dict | None) -> list[dict]:
    """失敗の内訳。件数の多い順。"""
    rows = [
        {"kind": kind, "count": count}
        for kind, count in (by_error_kind or {}).items()
    ]
    rows.sort(key=lambda row: (-row["count"], row["kind"]))
    return rows


def _judge(item: dict) -> tuple[str, str]:
    if item.get("gold_failed"):
        # 正解SQLが流せなかった問題。モデルの成否ではないので採点対象から外れている
        return JUDGE_SKIPPED
    if item.get("match"):
        return JUDGE_CORRECT
    if item.get("executed"):
        return JUDGE_MISMATCH
    return JUDGE_ERROR


def item_rows(payload: dict | None) -> list[dict]:
    """明細ファイルの `items` を表示用に整える。"""
    rows = []
    for item in (payload or {}).get("items", []):
        verdict, row_class = _judge(item)
        rows.append({
            **item,
            "verdict": verdict,
            "row_class": row_class,
            "tag_text": ", ".join(item.get("tags") or []),
        })
    return rows


def build_detail_context(experiment_id: int) -> dict | None:
    """詳細画面のコンテキスト。実験が無ければ `None`（ビューが404にする）。"""
    experiment = Nl2SqlExperiment.objects.filter(id=experiment_id).first()
    if experiment is None:
        return None

    payload = read_details(experiment_id)
    return {
        "experiment": experiment,
        "parameter_line": parameter_line(experiment.parameters),
        "warnings": warnings_for(experiment),
        "dataset_label": dataset_label(experiment),
        "dataset_hue": dataset_hue(experiment.dataset_digest),
        "parameter_rows": parameter_rows(experiment.parameters),
        "tag_rows": tag_rows(experiment.by_tag),
        "error_rows": error_rows(experiment.by_error_kind),
        "item_rows": item_rows(payload),
        "details_missing": payload is None,
    }
