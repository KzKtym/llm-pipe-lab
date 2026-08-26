"""段階2（ルール判定版）実験1回分の組み立てと記録。

`stage2_services.py`（旧・LLM総合判断版）と同じ位置づけ。SQLは生成しない。

    ① `core.axis_extractor.AxisExtractor`   … AI（構造化）
    ③ `core.population_rules.classify`      … ルール（判定。LLMは呼ばない）
    ② `core.ambiguity_judge.phrase_disclosure` … AI（文言化）

依存の向きは `stage2_rule_services → core / stage2_rule_store` の一方向。
"""
from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings

from app.common.llm_client import LLMFatalError, get_client

from .core.axis_extractor import (
    AxisExtractor,
)
from .core.axis_extractor import (
    flag_prompt_digest as compute_flag_prompt_digest,
)
from .core.axis_extractor import (
    prompt_digest as compute_prompt_digest,
)
from .core.flagged_activity import (
    check_flagged_activity,
    has_unrepresented_entity,
    has_unrepresented_time_unit,
)
from .core.stage2_rule_evaluator import (
    dataset_digest,
    evaluate,
    format_summary,
    load_questions,
)
from .models import Nl2SqlStage2RuleExperiment
from .stage2_rule_store import save_details

logger = logging.getLogger(__name__)

#: 評価データの既定の場所。段階1の35問（`issue_c014` 完了条件5：採点に使ってよい）
DEFAULT_QUESTIONS = Path("sample") / "sales" / "evaluation_questions.json"

DEFAULT_MODEL = "openai:gpt-4.1-mini"
DEFAULT_TEMPERATURE = 0.0


def _resolve(path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else Path(settings.BASE_DIR) / path


def run_stage2_rule_experiment(
    name: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    questions_path=DEFAULT_QUESTIONS,
    pause_sec: float = 0.0,
    flagged_activity_checker=check_flagged_activity,
    roster_emptiness_checker=has_unrepresented_entity,
    time_emptiness_checker=has_unrepresented_time_unit,
) -> dict:
    """段階2（ルール判定版）を流し、結果を保存して返す。SQLは生成しない。

    Args:
        flagged_activity_checker: `issue_c015` パターンA。既定は本物のDB照会
            （`core.flagged_activity.check_flagged_activity`）。テストでは
            `None` かフェイクを渡すこと
        roster_emptiness_checker: `issue_c020`。既定は本物のDB照会
            （`core.flagged_activity.has_unrepresented_entity`）。テストでは
            `None` かフェイクを渡すこと
        time_emptiness_checker: `issue_c028` (a)。既定は本物のDB照会
            （`core.flagged_activity.has_unrepresented_time_unit`）。テストでは
            `None` かフェイクを渡すこと

    Returns:
        `{"status": "success", "experiment_id": int, "summary": RuleEvalSummary, "text": str}`
        または `{"status": "error", "error": str}`
    """
    try:
        questions = load_questions(_resolve(questions_path))
        digest = dataset_digest(questions)
        p_digest = compute_prompt_digest()
        flag_p_digest = compute_flag_prompt_digest()

        client = get_client(model)
        extractor = AxisExtractor(client, temperature=temperature)
        summary, items = evaluate(
            questions,
            extractor,
            client,
            pause_sec=pause_sec,
            flagged_activity_checker=flagged_activity_checker,
            roster_emptiness_checker=roster_emptiness_checker,
            time_emptiness_checker=time_emptiness_checker,
        )
    except LLMFatalError as e:
        logger.error("run_stage2_rule_experiment aborted: %s", e)
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.exception("run_stage2_rule_experiment failed (model=%s)", model)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    parameters = {"model": model, "temperature": temperature}

    experiment = Nl2SqlStage2RuleExperiment.objects.create(
        name=name or "",
        parameters=parameters,
        dataset_digest=digest,
        dataset_path=str(questions_path),
        prompt_digest=p_digest,
        flag_prompt_digest=flag_p_digest,
        question_count=summary.total,
        judged=summary.judged,
        failed=summary.failed,
        type1_detected=summary.type1_detected,
        type2_detected=summary.type2_detected,
        type3_detected=summary.type3_detected,
        type4_detected=summary.type4_detected,
        needs_clarification_count=summary.needs_clarification_count,
        prompt_tokens=summary.prompt_tokens,
        completion_tokens=summary.completion_tokens,
        elapsed_sec=summary.elapsed_sec,
        aborted=summary.aborted,
        abort_reason=summary.abort_reason,
    )

    save_details(
        experiment.id,
        items,
        meta={
            "parameters": parameters,
            "questions": str(questions_path),
            "dataset_digest": digest,
            "prompt_digest": p_digest,
            "flag_prompt_digest": flag_p_digest,
        },
    )

    return {
        "status": "success",
        "experiment_id": experiment.id,
        "summary": summary,
        "text": format_summary(summary),
    }
