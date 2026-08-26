"""質問とスキーマ説明から、SQLを1つ生成する。

このモジュールは「プロンプトを組み立てて投げ、返ってきたテキストからSQLを取り出す」
までを担う。検査も実行もしない（それぞれ sql_validator / sql_executor の責務）。
再試行のループも持たない。生成→実行→エラーを戻して再生成、の組み立ては
上位のパイプラインが行う。ここが提供するのは、そのときに渡す `error` 引数だけ。

組み立てたプロンプトは `GenerationResult.prompt` に丸ごと残す。
プロンプトは実験パラメータそのものなので、後から「何を送ったのか」を
再現できないと比較にならない。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.common.llm_client import BaseLLMClient

DEFAULT_MAX_TOKENS = 800
DEFAULT_DIALECT = "PostgreSQL"

# コードフェンスは文中のどこにあっても拾う。
# 「以下のSQLです」のような前置きを付けてくるモデルがあるため、
# 全体一致（sql_validator.strip_code_fence）では取りこぼす。
_FENCE_BLOCK = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```", re.DOTALL)
_SQL_START = re.compile(r"\b(SELECT|WITH)\b", re.IGNORECASE)


@dataclass
class FewShotExample:
    question: str
    sql: str


@dataclass
class GenerationResult:
    """1回の生成の結果。

    Attributes:
        sql: 取り出したSQL
        raw: LLMの出力そのもの（取り出しに失敗したときの調査用）
        prompt: 実際に送ったプロンプト全文
        model: 使ったモデル
        prompt_tokens / completion_tokens / latency_ms: 使用量
    """

    sql: str
    raw: str = ""
    prompt: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def extract_sql(text: str) -> str:
    """LLMの出力からSQLを取り出す。

    次の順に試す。

        1. コードフェンスがあれば、最初のブロックの中身
        2. SELECT / WITH が現れる位置から末尾まで
        3. どちらも無ければ、前後の空白を落としただけの文字列

    2番目が要るのは、フェンス無しで「このクエリで取得できます。SELECT ...」と
    返してくる場合があるため。
    """
    if not text:
        return ""

    fenced = _FENCE_BLOCK.search(text)
    if fenced:
        return fenced.group(1).strip()

    start = _SQL_START.search(text)
    if start:
        return text[start.start():].strip()

    return text.strip()


def build_prompt(
    question: str,
    schema_text: str,
    *,
    few_shot: tuple[FewShotExample, ...] = (),
    reference_date: date | None = None,
    dialect: str = DEFAULT_DIALECT,
    hints: str = "",
    previous_sql: str | None = None,
    error: str | None = None,
) -> str:
    """プロンプトを組み立てる。

    Args:
        question: 日本語の質問
        schema_text: `schema_introspector.render_schema_text()` の出力
        few_shot: 例示。0件なら例のセクションごと出さない
        reference_date: 「先月」「今年」の基準日。**None なら基準日を書かない**。
            評価時は必ず固定すること。書かないとモデルが実行日を推測し、
            同じ質問でも日によって答えが変わって比較できなくなる
        dialect: プロンプトに書くSQL方言名
        hints: このデータ固有の注意事項。スキーマ説明の**後ろ**、質問の直前に置く。
            長いスキーマ説明の中に埋もれた注意書きが判断に結び付いていない、という
            観測（issue_081412）を受けて、目立つ位置に短く出せるようにしたもの
        previous_sql / error: 直前の試行が失敗したときの内容（self_correct 用）
    """
    parts: list[str] = [
        f"あなたは {dialect} に詳しいデータエンジニアです。",
        "与えられたスキーマだけを使い、質問に答えるSQLを1つ書いてください。",
        "",
        "# ルール",
        "- 出力はSQLのみ。説明・前置き・コードフェンスを付けない",
        "- SELECT 文のみ。データを変更する文は書かない",
        "- 下のスキーマに存在する表と列だけを使う。無いものを推測で作らない",
        "- 列のコメントを必ず読む。値の表記ゆれや欠損の扱いが書かれている",
        "- 上位N件を問われた場合は ORDER BY と LIMIT を使う",
        "- 複数の文に分けず、1つのSQLで答える",
    ]

    if reference_date is not None:
        parts += [
            "",
            "# 基準日",
            f"今日は {reference_date.isoformat()} です。"
            "「先月」「今年」などの相対的な表現は、この日付を基準に解釈してください。",
        ]

    parts += ["", "# スキーマ", schema_text]

    if hints:
        parts += ["", "# 注意（このデータ固有）", hints.strip()]

    if few_shot:
        parts += ["", "# 例"]
        for example in few_shot:
            parts += [f"質問: {example.question}", f"SQL: {example.sql}", ""]
        # 末尾の空行が二重になるのを避ける
        parts = parts[:-1]

    parts += ["", "# 質問", question]

    if previous_sql or error:
        parts += ["", "# 直前の試行", "次のSQLは失敗しました。原因を直したSQLを書いてください。"]
        if previous_sql:
            parts += ["", "SQL:", previous_sql]
        if error:
            parts += ["", "エラー:", error]

    parts += ["", "# SQL"]
    return "\n".join(parts)


class SqlGenerator:
    """スキーマ説明を固定し、質問ごとにSQLを生成する。

    スキーマ説明は実験の1回を通して変わらないため、コンストラクタで受け取る。
    どこまで説明を載せるか（コメントの有無、テーブルの絞り込み）は
    `render_schema_text()` 側で決めてから渡すこと。

    LLM の例外（`LLMFatalError` / `LLMTransientError`）はそのまま送出する。
    中断するか次の質問へ進むかは評価ランナーが決める。
    """

    def __init__(
        self,
        client: BaseLLMClient,
        *,
        schema_text: str,
        few_shot: tuple[FewShotExample, ...] = (),
        reference_date: date | None = None,
        dialect: str = DEFAULT_DIALECT,
        hints: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ):
        self.client = client
        self.schema_text = schema_text
        self.few_shot = tuple(few_shot)
        self.reference_date = reference_date
        self.dialect = dialect
        self.hints = hints
        self.max_tokens = max_tokens
        self.temperature = temperature

    def build_prompt(
        self,
        question: str,
        *,
        previous_sql: str | None = None,
        error: str | None = None,
    ) -> str:
        return build_prompt(
            question,
            self.schema_text,
            few_shot=self.few_shot,
            reference_date=self.reference_date,
            dialect=self.dialect,
            hints=self.hints,
            previous_sql=previous_sql,
            error=error,
        )

    def generate(
        self,
        question: str,
        *,
        previous_sql: str | None = None,
        error: str | None = None,
    ) -> GenerationResult:
        """質問からSQLを1つ生成する。

        Args:
            question: 日本語の質問
            previous_sql / error: 直前の試行が失敗した場合に渡す（self_correct 用）
        """
        prompt = self.build_prompt(question, previous_sql=previous_sql, error=error)
        response = self.client.complete(
            prompt, max_tokens=self.max_tokens, temperature=self.temperature
        )
        return GenerationResult(
            sql=extract_sql(response.text),
            raw=response.text,
            prompt=prompt,
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=response.latency_ms,
        )
