"""評価データを一括で流し、実行結果一致率を出す。

移植元の RAG 実験ツールの `core/evaluation/runner.py` と同じ位置づけ。指標だけが違う。

    RAG    : MRR / Recall@K
    NL→SQL : 実行結果一致率（execution accuracy）/ 実行成功率（valid SQL rate）

主指標は実行結果一致率。SQLの文字列一致は書き方の違いで落ちるため見ない。

タグ別の内訳を必ず出す。全体の正答率が 0.62 だと分かっても次に何をすればよいか
決まらないが、「`表記ゆれ` が 0.2、`集計` が 0.9」と分かれば手の打ちどころが決まる。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.common.llm_client import LLMFatalError

from .pipeline import Nl2SqlPipeline
from .result_compare import DEFAULT_DIGITS, compare_query_results
from .sql_executor import SqlExecutor

logger = logging.getLogger(__name__)


@dataclass
class EvalQuestion:
    """評価データ1件。`sample/sales/evaluation_questions.json` の1要素に対応する。

    `ordered` は「質問が並び順まで要求しているか」。既定は False。

    正解SQLの ORDER BY から推測してはいけない。「支払方法ごとの取引件数は？」の
    ような順序を問わない質問でも、正解SQLには見やすさのために ORDER BY が
    書かれることが多く、それを根拠にすると**正しい答えを不正解と判定する**。
    """

    id: str
    question: str
    gold_sql: str
    tags: list[str] = field(default_factory=list)
    note: str = ""
    ordered: bool = False


@dataclass
class EvalItem:
    """1問の結果。

    正解SQL・順序判定・解釈メモも持たせる。明細ファイルだけで結果を読めるように
    するため。評価データは実験後に変わり得るので、後から引き直すと
    **実験時点と食い違った内容を表示してしまう。**
    """

    id: str
    question: str
    tags: list[str] = field(default_factory=list)
    gold_sql: str = ""
    ordered: bool = False
    note: str = ""
    generated_sql: str = ""
    executed: bool = False
    match: bool = False
    reason: str = ""
    error: str = ""
    error_kind: str = ""
    attempts: int = 0
    gold_failed: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class TagStat:
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return round(self.correct / self.total, 4) if self.total else 0.0


@dataclass
class EvalSummary:
    """全体のスコア。

    Attributes:
        scored: 採点対象の件数（正解SQLが実行できた問題のみ）
        executed: 生成SQLを実行できた件数
        correct: 実行結果が一致した件数
        execution_accuracy: correct / scored … **主指標**
        valid_sql_rate: executed / scored
        gold_failed: 正解SQLが実行できなかった件数。**0 でなければ評価データの不備**
        aborted: 途中で打ち切ったか（認証切れ・レート超過）
    """

    total: int = 0
    scored: int = 0
    executed: int = 0
    correct: int = 0
    gold_failed: int = 0
    by_tag: dict[str, TagStat] = field(default_factory=dict)
    by_error_kind: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_sec: float = 0.0
    aborted: bool = False
    abort_reason: str = ""

    @property
    def execution_accuracy(self) -> float:
        return round(self.correct / self.scored, 4) if self.scored else 0.0

    @property
    def valid_sql_rate(self) -> float:
        return round(self.executed / self.scored, 4) if self.scored else 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def load_questions(path: str | Path) -> list[EvalQuestion]:
    """評価データのJSONを読む。

    未知のキーは無視する。評価データ側に `note` 以外の補足が増えても
    こちらを直さずに済むようにしておく。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    questions = []
    for index, item in enumerate(raw, start=1):
        questions.append(
            EvalQuestion(
                id=str(item.get("id") or f"q{index:03d}"),
                question=item["question"],
                gold_sql=item["gold_sql"],
                tags=list(item.get("tags") or []),
                note=item.get("note", ""),
                ordered=bool(item.get("ordered", False)),
            )
        )
    return questions


