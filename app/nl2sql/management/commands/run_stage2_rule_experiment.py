"""段階2（ルール判定版）の実験をコマンドラインから1回走らせる。

`stage2_rule_services.run_stage2_rule_experiment()` の薄い入口。
SQLは生成せず、質問の構造を抽出して「未確定の軸」をルールで判定する。

    python manage.py run_stage2_rule_experiment --name "rule baseline"

段階1と違い、振れるパラメータは**モデルと温度だけ**（判定はルールであり、
LLMを呼ぶのは工程①の構造抽出と工程②の文言化のみ）。判定基準は
`core/population_rules.py` にあり、実験パラメータでは動かさない。

**実行には OPENAI_API_KEY と、それに伴う課金が必要。** 評価データ全件へ
LLM を呼ぶため、1回の実行でトークン相当の料金が発生する。API を叩かずに
設定の解決結果だけを確かめたい場合は `--dry-run` を使うこと。

段階2は相対日付を解決しないため `reference_date` は持たない。段階1側の
`reference_date` と同じく、**評価では基準となる条件を固定すること。**
判定結果の比較は、プロンプト版（`prompt_digest`）と評価データ
（`dataset_digest`）が揃っていて初めて成り立つ。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from app.common.llm_client import split_model

from ... import stage2_rule_services as rule_services
from ...core.stage2_rule_evaluator import dataset_digest, load_questions
from ...stage2_rule_store import details_path


class Command(BaseCommand):
    help = (
        "段階2（ルール判定版）の実験を1回走らせ、結果を保存する。"
        "実行には OPENAI_API_KEY と課金が必要（評価データ全件にLLMを呼ぶ）。"
        "--dry-run を付けるとLLMを呼ばず、解決後の設定だけを表示する。"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            default="",
            help="実験名。一覧での目印。省略可",
        )
        parser.add_argument(
            "--model",
            default=rule_services.DEFAULT_MODEL,
            help=(
                'モデル指定 "provider:model"'
                f"（既定: {rule_services.DEFAULT_MODEL}）"
            ),
        )
        parser.add_argument(
            "--temperature",
            type=float,
            default=rule_services.DEFAULT_TEMPERATURE,
            help=f"構造抽出の温度（既定: {rule_services.DEFAULT_TEMPERATURE}）",
        )
        parser.add_argument(
            "--questions",
            help=f"評価データのJSON（既定: {rule_services.DEFAULT_QUESTIONS}）",
        )
        parser.add_argument(
            "--pause-sec",
            type=float,
            default=0.0,
            help=(
                "1問ごとに待つ秒数（既定: 0）。判定結果には影響しない。"
                "分あたりトークン上限（TPM）に当たる場合に間隔を空ける"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "LLMを呼ばず、解決後の設定と評価データだけを表示して終了する。"
                "OPENAI_API_KEY が無くても動く"
            ),
        )

    def handle(self, *args, **options):
        questions_path = options["questions"] or rule_services.DEFAULT_QUESTIONS

        if options["dry_run"]:
            self._dry_run(options["model"], options["temperature"], questions_path)
            return

        result = rule_services.run_stage2_rule_experiment(
            options["name"],
            model=options["model"],
            temperature=options["temperature"],
            questions_path=questions_path,
            pause_sec=options["pause_sec"],
        )

        # run_stage2_rule_experiment は例外を握り潰して error を返す。
        # 終了コードで失敗が分かるよう、ここで必ず落とす
        if result.get("status") != "success":
            raise CommandError(f"実験に失敗しました: {result.get('error')}")

        experiment_id = result["experiment_id"]
        self.stdout.write(self.style.SUCCESS(f"実験ID: {experiment_id}"))
        self.stdout.write(result["text"])
        self.stdout.write(f"明細: {details_path(experiment_id)}")
        self.stdout.write(f"画面: /nl2sql/stage2/{experiment_id}/")

    # --- dry-run ------------------------------------------------------------

    def _dry_run(self, model: str, temperature: float, questions_path) -> None:
        """設定の解決と評価データの確認だけを行う。LLMは呼ばない。

        Raises:
            CommandError: モデル指定が不正、または評価データが無い場合
        """
        try:
            provider, model_name = split_model(model)
        except ValueError as e:
            raise CommandError(f"モデル指定が不正です: {e}") from e

        self.stdout.write("--dry-run: LLMは呼びません")
        self.stdout.write("")
        self.stdout.write("解決後の設定:")
        self.stdout.write(f"  model: {model}")
        self.stdout.write(f"  temperature: {temperature}")
        self.stdout.write(f"  （プロバイダ: {provider} / モデル: {model_name}）")
        self.stdout.write("")

        resolved_questions = rule_services._resolve(questions_path)
        if not resolved_questions.exists():
            raise CommandError(f"評価データがありません: {resolved_questions}")
        questions = load_questions(resolved_questions)
        self.stdout.write(f"評価データ: {resolved_questions}")
        self.stdout.write(f"  件数: {len(questions)}")
        self.stdout.write(f"  ダイジェスト: {dataset_digest(questions)}")

        self.stdout.write("")
        self.stdout.write(
            "本実行には OPENAI_API_KEY と課金が必要です（評価データ全件にLLMを呼びます）"
        )
