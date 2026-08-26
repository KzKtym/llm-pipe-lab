"""段階2の評価データを一括で流し、混同行列とキーワード一致を集計する。

`app/nl2sql/core/evaluator.py`（段階1）と同じ位置づけ。指標の形が違う。

    段階1: 実行結果一致率（SQLを生成・実行して判定）
    段階2: 見逃し率（主指標）＋誤検出率（併記）＋キーワード一致（第2軸・参考値）

SQLは生成しない。判断ステップ（`AmbiguityJudge`）だけを1問ずつ流す。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.common.llm_client import LLMFatalError, LLMTransientError

from .ambiguity_judge import AmbiguityJudge

logger = logging.getLogger(__name__)


@dataclass
class Stage2Question:
    """段階2の評価データ1件。`sample/sales/stage2_questions.json` の1要素に対応する。"""

    id: str
    question: str
    is_ambiguous: bool
    expected_clarification_keywords: list[str] = field(default_factory=list)
    ambiguity_axis: str = ""


@dataclass
class Stage2Item:
    """1問の結果。人の判定を後から書き込めるよう `human_correct` を持つ。"""

    id: str
    question: str
    gold_is_ambiguous: bool
    ambiguity_axis: str = ""
    judged_ask: bool | None = None
    clarification: str = ""
    raw_response: str = ""
    expected_clarification_keywords: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    keyword_match: bool = False
    human_correct: bool | None = None
    #: "TP" / "FN" / "FP" / "TN" / "FAILED"
    confusion: str = ""
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


@dataclass
class Stage2Summary:
    """全体の集計。

    Attributes:
        judged: 判定できた件数（tp+fn+fp+tn）
        failed: 判断ステップの出力がパースできなかった件数。0でなければ
            プロンプトか出力契約に不備があるサイン
        tp/fn/fp/tn: 混同行列（実数）。fn が見逃し＝主指標の分子
        axis2_scored: 第2軸の母数（= tp）
        axis2_keyword_match: キーワード一致した件数（参考値）
    """

    total: int = 0
    judged: int = 0
    failed: int = 0
    tp: int = 0
    fn: int = 0
    fp: int = 0
    tn: int = 0
    axis2_scored: int = 0
    axis2_keyword_match: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_sec: float = 0.0
    aborted: bool = False
    abort_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def miss_rate(self) -> float:
        denom = self.tp + self.fn
        return round(self.fn / denom, 4) if denom else 0.0

    @property
    def false_positive_rate(self) -> float:
        denom = self.fp + self.tn
        return round(self.fp / denom, 4) if denom else 0.0

    @property
    def axis2_keyword_match_rate(self) -> float:
        return round(self.axis2_keyword_match / self.axis2_scored, 4) if self.axis2_scored else 0.0


def load_questions(path: str | Path) -> list[Stage2Question]:
    """評価データのJSONを読む。未知のキーは無視する。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Stage2Question(
            id=str(item["id"]),
            question=item["question"],
            is_ambiguous=bool(item.get("is_ambiguous", False)),
            expected_clarification_keywords=list(item.get("expected_clarification_keywords") or []),
            ambiguity_axis=item.get("ambiguity_axis") or "",
        )
        for item in raw
    ]