def dataset_digest(questions: list[EvalQuestion]) -> str:
    """評価データの版を表すダイジェストを返す。

    **採点に影響する内容だけ**を対象にする。整形やコメントの手直しで版が変わると、
    実際には同条件なのに「別の評価データ」と見えてしまうため、
    ファイルのバイト列ではなく、読み込んだ内容から作る。

    対象: `id` / `question` / `gold_sql` / `ordered` / `tags`
    対象外: `note`（解釈の覚え書きで、採点には効かない）

    `tags` を含めるのは、タグが変わるとタグ別の内訳が比較できなくなるため。
    """
    canonical = [
        {
            "id": q.id,
            "question": q.question,
            "gold_sql": q.gold_sql,
            "ordered": q.ordered,
            "tags": sorted(q.tags),
        }
        for q in sorted(questions, key=lambda q: q.id)
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate(
    questions: list[EvalQuestion],
    pipeline: Nl2SqlPipeline,
    executor: SqlExecutor,
    *,
    digits: int = DEFAULT_DIGITS,
    pause_sec: float = 0.0,
) -> tuple[EvalSummary, list[EvalItem]]:
    """評価データを順に流し、集計と明細を返す。

    Args:
        pipeline: 生成〜実行のパイプライン
        executor: **正解SQLの実行に使う**。生成側と同じ上限（LIMIT・タイムアウト）を
            かけるため、パイプラインが使っているものと同じ設定の executor を渡すこと。
            条件を揃えないと、行数上限の違いだけで不一致になる
        pause_sec: 1問ごとに待つ秒数。**採点結果には影響しない**ので実験パラメータに
            しない。LLM側の分あたりトークン上限（TPM）に当たると 429 が返り、
            `LLMFatalError` で全体が中断する。プロンプトが大きいモデルでは
            間隔を空けないと35問を流しきれない

    Returns:
        (集計, 明細)。`LLMFatalError` が出た場合はそこで打ち切り、
        それまでの結果と `aborted=True` を返す
    """
    summary = EvalSummary(total=len(questions))
    items: list[EvalItem] = []
    started = time.time()

    for index, question in enumerate(questions):
        # 2問目以降だけ待つ（先頭で待っても意味がない）
        if pause_sec > 0 and index > 0:
            time.sleep(pause_sec)

        item = EvalItem(
            id=question.id,
            question=question.question,
            tags=list(question.tags),
            gold_sql=question.gold_sql,
            ordered=question.ordered,
            note=question.note,
        )

        gold = executor.run(question.gold_sql)
        if not gold.ok:
            # 正解SQLが流せないのはモデルの責任ではない。採点対象から外して数だけ残す
            item.gold_failed = True
            item.error = gold.error
            item.error_kind = "gold"
            summary.gold_failed += 1
            items.append(item)
            logger.warning(
                "gold_sql failed (id=%s, kind=%s): %s", question.id, gold.error_kind, gold.error
            )
            continue

        try:
            outcome = pipeline.run(question.question)
        except LLMFatalError as e:
            # 次の質問でも同じ結果になる。ここで打ち切る
            summary.aborted = True
            summary.abort_reason = str(e)
            logger.error("evaluation aborted (id=%s): %s", question.id, e)
            break

        summary.scored += 1
        item.generated_sql = outcome.sql
        item.executed = outcome.ok
        item.attempts = outcome.attempts
        item.error = outcome.error
        item.error_kind = outcome.error_kind
        item.prompt_tokens = outcome.prompt_tokens
        item.completion_tokens = outcome.completion_tokens
        item.latency_ms = outcome.latency_ms

        summary.prompt_tokens += outcome.prompt_tokens
        summary.completion_tokens += outcome.completion_tokens

        if outcome.ok and outcome.result is not None:
            summary.executed += 1
            comparison = compare_query_results(
                gold,
                outcome.result,
                gold_sql=question.gold_sql,
                ordered=question.ordered,
                digits=digits,
            )
            item.match = comparison.match
            item.reason = comparison.reason
        else:
            item.reason = outcome.error_kind or "実行できなかった"

        if item.error_kind:
            summary.by_error_kind[item.error_kind] = (
                summary.by_error_kind.get(item.error_kind, 0) + 1
            )
        if item.match:
            summary.correct += 1

        for tag in item.tags:
            stat = summary.by_tag.setdefault(tag, TagStat())
            stat.total += 1
            if item.match:
                stat.correct += 1

        items.append(item)

    summary.elapsed_sec = round(time.time() - started, 2)
    return summary, items


def format_summary(summary: EvalSummary) -> str:
    """集計を人が読める形にする。実験一覧に貼れる粒度にとどめる。"""
    lines = [
        f"実行結果一致率: {summary.execution_accuracy}"
        f"（{summary.correct}/{summary.scored}）",
        f"実行成功率: {summary.valid_sql_rate}（{summary.executed}/{summary.scored}）",
        f"所要: {summary.elapsed_sec}秒 / トークン: {summary.total_tokens}",
    ]
    if summary.gold_failed:
        lines.append(f"※ 正解SQLが実行できなかった問題: {summary.gold_failed}件（評価データの不備）")
    if summary.aborted:
        lines.append(f"※ 中断: {summary.abort_reason}")
    if summary.by_tag:
        lines.append("タグ別:")
        for tag in sorted(summary.by_tag):
            stat = summary.by_tag[tag]
            lines.append(f"  {tag}: {stat.accuracy}（{stat.correct}/{stat.total}）")
    if summary.by_error_kind:
        breakdown = ", ".join(
            f"{kind}={count}" for kind, count in sorted(summary.by_error_kind.items())
        )
        lines.append(f"失敗の内訳: {breakdown}")
    return "\n".join(lines)
