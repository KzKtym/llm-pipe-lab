"""段階2実験1回分の組み立てと記録。

段階1の `services.py` と同じ位置づけ。SQLは生成しない（判断ステップだけを流す）ため、
SQL実行の組み立ては無い。スキーマ取得は `schema_desc` を指定したときだけ行う
（`issue_c010`）。指定しなければ `issue_c009` のベースラインと同じく、
判断ステップにスキーマを一切渡さない。

依存の向きは `stage2_services → core / stage2_store` の一方向で、段階1と同じ約束。
"""
from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings

from app.common.llm_client import LLMFatalError, get_client

from .core.ambiguity_judge import AmbiguityJudge
from .core.schema_introspector import introspect, render_schema_text
from .core.sql_executor import connect_demo
from .core.stage2_evaluator import dataset_digest, evaluate, format_summary, load_questions
from .models import Nl2SqlStage2Experiment
from .params import Nl2SqlParams
from .services import DEFAULT_HINTS, load_hints
from .stage2_store import save_details

logger = logging.getLogger(__name__)

#: 評価データの既定の場所
DEFAULT_QUESTIONS = Path("sample") / "sales" / "stage2_questions.json"

#: 実験パラメータ（`issue_c009` で決めた既定値。段階1の既定と揃える）
DEFAULT_MODEL = "openai:gpt-4.1-mini"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_SCHEMA = "demo_sales"

#: `schema_desc` の許容値。段階1と同じ3値（`issue_c010`。`schema_mode`/`value_hint` は対象外）
SCHEMA_DESC_VALUES = ("none", "hint", "comment")


def _resolve(path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else Path(settings.BASE_DIR) / path


def _build_schema_text(schema_desc: str, schema: str = DEFAULT_SCHEMA) -> tuple[str, str]:
    """スキーマ説明とヒント文言を取得する。

    段階1の `schema_introspector.introspect` / `render_schema_text` をそのまま
    再利用する（段階2用に別実装しない。`issue_c010`）。`hint` は段階1の
    `services.load_hints()` を再利用し、ファイルが無ければ同じ例外で落ちる。

    Returns:
        (schema_text, hints)
    """
    connection = connect_demo(schema)
    try:
        snapshot = introspect(connection, schema)
    finally:
        connection.close()

    schema_text = render_schema_text(snapshot, include_comments=(schema_desc == "comment"))
    hints = load_hints(Nl2SqlParams(schema_desc=schema_desc), DEFAULT_HINTS)
    return schema_text, hints


def run_stage2_experiment(
    name: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    schema_desc: str | None = None,
    questions_path=DEFAULT_QUESTIONS,
    pause_sec: float = 0.0,
) -> dict:
    """段階2の判断ステップだけを流し、結果を保存して返す。SQLは生成しない。

    Args:
        schema_desc: `None`（既定）なら判断ステップにスキーマを一切渡さない
            （`issue_c009` のベースラインと同じ、DB接続もしない）。
            `"none"` / `"hint"` / `"comment"` を指定すると、段階1と同じ意味で
            スキーマ説明を取得して渡す（`issue_c010`）。それ以外の値は落とす

    Returns:
        `{"status": "success", "experiment_id": int, "summary": Stage2Summary, "text": str}`
        または `{"status": "error", "error": str}`
    """
    try:
        if schema_desc is not None and schema_desc not in SCHEMA_DESC_VALUES:
            raise ValueError(
                f"schema_desc が不正です: {schema_desc!r}"
                f"（許容値: {', '.join(SCHEMA_DESC_VALUES)}）"
            )

        questions = load_questions(_resolve(questions_path))
        digest = dataset_digest(questions)

        if schema_desc is not None:
            schema_text, hints = _build_schema_text(schema_desc)
        else:
            schema_text, hints = "", ""

        judge = AmbiguityJudge(
            get_client(model), schema_text=schema_text, hints=hints, temperature=temperature
        )
        summary, items = evaluate(questions, judge, pause_sec=pause_sec)
    except LLMFatalError as e:
        # evaluate 内で捕捉されるはずだが、組み立て時に出た場合の保険
        logger.error("run_stage2_experiment aborted: %s", e)
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.exception("run_stage2_experiment failed (model=%s)", model)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    parameters = {"model": model, "temperature": temperature, "schema_desc": schema_desc}

    experiment = Nl2SqlStage2Experiment.objects.create(
        name=name or "",
        parameters=parameters,
        dataset_digest=digest,
        dataset_path=str(questions_path),
        question_count=summary.total,
        judged=summary.judged,
        failed=summary.failed,
        tp=summary.tp,
        fn=summary.fn,
        fp=summary.fp,
        tn=summary.tn,
        axis2_scored=summary.axis2_scored,
        axis2_keyword_match=summary.axis2_keyword_match,
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
        },
    )

    return {
        "status": "success",
        "experiment_id": experiment.id,
        "summary": summary,
        "text": format_summary(summary),
    }
