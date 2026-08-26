"""実験1回分を組み立てて走らせ、結果を記録する。

    パラメータ
        → スキーマ取得・整形
        → 生成器・実行器・パイプラインの組み立て
        → 評価データを流す
        → Nl2SqlExperiment に保存、明細はファイルへ

呼び出し側（ビュー・CLI）はこのモジュールだけを見ればよい。
依存の向きは `services → core / store` の一方向で、移植元の RAG 実験ツールと同じ。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from django.conf import settings

from app.common.llm_client import LLMFatalError, get_client

from .core.evaluator import dataset_digest, evaluate, format_summary, load_questions
from .core.generator import FewShotExample, SqlGenerator
from .core.pipeline import Nl2SqlPipeline
from .core.schema_introspector import introspect, render_schema_text
from .core.sql_executor import SqlExecutor, allowed_tables_from, connect_demo
from .models import Nl2SqlExperiment
from .params import Nl2SqlParams
from .store import save_details

logger = logging.getLogger(__name__)

#: 評価データの既定の場所
DEFAULT_QUESTIONS = Path("sample") / "sales" / "evaluation_questions.json"
#: few-shot の例示。評価データとは別に用意する（後述）
DEFAULT_FEW_SHOT = Path("sample") / "sales" / "few_shot_examples.json"
#: schema_desc: hint で読み込む、データ固有の注意事項
DEFAULT_HINTS = Path("sample") / "sales" / "schema_hints.md"


def _resolve(path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else Path(settings.BASE_DIR) / path


def load_few_shot(count: int, path=DEFAULT_FEW_SHOT) -> tuple[FewShotExample, ...]:
    """例示を読み込む。

    **評価データから例示を取ってはいけない。** 測る対象の答えをそのまま
    プロンプトに載せることになり、スコアが意味を失う。別ファイルに用意する。

    Raises:
        FileNotFoundError: `count > 0` なのに例示ファイルが無い場合
    """
    if count <= 0:
        return ()

    resolved = _resolve(path)
    if not resolved.exists():
        raise FileNotFoundError(
            f"few_shot: {count} を指定されましたが、例示ファイルがありません: {resolved}"
            "（評価データからの流用は不可。別途用意してください）"
        )

    raw = json.loads(resolved.read_text(encoding="utf-8"))
    examples = [FewShotExample(question=r["question"], sql=r["sql"]) for r in raw]
    if len(examples) < count:
        raise ValueError(
            f"例示が足りません（要求 {count} / 用意 {len(examples)}）: {resolved}"
        )
    return tuple(examples[:count])


def load_hints(params: Nl2SqlParams, path=DEFAULT_HINTS) -> str:
    """`schema_desc: hint` のときだけ注意事項を読む。

    ファイルが無ければエラーにする。黙って空文字で進むと、記録上は `hint` なのに
    実際には何も渡していない、という食い違いが起きる。

    Raises:
        FileNotFoundError: `hint` 指定なのにファイルが無い場合
    """
    if not params.uses_hints:
        return ""

    resolved = _resolve(path)
    if not resolved.exists():
        raise FileNotFoundError(
            f"schema_desc: hint を指定されましたが、注意事項のファイルがありません: {resolved}"
        )
    return resolved.read_text(encoding="utf-8").strip()


def build_schema_text(params: Nl2SqlParams) -> tuple[str, set[str]]:
    """スキーマを読み、プロンプト用テキストと参照許可テーブルを返す。"""
    connection = connect_demo(params.schema)
    try:
        snapshot = introspect(connection, params.schema)
    finally:
        connection.close()

    if not snapshot.tables:
        raise ValueError(f"スキーマにテーブルがありません: {params.schema}")

    text = render_schema_text(snapshot, include_comments=params.include_comments)
    return text, allowed_tables_from(snapshot)


def run_experiment(
    name: str,
    raw_params: dict,
    *,
    questions_path=DEFAULT_QUESTIONS,
    few_shot_path=DEFAULT_FEW_SHOT,
    pause_sec: float = 0.0,
) -> dict:
    """実験を1回走らせ、結果を保存して返す。

    Returns:
        `{"status": "success", "experiment_id": int, "summary": EvalSummary, "text": str}`
        または `{"status": "error", "error": str}`

    `LLMFatalError` は評価ランナーが受け止めて `aborted` を立てるため、
    ここまで上がってこない。それ以外の想定外の例外はログに残して error を返す。
    """
    try:
        params = Nl2SqlParams.from_dict(raw_params)
    except (ValueError, TypeError) as e:
        return {"status": "error", "error": str(e)}

    executor = None
    try:
        schema_text, allowed = build_schema_text(params)
        few_shot = load_few_shot(params.few_shot, few_shot_path)
        hints = load_hints(params)
        questions = load_questions(_resolve(questions_path))
        digest = dataset_digest(questions)

        executor = SqlExecutor(
            schema=params.schema,
            allowed_tables=allowed,
            max_rows=params.max_rows,
            timeout_ms=params.timeout_ms,
        )
        generator = SqlGenerator(
            get_client(params.model),
            schema_text=schema_text,
            few_shot=few_shot,
            reference_date=params.reference_date_value,
            dialect=params.dialect,
            hints=hints,
            max_tokens=params.max_tokens,
            temperature=params.temperature,
        )
        pipeline = Nl2SqlPipeline(
            generator,
            executor,
            self_correct=params.self_correct,
            retry_k=params.retry_k,
        )

        # 代表プロンプトを1本だけ残す。プロンプトはスキーマ説明が大半で、
        # 質問文以外はどの問題でも同じ。組み立てるだけなのでLLMは呼ばない
        prompt_sample = (
            generator.build_prompt(questions[0].question) if questions else ""
        )

        summary, items = evaluate(questions, pipeline, executor, pause_sec=pause_sec)

    except LLMFatalError as e:
        # evaluate 内で捕捉されるはずだが、組み立て時に出た場合の保険
        logger.error("run_experiment aborted: %s", e)
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.exception("run_experiment failed (params=%s)", raw_params)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        if executor is not None:
            executor.close()

    experiment = Nl2SqlExperiment.objects.create(
        name=name or "",
        parameters=params.to_dict(),
        schema_snapshot=schema_text,
        prompt_sample=prompt_sample,
        dataset_digest=digest,
        dataset_path=str(questions_path),
        question_count=summary.total,
        scored=summary.scored,
        executed=summary.executed,
        correct=summary.correct,
        gold_failed=summary.gold_failed,
        execution_accuracy=summary.execution_accuracy,
        valid_sql_rate=summary.valid_sql_rate,
        by_tag={tag: {"correct": s.correct, "total": s.total} for tag, s in summary.by_tag.items()},
        by_error_kind=dict(summary.by_error_kind),
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
            "parameters": params.to_dict(),
            "parameter_line": params.summary_line(),
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
