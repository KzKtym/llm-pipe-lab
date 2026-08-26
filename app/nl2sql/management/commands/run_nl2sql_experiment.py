"""段階1（NL→SQL 生成・実行・採点）の実験をコマンドラインから1回走らせる。

`services.run_experiment()` の薄い入口。実験の中身はここには書かない
（依存の向きは `command → services → core / store` の一方向）。

    python manage.py run_nl2sql_experiment --name "baseline" --schema-desc comment

指定できるパラメータは `params.Nl2SqlParams` のフィールドと1対1で対応する。
**未指定のオプションは渡さない。** 既定値は `params.py` 側だけが持ち、
コマンドは既定値を複製しない（二重管理は記録と実挙動のずれの元になる）。

**実行には OPENAI_API_KEY と、それに伴う課金が必要。** 評価データ全件へ
LLM を呼ぶため、1回の実行でトークン相当の料金が発生する。API を叩かずに
パラメータの解決結果だけを確かめたい場合は `--dry-run` を使うこと。

`--reference_date`（相対日付の基準）は**評価では必ず固定する。** 「今月」「先月」
といった質問の正解は基準日で変わるため、日付を固定しないと同じ実験を後から
再現できず、過去の実験との比較も成り立たない。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from app.common.llm_client import split_model

from ... import services
from ...core.evaluator import dataset_digest, load_questions
from ...params import Nl2SqlParams
from ...store import details_path

#: argparse のオプション名 → `Nl2SqlParams` のフィールド名
#: （`--schema-desc` のようにハイフンで受けたものを dest がそのまま担う）
PARAM_FIELDS = (
    "schema",
    "schema_mode",
    "schema_k",
    "schema_desc",
    "value_hint",
    "few_shot",
    "model",
    "temperature",
    "max_tokens",
    "dialect",
    "reference_date",
    "self_correct",
    "retry_k",
    "max_rows",
    "timeout_ms",
)


class Command(BaseCommand):
    help = (
        "段階1（NL→SQL）の実験を1回走らせ、結果を保存する。"
        "実行には OPENAI_API_KEY と課金が必要（評価データ全件にLLMを呼ぶ）。"
        "--dry-run を付けるとLLMを呼ばず、解決後のパラメータだけを表示する。"
    )

    def add_arguments(self, parser):
        # 既定値は params.Nl2SqlParams が持つ。ここでは default=None のままにし、
        # 指定されたものだけを raw dict に載せる
        parser.add_argument(
            "--name",
            default="",
            help="実験名。一覧での目印。省略可",
        )

        group = parser.add_argument_group("実験パラメータ（params.Nl2SqlParams に対応）")
        group.add_argument(
            "--schema",
            help="対象スキーマ名（既定: demo_sales）",
        )
        group.add_argument(
            "--schema-mode",
            choices=("full", "retrieved"),
            help="スキーマ説明の載せ方（既定: full）。retrieved は未実装で、指定すると落ちる",
        )
        group.add_argument(
            "--schema-k",
            type=int,
            help="retrieved 時に載せるテーブル数（retrieved が未実装のため現状は効かない）",
        )
        group.add_argument(
            "--schema-desc",
            choices=("comment", "none", "hint"),
            help=(
                "スキーマ説明の粒度（既定: comment）。"
                "hint は sample/sales/schema_hints.md を読む"
            ),
        )
        group.add_argument(
            "--value-hint",
            action="store_true",
            default=None,
            help="カテゴリ値の候補を添える。**未実装**で、指定すると落ちる",
        )
        group.add_argument(
            "--few-shot",
            type=int,
            help=(
                "プロンプトに載せる例示の件数（既定: 0）。"
                "例示は評価データとは別ファイル（--few-shot-file）から取る"
            ),
        )
        group.add_argument(
            "--model",
            help='モデル指定 "provider:model"（既定: openai:gpt-4.1-mini）',
        )
        group.add_argument(
            "--temperature",
            type=float,
            help="生成の温度（既定: 0.0）",
        )
        group.add_argument(
            "--max-tokens",
            type=int,
            help="生成の上限トークン数（既定: 800）",
        )
        group.add_argument(
            "--dialect",
            help="SQL方言の指定（既定: PostgreSQL）",
        )
        group.add_argument(
            "--reference-date",
            help=(
                "相対日付（今月・先月など）の基準日 YYYY-MM-DD（既定: 2026-08-14）。"
                "**評価では必ず固定すること。**基準日が動くと同じ実験を再現できない"
            ),
        )
        group.add_argument(
            "--self-correct",
            action="store_true",
            default=None,
            help="実行エラー時にエラーメッセージを添えて再生成する（既定: しない）",
        )
        group.add_argument(
            "--retry-k",
            type=int,
            help="self-correct の再生成回数（既定: 1）",
        )
        group.add_argument(
            "--max-rows",
            type=int,
            help="SQL実行時の行数上限（既定: 1000）",
        )
        group.add_argument(
            "--timeout-ms",
            type=int,
            help="SQL実行のタイムアウト（ミリ秒。既定: 10000）",
        )

        runner = parser.add_argument_group("実行の制御（実験パラメータではない）")
        runner.add_argument(
            "--questions",
            help=f"評価データのJSON（既定: {services.DEFAULT_QUESTIONS}）",
        )
        runner.add_argument(
            "--few-shot-file",
            help=f"例示のJSON（既定: {services.DEFAULT_FEW_SHOT}）",
        )
        runner.add_argument(
            "--pause-sec",
            type=float,
            default=0.0,
            help=(
                "1問ごとに待つ秒数（既定: 0）。採点結果には影響しない。"
                "分あたりトークン上限（TPM）に当たる場合に間隔を空ける"
            ),
        )
        runner.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "LLMを呼ばず、解決後のパラメータと入力ファイルだけを表示して終了する。"
                "OPENAI_API_KEY が無くても動く"
            ),
        )

    def handle(self, *args, **options):
        raw_params = {
            field: options[field]
            for field in PARAM_FIELDS
            if options.get(field) is not None
        }
        questions_path = options["questions"] or services.DEFAULT_QUESTIONS
        few_shot_path = options["few_shot_file"] or services.DEFAULT_FEW_SHOT

        if options["dry_run"]:
            self._dry_run(raw_params, questions_path, few_shot_path)
            return

        result = services.run_experiment(
            options["name"],
            raw_params,
            questions_path=questions_path,
            few_shot_path=few_shot_path,
            pause_sec=options["pause_sec"],
        )

        # run_experiment は例外を握り潰して error を返す。
        # 終了コードで失敗が分かるよう、ここで必ず落とす
        if result.get("status") != "success":
            raise CommandError(f"実験に失敗しました: {result.get('error')}")

        experiment_id = result["experiment_id"]
        self.stdout.write(self.style.SUCCESS(f"実験ID: {experiment_id}"))
        self.stdout.write(result["text"])
        self.stdout.write(f"明細: {details_path(experiment_id)}")
        self.stdout.write(f"画面: /nl2sql/{experiment_id}/")

    # --- dry-run ------------------------------------------------------------

    def _dry_run(self, raw_params, questions_path, few_shot_path) -> None:
        """パラメータの正規化と入力ファイルの確認だけを行う。LLMは呼ばない。

        Raises:
            CommandError: パラメータが不正・未実装、または入力ファイルが無い場合
        """
        try:
            params = Nl2SqlParams.from_dict(raw_params)
        except (ValueError, TypeError) as e:
            raise CommandError(f"パラメータが不正です: {e}") from e

        try:
            provider, model = split_model(params.model)
        except ValueError as e:
            raise CommandError(f"モデル指定が不正です: {e}") from e

        self.stdout.write("--dry-run: LLMは呼びません")
        self.stdout.write("")
        self.stdout.write(f"解決後のパラメータ: {params.summary_line()}")
        for key, value in params.to_dict().items():
            self.stdout.write(f"  {key}: {value}")
        self.stdout.write(f"  （プロバイダ: {provider} / モデル: {model}）")
        self.stdout.write("")

        resolved_questions = services._resolve(questions_path)
        if not resolved_questions.exists():
            raise CommandError(f"評価データがありません: {resolved_questions}")
        questions = load_questions(resolved_questions)
        self.stdout.write(f"評価データ: {resolved_questions}")
        self.stdout.write(f"  件数: {len(questions)}")
        self.stdout.write(f"  ダイジェスト: {dataset_digest(questions)}")

        # 例示・注意事項は「指定したのにファイルが無い」ときだけ落ちる。
        # 本番と同じ関数で確かめる
        try:
            examples = services.load_few_shot(params.few_shot, few_shot_path)
            hints = services.load_hints(params)
        except (FileNotFoundError, ValueError) as e:
            raise CommandError(str(e)) from e
        if params.few_shot:
            self.stdout.write(
                f"例示: {services._resolve(few_shot_path)}（{len(examples)}件）"
            )
        if params.uses_hints:
            self.stdout.write(
                f"注意事項: {services._resolve(services.DEFAULT_HINTS)}"
                f"（{len(hints)}文字）"
            )

        self.stdout.write("")
        self.stdout.write(
            "本実行には OPENAI_API_KEY と課金が必要です（評価データ全件にLLMを呼びます）"
        )
