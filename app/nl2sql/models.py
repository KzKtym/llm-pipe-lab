"""NL→SQL 実験の記録。

移植元の RAG 実験ツールの `Experiment` とは別テーブルにしてある。指標が違う（MRR/Recall@K と
実行結果一致率）ため、同じ列に別の意味の数字を入れると、後から見たときに
どちらの実験か分からなくなる。

将来この2つを1つの器にまとめる可能性はあるが、**2本目を作り終えてから
本当に同じだった部分だけを共通化する**方針。先に抽象化すると、
2本目の要件が見えないまま間違った共通部品を作ることになる。
"""
from django.db import models

from app.common.pricing import estimate_cost_usd, format_cost_usd


class Nl2SqlExperiment(models.Model):
    name = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    #: 実験パラメータ（Nl2SqlParams.to_dict）
    parameters = models.JSONField(default=dict)
    #: 実際にLLMへ渡したスキーマ説明。プロンプトの再現に要る
    schema_snapshot = models.TextField(blank=True)
    #: 代表として1本だけ残すプロンプト全文
    prompt_sample = models.TextField(blank=True)

    #: 評価データの版。内容が変われば値が変わる。
    #: **版が違う実験のスコアは比較できない**ため、必ず記録する
    dataset_digest = models.CharField(max_length=64, blank=True)
    #: 使った評価データの場所（相対パス）
    dataset_path = models.CharField(max_length=300, blank=True)

    question_count = models.IntegerField(default=0)
    #: 採点対象（正解SQLが実行できた問題）
    scored = models.IntegerField(default=0)
    executed = models.IntegerField(default=0)
    correct = models.IntegerField(default=0)
    #: 正解SQLが実行できなかった件数。0でなければ評価データの不備
    gold_failed = models.IntegerField(default=0)

    #: 主指標
    execution_accuracy = models.FloatField(default=0.0)
    valid_sql_rate = models.FloatField(default=0.0)

    #: {タグ: {"correct": n, "total": n}}
    by_tag = models.JSONField(default=dict)
    #: {エラー種別: 件数}
    by_error_kind = models.JSONField(default=dict)

    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    elapsed_sec = models.FloatField(default=0.0)

    aborted = models.BooleanField(default=False)
    abort_reason = models.TextField(blank=True)

    is_starred = models.BooleanField(default=False)

    class Meta:
        db_table = "nl2sql_experiment"
        ordering = ["-id"]

    def __str__(self):
        return f"Nl2SqlExperiment {self.id} ({self.execution_accuracy})"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float | None:
        """概算コスト（USD）。単価表に無いモデルは `None`。

        **列に持たせず、読むたびに導く。** 保存すると単価改定で過去の記録が
        実態と食い違い、しかも「いつの単価で出した値か」が読めなくなる。
        トークン数は事実なので変わらない。単価表は `app/common/pricing.py`。
        """
        return estimate_cost_usd(
            (self.parameters or {}).get("model", ""),
            self.prompt_tokens,
            self.completion_tokens,
        )

    @property
    def cost_display(self) -> str:
        return format_cost_usd(self.cost_usd)

    @property
    def dataset_short(self) -> str:
        """一覧に出すための短縮形。先頭8桁あれば取り違えは起きない。"""
        return self.dataset_digest[:8]


