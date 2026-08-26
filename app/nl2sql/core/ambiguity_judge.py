"""段階2：`AmbiguityJudge`（LLMの総合判断、`hint` で凍結・`issue_c009`/`c010` の基準線）と
`phrase_disclosure`（ルール判定の結果を文言にする、`issue_c014` 工程②）の2役を持つ。

`issue_c013` 論点E「既存資産の継承範囲」：`AmbiguityJudge` の LLM 呼び出しは
「曖昧さの判定」から「確認文言の言い回し生成」へ役割を絞って再利用する、とした。
**このファイルの `AmbiguityJudge` / `parse_judgement` / `build_prompt` は変更していない**
（`exp_1`〜`exp_10` の基準線はコードの参照先として残すため）。新しい役割は
`phrase_disclosure` / `PhrasingResult` として追加した。

## `AmbiguityJudge`（旧・工程①③一体版。`hint` で凍結）

SQLは生成しない（`issue_c009` 確定事項c）。曖昧さの型は「母集合の境界」
1型のみを見る（`issue_c007` 論点4）。この10件のどれも例示しない
（`issue_c009` 確定事項d）。

スキーマ説明は既定では渡さない（`issue_c007` 論点2「まず全文なしで測る」）。
`schema_text` / `hints` を渡すと、その分だけプロンプトに追加される
（`issue_c010`）。**追加するだけで、既存の指示文言は変えない**（確定事項e）。

出力契約は固定の2形式のみ。パースできない場合は `LLMTransientError` を送出する
（段階1の失敗分類を流用し、新しい例外は作らない）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.common.llm_client import BaseLLMClient, LLMTransientError

DEFAULT_MAX_TOKENS = 300

_INSTRUCTIONS = """\
あなたはデータ分析システムの一次窓口です。ユーザーの質問を見て、
そのまま集計してよいか、先に確認すべきことがあるかを判断してください。

# 確認すべき観点
集計の対象範囲（母集合）が、質問文だけで一つに定まるかどうかを判断してください。
特に「活動実績のない対象（取引のない店舗、購入していない会員、廃番・退会・閉店などで
除外されうる対象）」を含めるかどうかが質問文に明記されていない場合は、確認が必要です。
それ以外の曖昧さ（言葉づかいの粗さなど）は判断の対象にしないでください。"""

_OUTPUT_FORMAT = """\
# 出力形式
次のいずれかの形式だけで答えてください。説明や前置きは付けないでください。

集計対象が一つに定まる場合:
JUDGEMENT: ANSWER

確認が必要な場合:
JUDGEMENT: CLARIFY
QUESTION: <ユーザーへの確認文言を1文で>"""

_JUDGEMENT_RE = re.compile(r"JUDGEMENT:\s*(ANSWER|CLARIFY)", re.IGNORECASE)
_QUESTION_RE = re.compile(r"QUESTION:\s*(.+)", re.IGNORECASE)


def build_prompt(question: str, *, schema_text: str = "", hints: str = "") -> str:
    """判断ステップのプロンプトを組み立てる。

    `schema_text` / `hints` が空なら、`issue_c009` 時点と同じ文言になる。
    指示・出力形式・質問の各節の文言はそのままに、間に
    `# スキーマ` / `# 注意（このデータ固有）` を挟むだけ（`issue_c010` 確定事項e）。
    """
    parts = [_INSTRUCTIONS]
    if schema_text:
        parts += ["", "# スキーマ", schema_text]
    if hints:
        parts += ["", "# 注意（このデータ固有）", hints.strip()]
    parts += ["", _OUTPUT_FORMAT, "", "# 質問", question, "", "# 出力"]
    return "\n".join(parts)


def parse_judgement(text: str) -> tuple[bool, str]:
    """判断ステップの出力を解釈する。

    Returns:
        (judged_ask, clarification)

    Raises:
        LLMTransientError: `JUDGEMENT:` 行が無い、値が2形式のどちらでもない、
            または `CLARIFY` なのに `QUESTION:` 行が無い場合
    """
    match = _JUDGEMENT_RE.search(text or "")
    if not match:
        raise LLMTransientError(f"JUDGEMENT行が見つかりません: {text!r}")

    if match.group(1).upper() == "ANSWER":
        return False, ""

    question_match = _QUESTION_RE.search(text)
    if not question_match:
        raise LLMTransientError(f"CLARIFY なのに QUESTION行が見つかりません: {text!r}")
    return True, question_match.group(1).strip()


@dataclass
class JudgementResult:
    """1件の判断結果。SQLは持たない。"""

    judged_ask: bool
    clarification: str
    raw_response: str
    prompt: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AmbiguityJudge:
    """判断ステップだけを走らせる。段階1の `SqlGenerator` に相当するが、SQLは扱わない。

    `schema_text` / `hints` はコンストラクタで固定する（段階1の `SqlGenerator` が
    スキーマ説明を実験の1回を通して固定するのと同じ考え方）。
    """

    def __init__(
        self,
        client: BaseLLMClient,
        *,
        schema_text: str = "",
        hints: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ):
        self.client = client
        self.schema_text = schema_text
        self.hints = hints
        self.max_tokens = max_tokens
        self.temperature = temperature

    def judge(self, question: str) -> JudgementResult:
        """質問1件について判断する。

        Raises:
            LLMFatalError: 認証切れ・レート超過など（呼び出し側でループ中断）
            LLMTransientError: 通信断・単発の不備・出力のパース失敗
        """
        prompt = build_prompt(question, schema_text=self.schema_text, hints=self.hints)
        response = self.client.complete(
            prompt, max_tokens=self.max_tokens, temperature=self.temperature
        )
        judged_ask, clarification = parse_judgement(response.text)
        return JudgementResult(
            judged_ask=judged_ask,
            clarification=clarification,
            raw_response=response.text,
            prompt=prompt,
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=response.latency_ms,
        )


# --- 工程②：ルール判定の結果を文言にする（issue_c014） -----------------------
#
# 判定（③、`population_rules.classify` の結果）は済んでいる。ここでやるのは
# 「その結果をどう言うか」だけ。新しい判断はしない（issue_c013 論点F）。
# `rule_result` は `population_rules.RuleResult` を想定するが、
# `.axes` / `.needs_clarification` / `.defaults_applied` を持つダックタイピングで扱う
# （このモジュールから `population_rules` を import しない。段階1の各層が
# 互いを知らない作りにしてあるのと同じ考え方）。

_DISCLOSURE_HEADER = """\
あなたはデータ分析システムの応答文言担当です。次の判定結果だけをもとに、
ユーザーへの応答文を作成してください。新しい判断や数値の計算はしないでください。"""

_DISCLOSURE_GUIDE = """\
# 書き方
- 開示は先に書く。含めた場合も除いた場合も、統合した場合も、独立区分にした場合も、
  何を前提にしたかを必ず告げること（黙ってはいけない）