def dataset_digest(questions: list[Stage2Question]) -> str:
    """段階2評価データの版を表すダイジェスト。

    対象: `id` / `question` / `is_ambiguous` / `expected_clarification_keywords`
    対象外: `note` / `why_unambiguous` / `styles` / `topic` / `based_on` / `group` /
    `ambiguity_axis`（型が「母集合の境界」1種類しか無い間は対象外。型別の内訳を
    出すようになった時点で対象に加える）。
    """
    canonical = [
        {
            "id": q.id,
            "question": q.question,
            "is_ambiguous": q.is_ambiguous,
            "expected_clarification_keywords": sorted(q.expected_clarification_keywords),
        }
        for q in sorted(questions, key=lambda q: q.id)
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _match_keywords(clarification: str, keywords: list[str]) -> list[str]:
    """素の部分一致。表記の揺れは正規化しない（`issue_c009` 確定事項b）。"""
    return [keyword for keyword in keywords if keyword in clarification]


def _confusion(gold_is_ambiguous: bool, judged_ask: bool) -> str:
    if gold_is_ambiguous:
        return "TP" if judged_ask else "FN"
    return "FP" if judged_ask else "TN"


def evaluate(
    questions: list[Stage2Question],
    judge: AmbiguityJudge,
    *,
    pause_sec: float = 0.0,
) -> tuple[Stage2Summary, list[Stage2Item]]:
    """評価データを順に流し、集計と明細を返す。

    第2軸（キーワード一致）は `confusion == "TP"` の問題だけ計算する。
    見逃した問題は聞き返していないので、聞き返しの中身を判定しようがない。
    """
    summary = Stage2Summary(total=len(questions))
    items: list[Stage2Item] = []
    started = time.time()

    for index, question in enumerate(questions):
        if pause_sec > 0 and index > 0:
            time.sleep(pause_sec)

        item = Stage2Item(
            id=question.id,
            question=question.question,
            gold_is_ambiguous=question.is_ambiguous,
            ambiguity_axis=question.ambiguity_axis,
            expected_clarification_keywords=list(question.expected_clarification_keywords),
        )

        try:
            result = judge.judge(question.question)
        except LLMFatalError as e:
            summary.aborted = True
            summary.abort_reason = str(e)
            logger.error("stage2 evaluation aborted (id=%s): %s", question.id, e)
            break
        except LLMTransientError as e:
            item.confusion = "FAILED"
            item.error = str(e)
            summary.failed += 1
            items.append(item)
            logger.warning("stage2 judgement failed (id=%s): %s", question.id, e)
            continue

        item.judged_ask = result.judged_ask
        item.clarification = result.clarification
        item.raw_response = result.raw_response
        item.prompt_tokens = result.prompt_tokens
        item.completion_tokens = result.completion_tokens
        item.latency_ms = result.latency_ms

        item.confusion = _confusion(question.is_ambiguous, result.judged_ask)
        summary.judged += 1
        summary.prompt_tokens += result.prompt_tokens
        summary.completion_tokens += result.completion_tokens

        if item.confusion == "TP":
            summary.tp += 1
            summary.axis2_scored += 1
            item.matched_keywords = _match_keywords(
                result.clarification, question.expected_clarification_keywords
            )
            item.keyword_match = bool(item.matched_keywords)
            if item.keyword_match:
                summary.axis2_keyword_match += 1
        elif item.confusion == "FN":
            summary.fn += 1
        elif item.confusion == "FP":
            summary.fp += 1
        else:  # TN
            summary.tn += 1

        items.append(item)

    summary.elapsed_sec = round(time.time() - started, 2)
    return summary, items


def format_summary(summary: Stage2Summary) -> str:
    """集計を人が読める形にする。"""
    lines = [
        f"見逃し率: {summary.miss_rate}（{summary.fn}/{summary.tp + summary.fn}）",
        f"誤検出率: {summary.false_positive_rate}（{summary.fp}/{summary.fp + summary.tn}）",
        f"混同行列: TP={summary.tp} FN={summary.fn} FP={summary.fp} TN={summary.tn}",
        f"所要: {summary.elapsed_sec}秒 / トークン: {summary.total_tokens}",
    ]
    if summary.failed:
        lines.append(f"※ 判定できなかった問題: {summary.failed}件（プロンプト/出力契約の不備の疑い）")
    if summary.axis2_scored:
        lines.append(
            f"第2軸（キーワード一致・参考値）: {summary.axis2_keyword_match_rate}"
            f"（{summary.axis2_keyword_match}/{summary.axis2_scored}）"
        )
    if summary.aborted:
        lines.append(f"※ 中断: {summary.abort_reason}")
    return "\n".join(lines)
