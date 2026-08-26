"""段階2（ルール判定版）の評価データを一括で流す。

`stage2_evaluator.py`（LLM総合判断・`hint`で凍結・`exp_1`〜`exp_10`）とは
**別の指標体系**（`issue_c013` 論点D）。旧評価は「聞き返せたか」の混同行列
（TP/FN/FP/TN）だったが、新評価は「未確定な軸を検出できたか」を型別に数える。

工程は3つに分かれる（`issue_c013` 論点F）。

    ① axis_extractor.AxisExtractor.extract()      … AI（構造化）
    ③ population_rules.classify()                  … ルール（判定。LLMは呼ばない）
    ② ambiguity_judge.phrase_disclosure()           … AI（文言化）

SQLは生成しない。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.common.llm_client import LLMFatalError, LLMTransientError

from . import population_rules
from .ambiguity_judge import phrase_disclosure
from .axis_extractor import AxisExtractor

logger = logging.getLogger(__name__)


@dataclass
class RuleQuestion:
    """評価データ1件。`id` / `question` さえあれば、どの評価データファイルでも読める
    （`evaluation_questions.json` と `stage2_questions.json` の両方を想定）。
    """

    id: str
    question: str


@dataclass
class RuleEvalItem:
    """1問の結果。"""

    id: str
    question: str
    aggregation: str = ""
    group_by: str = ""
    period: str = ""
    #: [{"name": str, "kind": int, "resolved": bool, "default": str}]
    axes: list[dict] = field(default_factory=list)
    needs_clarification: bool = False
    disclosure_text: str = ""
    raw_extraction_response: str = ""
    raw_phrasing_response: str = ""
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


@dataclass
class RuleEvalSummary:
    """全体の集計。

    Attributes:
        judged: 工程①（構造抽出）が成功した件数
        failed: 工程①がパースできなかった件数。0でなければプロンプト/出力契約の不備の疑い
        type1_detected〜type4_detected: 型別に「未確定」と判定された件数（実数）
        needs_clarification_count: 型1が1つでもあり、聞き返しが要ると判定した件数
    """

    total: int = 0
    judged: int = 0
    failed: int = 0
    type1_detected: int = 0
    type2_detected: int = 0
    type3_detected: int = 0
    type4_detected: int = 0
    needs_clarification_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_sec: float = 0.0
    aborted: bool = False
    abort_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def load_questions(path: str | Path) -> list[RuleQuestion]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [RuleQuestion(id=str(item["id"]), question=item["question"]) for item in raw]


def dataset_digest(questions: list[RuleQuestion]) -> str:
    """評価データの版を表すダイジェスト。対象は `id` / `question` のみ。"""
    canonical = [
        {"id": q.id, "question": q.question} for q in sorted(questions, key=lambda q: q.id)
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate(
    questions: list[RuleQuestion],
    extractor: AxisExtractor,
    phrasing_client,
    *,
    pause_sec: float = 0.0,
    flagged_activity_checker=None,
    roster_emptiness_checker=None,
    time_emptiness_checker=None,
) -> tuple[RuleEvalSummary, list[RuleEvalItem]]:
    """評価データを順に流し、集計と明細を返す。

    Args:
        extractor: 工程①（構造抽出）
        phrasing_client: 工程②（文言生成）に使うLLMクライアント。
            `extractor` と同じクライアントでよいが、役割が違うため引数を分けている
        flagged_activity_checker: `population_rules.classify` へそのまま渡す
            （`issue_c015` パターンA）。`None` なら型1・型2を常に別々に検出する
        roster_emptiness_checker: `population_rules.classify` へそのまま渡す
            （`issue_c020`）。`None` なら常に型1を立てる
        time_emptiness_checker: `population_rules.classify` へそのまま渡す
            （`issue_c028` (a)）。`None` なら常に型1を立てる
    """
    summary = RuleEvalSummary(total=len(questions))
    items: list[RuleEvalItem] = []
    started = time.time()

    for index, question in enumerate(questions):
        if pause_sec > 0 and index > 0:
            time.sleep(pause_sec)

        item = RuleEvalItem(id=question.id, question=question.question)

        try:
            extraction = extractor.extract(question.question)
        except LLMFatalError as e:
            summary.aborted = True
            summary.abort_reason = str(e)
            logger.error("stage2 rule evaluation aborted (id=%s): %s", question.id, e)
            break
        except LLMTransientError as e:
            item.error = f"extraction failed: {e}"
            summary.failed += 1
            items.append(item)
            logger.warning("axis extraction failed (id=%s): %s", question.id, e)
            continue

        item.aggregation = extraction.aggregation
        item.group_by = extraction.group_by
        item.period = extraction.period
        item.raw_extraction_response = extraction.raw_response
        item.prompt_tokens += extraction.prompt_tokens
        item.completion_tokens += extraction.completion_tokens
        item.latency_ms += extraction.latency_ms
        summary.judged += 1
        summary.prompt_tokens += extraction.prompt_tokens
        summary.completion_tokens += extraction.completion_tokens

        result = population_rules.classify(
            extraction,
            flagged_activity_checker=flagged_activity_checker,
            roster_emptiness_checker=roster_emptiness_checker,
            time_emptiness_checker=time_emptiness_checker,
        )
        item.axes = [
            {"name": a.name, "kind": a.kind, "resolved": a.resolved, "default": a.default}
            for a in result.axes
        ]
        item.needs_clarification = result.needs_clarification

        for axis in result.unresolved_axes:
            if axis.kind == 1:
                summary.type1_detected += 1
            elif axis.kind == 2:
                summary.type2_detected += 1
            elif axis.kind == 3:
                summary.type3_detected += 1
            elif axis.kind == 4:
                summary.type4_detected += 1
        if result.needs_clarification:
            summary.needs_clarification_count += 1

        try:
            phrasing = phrase_disclosure(phrasing_client, question.question, result)
            item.disclosure_text = phrasing.text
            item.raw_phrasing_response = phrasing.raw_response
            item.prompt_tokens += phrasing.prompt_tokens
            item.completion_tokens += phrasing.completion_tokens
            item.latency_ms += phrasing.latency_ms
            summary.prompt_tokens += phrasing.prompt_tokens
            summary.completion_tokens += phrasing.completion_tokens
        except LLMFatalError as e:
            summary.aborted = True
            summary.abort_reason = str(e)
            items.append(item)
            logger.error("stage2 rule evaluation aborted during phrasing (id=%s): %s", question.id, e)
            break
        except LLMTransientError as e:
            # 文言生成の失敗は軸の検出そのものを無効にしない。別に記録するだけ
            item.error = f"phrasing failed: {e}"
            logger.warning("disclosure phrasing failed (id=%s): %s", question.id, e)

        items.append(item)

    summary.elapsed_sec = round(time.time() - started, 2)
    return summary, items


def format_summary(summary: RuleEvalSummary) -> str:
    lines = [
        f"未確定軸の検出: 型1={summary.type1_detected} 型2={summary.type2_detected} "
        f"型3={summary.type3_detected} 型4={summary.type4_detected}",
        f"聞き返しが要ると判定した件数: {summary.needs_clarification_count}/{summary.judged}",
        f"所要: {summary.elapsed_sec}秒 / トークン: {summary.total_tokens}",
    ]
    if summary.failed:
        lines.append(f"※ 構造を抽出できなかった問題: {summary.failed}件（プロンプト/出力契約の不備の疑い）")
    if summary.aborted:
        lines.append(f"※ 中断: {summary.abort_reason}")
    return "\n".join(lines)