- 開示した既定には、それぞれ「除く場合はお知らせください」のように、
  **変える手段を必ず1文添える**こと。既定を宣言するだけで終えないこと
- 「確認したいこと」があれば、開示のあとに確認の質問を1文添える。無ければ書かない
- 説明的な前置き・謝辞・SQLは書かない。開示と（あれば）確認だけを書く
- 列名・テーブル名のような英数字の識別子は使わず、日本語の業務の言葉で書くこと"""


# `RuleAxis.name` は監査用の識別子（`stores.close_date` / `店舗の母集合（結合欠落）` 等）で、
# 利用者向けの文にそのまま出す語ではない。②へ渡す前に業務の言葉へ変換する
# （`issue_c018`）。対象になりうる name は `population_rules.py` の列挙
# （`FLAG_COLUMN`・`_MASTER_GROUP_BY_NAME`・`_TIME_AXIS_NAME`、`SYNONYM`/`CATCHALL` の
# 列名）で尽くされている（有限）。ここから `population_rules` は import しない
# （各層が互いを知らない作りを保つ）。値は `DIAGNOSIS.md` の事実（`region` の関西/近畿、
# `channel` の EC/オンライン、`pref`/`gender` の「その他」）に合わせた。
_AXIS_DISPLAY_NAME = {
    "stores.close_date": "閉店した店舗",
    "members.is_active": "退会・休眠の会員",
    "products.is_discontinued": "廃番になった商品",
    "店舗の母集合（結合欠落）": "売上の無い店舗を並べるかどうか",
    "会員の母集合（結合欠落）": "購入の無い会員を並べるかどうか",
    "商品の母集合（結合欠落）": "販売の無い商品を並べるかどうか",
    "時間軸の母集合（暗黙のカレンダー）": "取引の無かった期間を並べるかどうか",
    "regionの表記ゆれ": "「関西」「近畿」表記の店舗",
    "channelの表記ゆれ": "「EC」「オンライン」表記のチャネル",
    "prefの包括値": "都道府県が「その他」の会員",
    "genderの包括値": "性別が「その他」の会員",
}


def _display_name(axis_name: str) -> str:
    """利用者向けの言い方に変換する。未知の名前はそのまま返す（フェイルセーフ）。"""
    return _AXIS_DISPLAY_NAME.get(axis_name, axis_name)


def build_disclosure_prompt(question: str, rule_result) -> str:
    """`population_rules.classify` の結果から、文言生成用のプロンプトを組み立てる。"""
    default_lines = [
        f"- {_display_name(axis.name)}: 「{axis.default}」を既定として適用"
        for axis in rule_result.defaults_applied
    ]
    clarify_lines = [
        f"- {_display_name(axis.name)} について、質問文が確定していない"
        for axis in rule_result.axes
        if axis.kind == 1 and not axis.resolved
    ]

    parts = [
        _DISCLOSURE_HEADER,
        "",
        "# 開示する既定（適用した前提。必ず1つずつ文章にすること）",
        *(default_lines or ["- （無し）"]),
        "",
        "# 確認したいこと（あれば、開示のあとに1文添える）",
        *(clarify_lines or ["- （無し）"]),
        "",
        _DISCLOSURE_GUIDE,
        "",
        "# 質問",
        question,
        "",
        "# 応答文",
    ]
    return "\n".join(parts)


@dataclass
class PhrasingResult:
    """工程②の結果。判定は持たない（③はすでに終わっている）。"""

    text: str
    raw_response: str = ""
    prompt: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def phrase_disclosure(
    client: BaseLLMClient,
    question: str,
    rule_result,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.0,
) -> PhrasingResult:
    """ルール判定結果（③）を自然文にする（②）。判定はしない。

    開示（型2〜4の既定）も確認（型1）も無ければ、LLMを呼ばず空文字を返す
    （言うことが無い問いにまでコストをかけない）。

    Raises:
        LLMFatalError: 認証切れ・レート超過など
        LLMTransientError: 通信断・単発の不備・応答が空
    """
    if not rule_result.defaults_applied and not rule_result.needs_clarification:
        return PhrasingResult(text="")

    prompt = build_disclosure_prompt(question, rule_result)
    response = client.complete(prompt, max_tokens=max_tokens, temperature=temperature)
    text = (response.text or "").strip()
    if not text:
        raise LLMTransientError(f"応答文が空です: prompt={prompt!r}")

    return PhrasingResult(
        text=text,
        raw_response=response.text,
        prompt=prompt,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        latency_ms=response.latency_ms,
    )
