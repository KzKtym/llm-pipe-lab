"""nl2sql/core/rule_scoring。`issue_c017`：盲検セットでの採点をコードで再現できるようにする。

`stage2_rule_evaluator.evaluate()` の戻り値（または `stage2_rule_store.read_details()` で
読み戻した明細）を、`rule_validation_questions*.json` の `types` と突き合わせて数字を出す。

判定ロジック（`axis_extractor.py` / `population_rules.py`）には一切触れない。
既存の `evaluate()` の戻り値も変えない——採点はここに別関数として足しただけ
（`issue_c017` 依頼(1)）。

数字は次のとおり定義する（`issue_c017` 依頼(3)）。

    型の完全一致          … 判定側の検出型集合 == gold の `types` 集合
    「聞き返すか否か」一致 … 型1の有無が gold と判定で一致
    見逃し                … gold にあり判定に無い型（型別に数える）
    過検出                … 判定にあり gold に無い型（型別に数える）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .stage2_rule_evaluator import RuleEvalItem

#: 型は1〜4の4種（`population_rules.py` の kind と揃える）
ALL_KINDS = (1, 2, 3, 4)


@dataclass(frozen=True)
class GoldLabel:
    id: str
    unique: bool
    types: frozenset[int]


def load_gold(path: str | Path) -> dict[str, GoldLabel]:
    """`rule_validation_questions*.json` を正解として読む。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        str(item["id"]): GoldLabel(
            id=str(item["id"]),
            unique=bool(item["unique"]),
            types=frozenset(int(t) for t in item["types"]),
        )
        for item in raw
    }


def detected_types(item: RuleEvalItem) -> frozenset[int]:
    """判定側が未確定として検出した型の集合。`item.axes` の `resolved=False` から求める。"""
    return frozenset(a["kind"] for a in item.axes if not a["resolved"])


def items_from_details(details: dict) -> list[RuleEvalItem]:
    """`stage2_rule_store.read_details()` の payload から `RuleEvalItem` を復元する。

    保存済みの明細から、API を再び呼ばずに採点だけをやり直せるようにするため
    （`issue_c017` 完了条件1：同じ入力から同じ数字が出る）。
    """
    return [RuleEvalItem(**item) for item in details["items"]]


@dataclass(frozen=True)
class Mismatch:
    id: str
    question: str
    gold_types: frozenset[int]
    detected: frozenset[int]
    missed: frozenset[int]
    overdetected: frozenset[int]


@dataclass
class ScoreSummary:
    total: int = 0
    exact_match: int = 0
    ask_or_not_match: int = 0
    missed_by_type: dict = field(default_factory=lambda: {k: 0 for k in ALL_KINDS})
    overdetected_by_type: dict = field(default_factory=lambda: {k: 0 for k in ALL_KINDS})
    mismatches: list = field(default_factory=list)
    #: gold に無い、または判定側に無い id（本来は空であるべき。整合性の確認用）
    unscored_ids: list = field(default_factory=list)


def score(
    items: list[RuleEvalItem],
    gold: dict[str, GoldLabel],
    *,
    exclude_ids: frozenset = frozenset(),
) -> ScoreSummary:
    """`items`（判定結果）を `gold`（正解）と突き合わせて集計する。

    Args:
        exclude_ids: 集計から除く `id`（`issue_c017` の `s211` 除外版に使う）
    """
    summary = ScoreSummary()
    for item in items:
        if item.id in exclude_ids:
            continue
        label = gold.get(item.id)
        if label is None:
            summary.unscored_ids.append(item.id)
            continue

        summary.total += 1
        detected = detected_types(item)
        gold_types = label.types

        if detected == gold_types:
            summary.exact_match += 1
        if (1 in detected) == (1 in gold_types):
            summary.ask_or_not_match += 1

        missed = gold_types - detected
        overdetected = detected - gold_types
        for t in missed:
            summary.missed_by_type[t] += 1
        for t in overdetected:
            summary.overdetected_by_type[t] += 1

        if missed or overdetected:
            summary.mismatches.append(
                Mismatch(
                    id=item.id,
                    question=item.question,
                    gold_types=gold_types,
                    detected=detected,
                    missed=frozenset(missed),
                    overdetected=frozenset(overdetected),
                )
            )
    return summary


def format_score(summary: ScoreSummary, *, label: str = "") -> str:
    lines = [
        f"{label}件数: {summary.total}",
        f"型の完全一致: {summary.exact_match}/{summary.total}",
        f"「聞き返すか否か」のみ一致: {summary.ask_or_not_match}/{summary.total}",
        "見逃し（型別）: " + " ".join(f"型{k}={v}" for k, v in summary.missed_by_type.items()),
        "過検出（型別）: " + " ".join(f"型{k}={v}" for k, v in summary.overdetected_by_type.items()),
    ]
    if summary.unscored_ids:
        lines.append(f"※ gold と突き合わせられなかった id: {summary.unscored_ids}")
    return "\n".join(lines)
