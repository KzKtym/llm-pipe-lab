"""生成 → 検査 → 実行 を1本につなぎ、失敗したら書き直させる。

各層（generator / sql_validator / sql_executor）は互いを知らない作りにしてある。
それらをどう並べ、何回やり直すかを決めるのがここ。
`self_correct` と `retry_k` を実験パラメータとして振れるようにするため、
再試行の判断はこの層だけが持つ。

検査で落ちた場合も書き直しの対象にする。「許可されていないテーブルを参照しています」
のような理由はモデルにとって十分な手がかりで、実行時エラーと区別する理由がない。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.common.llm_client import LLMTransientError

from .generator import GenerationResult, SqlGenerator
from .sql_executor import QueryResult, SqlExecutor


@dataclass
class Attempt:
    """失敗した1回の試行。書き直しの材料になる。"""

    sql: str
    error: str
    error_kind: str


@dataclass
class PipelineResult:
    """1つの質問に対する最終結果。

    Attributes:
        ok: 最終的にSQLを実行できたか（結果が正しいかは見ていない）
        sql: 最後に生成したSQL
        result: 最後の実行結果。生成自体に失敗した場合は None
        prompt: 最後に送ったプロンプト
        attempts: 生成を試みた回数
        history: 失敗した試行の記録（最後の1回が成功していれば、それは含まない）
        error / error_kind: 失敗した場合の内容
    """

    question: str
    ok: bool = False
    sql: str = ""
    result: QueryResult | None = None
    prompt: str = ""
    model: str = ""
    attempts: int = 0
    history: list[Attempt] = field(default_factory=list)
    error: str = ""
    error_kind: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def rows(self) -> list[tuple]:
        return self.result.rows if self.result else []


class Nl2SqlPipeline:
    """質問を受け取り、実行できるSQLになるまで（上限つきで）試みる。

    `LLMFatalError` は握りつぶさずそのまま送出する。認証切れやレート超過は
    次の質問でも同じ結果になるため、評価ランナー側でループごと止めるべきもの。
    `LLMTransientError` はこの質問の失敗として扱い、呼び出し側は次へ進める。
    """

    def __init__(
        self,
        generator: SqlGenerator,
        executor: SqlExecutor,
        *,
        self_correct: bool = False,
        retry_k: int = 1,
    ):
        self.generator = generator
        self.executor = executor
        self.self_correct = self_correct
        self.retry_k = max(0, retry_k)

    @property
    def max_attempts(self) -> int:
        return 1 + (self.retry_k if self.self_correct else 0)

    def run(self, question: str) -> PipelineResult:
        outcome = PipelineResult(question=question)
        previous_sql: str | None = None
        previous_error: str | None = None

        for attempt in range(1, self.max_attempts + 1):
            outcome.attempts = attempt

            try:
                generation: GenerationResult = self.generator.generate(
                    question, previous_sql=previous_sql, error=previous_error
                )
            except LLMTransientError as e:
                # この質問だけ諦める。プロンプトは組み立て済みでないので残せない
                outcome.error = str(e)
                outcome.error_kind = "llm"
                return outcome

            outcome.sql = generation.sql
            outcome.prompt = generation.prompt
            outcome.model = generation.model
            outcome.prompt_tokens += generation.prompt_tokens
            outcome.completion_tokens += generation.completion_tokens
            outcome.latency_ms += generation.latency_ms

            result = self.executor.run(generation.sql)
            outcome.result = result

            if result.ok:
                outcome.ok = True
                outcome.error = ""
                outcome.error_kind = ""
                return outcome

            outcome.error = result.error
            outcome.error_kind = result.error_kind
            outcome.history.append(
                Attempt(sql=generation.sql, error=result.error, error_kind=result.error_kind)
            )

            previous_sql = generation.sql
            previous_error = result.error

        return outcome