class Nl2SqlStage2Experiment(models.Model):
    """段階2（曖昧な質問を検出して聞き返す）の実験記録。

    `Nl2SqlExperiment` とは別モデル（`issue_c007` 論点3）。指標の形が違う
    （実行結果一致率 ではなく 混同行列＋キーワード一致）ため、同じ列に
    意味の違う数字を混ぜない、という `Nl2SqlExperiment` 冒頭と同じ考え方。
    """

    name = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    #: {"model": "openai:gpt-4.1-mini", "temperature": 0.0, "schema_desc": "none"}
    parameters = models.JSONField(default=dict)

    #: sample/sales/stage2_questions.json の内容ハッシュ（対象列は issue_c009 参照）
    dataset_digest = models.CharField(max_length=64, blank=True)
    dataset_path = models.CharField(max_length=300, blank=True)

    question_count = models.IntegerField(default=0)  # 10

    #: 第1軸：混同行列（実数）。judged = tp+fn+fp+tn、failed とは排他
    judged = models.IntegerField(default=0)
    #: 判断ステップの出力がパースできなかった件数。0でなければプロンプト／出力契約の不備
    failed = models.IntegerField(default=0)
    tp = models.IntegerField(default=0)
    fn = models.IntegerField(default=0)  # 見逃し。主指標の分子
    fp = models.IntegerField(default=0)  # 誤検出
    tn = models.IntegerField(default=0)

    #: 第2軸：キーワード一致（参考値）と人の最終判定
    axis2_scored = models.IntegerField(default=0)  # 母数 = tp
    axis2_keyword_match = models.IntegerField(default=0)
    axis2_human_reviewed = models.IntegerField(default=0)
    axis2_human_correct = models.IntegerField(default=0)
    axis2_disagreement = models.IntegerField(default=0)

    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    elapsed_sec = models.FloatField(default=0.0)

    aborted = models.BooleanField(default=False)
    abort_reason = models.TextField(blank=True)

    class Meta:
        db_table = "nl2sql_stage2_experiment"
        ordering = ["-id"]

    def __str__(self):
        return f"Nl2SqlStage2Experiment {self.id} (miss_rate={self.miss_rate})"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float | None:
        """概算コスト（USD）。単価表に無いモデルは `None`。

        **列に持たせず、読むたびに導く。** 保存すると単価改定で過去の記録が
        実態と食い違い、しかも「いつの単価で出した値か」が読めなくなる。
        トークン数は事実なので変わらない。単価表は `app/common/pricing.py`。
        """
        return estimate_cost_usd(
            (self.parameters or {}).get("model", ""),
            self.prompt_tokens,
            self.completion_tokens,
        )

    @property
    def cost_display(self) -> str:
        return format_cost_usd(self.cost_usd)

    @property
    def dataset_short(self) -> str:
        return self.dataset_digest[:8]

    @property
    def miss_rate(self) -> float:
        """主指標。見逃し率 = fn / (tp + fn)。分母は曖昧群のうち判定できた件数。"""
        denom = self.tp + self.fn
        return round(self.fn / denom, 4) if denom else 0.0

    @property
    def false_positive_rate(self) -> float:
        """併記のみ。数値上限は置かない。分母は対照群のうち判定できた件数。"""
        denom = self.fp + self.tn
        return round(self.fp / denom, 4) if denom else 0.0

    @property
    def axis2_keyword_match_rate(self) -> float:
        return round(self.axis2_keyword_match / self.axis2_scored, 4) if self.axis2_scored else 0.0


class Nl2SqlStage2RuleExperiment(models.Model):
    """段階2（ルール判定版）の実験記録。

    `Nl2SqlStage2Experiment`（LLM総合判断・`hint`で凍結・`exp_1`〜`exp_10`）とは
    **別の指標体系**（`issue_c013` 論点D）。「聞き返せたか」の混同行列ではなく、
    「未確定な軸を型別に検出できたか」を数える。`exp_1`〜`exp_10` とは同じ軸で
    比較しない。
    """

    name = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    #: {"model": "openai:gpt-4.1-mini", "temperature": 0.0}
    parameters = models.JSONField(default=dict)

    #: 使った評価データの内容ハッシュ（id/questionのみが対象）
    dataset_digest = models.CharField(max_length=64, blank=True)
    dataset_path = models.CharField(max_length=300, blank=True)

    #: 工程①（axis_extractor）の `_PROMPT`（本体）の内容ハッシュ（`issue_c031`）
    prompt_digest = models.CharField(max_length=64, blank=True)
    #: 工程①の `_FLAG_PROMPT`（`FLAG_*`用、別呼び出し）の内容ハッシュ（`issue_c034`）
    flag_prompt_digest = models.CharField(max_length=64, blank=True)

    question_count = models.IntegerField(default=0)

    #: 工程①（構造抽出）が成功した件数／パースできなかった件数
    judged = models.IntegerField(default=0)
    failed = models.IntegerField(default=0)

    #: 型別に「未確定」と判定された件数（実数）
    type1_detected = models.IntegerField(default=0)
    type2_detected = models.IntegerField(default=0)
    type3_detected = models.IntegerField(default=0)
    type4_detected = models.IntegerField(default=0)
    #: 型1が1つでもあり、聞き返しが要ると判定した件数
    needs_clarification_count = models.IntegerField(default=0)

    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    elapsed_sec = models.FloatField(default=0.0)

    aborted = models.BooleanField(default=False)
    abort_reason = models.TextField(blank=True)

    class Meta:
        db_table = "nl2sql_stage2_rule_experiment"
        ordering = ["-id"]

    def __str__(self):
        return f"Nl2SqlStage2RuleExperiment {self.id} (needs_clarification={self.needs_clarification_count}/{self.judged})"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float | None:
        """概算コスト（USD）。単価表に無いモデルは `None`。

        **列に持たせず、読むたびに導く。** 保存すると単価改定で過去の記録が
        実態と食い違い、しかも「いつの単価で出した値か」が読めなくなる。
        トークン数は事実なので変わらない。単価表は `app/common/pricing.py`。
        """
        return estimate_cost_usd(
            (self.parameters or {}).get("model", ""),
            self.prompt_tokens,
            self.completion_tokens,
        )

    @property
    def cost_display(self) -> str:
        return format_cost_usd(self.cost_usd)

    @property
    def dataset_short(self) -> str:
        return self.dataset_digest[:8]
