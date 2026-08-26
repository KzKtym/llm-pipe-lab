"""NL→SQL 実験のパラメータ。

移植元の RAG 実験ツールの `core/evaluation/params.py` と同じ役割。画面のテキスト欄から来る
生の dict を正規化し、既定値を埋めて型付きの値にする。

**未実装のパラメータを指定されたら例外を投げる。** 黙って無視すると、
記録に残るパラメータと実際に走った処理が食い違い、後から見たときに
「この設定で測ったはず」という前提が崩れる。実験管理ツールとしては
静かにずれるより落ちるほうがよい。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date


class UnsupportedParameter(ValueError):
    """まだ実装していないパラメータを指定された。"""


# 真偽値として扱う語。"1" / "0" は含めない（`retry_k: 0` のような整数と衝突する）
_TRUE_TEXT = {"true", "yes", "on"}
_FALSE_TEXT = {"false", "no", "off"}
# 未指定を表す語。**キーの型を見てから解釈する。**
# 文字列の段階で None にすると、`schema_desc: none` のような
# 「none という値そのもの」を潰してしまう
_NULL_TEXT = {"none", "null", ""}


@dataclass
class Nl2SqlParams:
    """1回の実験の設定。

    各モジュールへは必要な値だけを個別に渡すこと（このオブジェクトを
    そのまま渡さない）。移植元の RAG 実験ツールと同じ約束。
    """

    # --- スキーマ説明 ---
    schema: str = "demo_sales"
    schema_mode: str = "full"          # "full" / "retrieved"（retrieved は未実装）
    schema_k: int | None = None        # retrieved 時に載せるテーブル数
    schema_desc: str = "comment"       # "comment" / "none" / "hint"
    value_hint: bool = False           # カテゴリ値の候補を添える（未実装）

    # --- 例示 ---
    few_shot: int = 0                  # 例示の件数

    # --- 生成 ---
    model: str = "openai:gpt-4.1-mini"
    temperature: float = 0.0
    max_tokens: int = 800
    dialect: str = "PostgreSQL"
    reference_date: str = "2026-08-14"  # 相対日付の基準。評価では必ず固定する

    # --- 再試行 ---
    self_correct: bool = False
    retry_k: int = 1

    # --- 実行 ---
    max_rows: int = 1000
    timeout_ms: int = 10_000

    @property
    def reference_date_value(self) -> date | None:
        if not self.reference_date:
            return None
        return date.fromisoformat(self.reference_date)

    @property
    def include_comments(self) -> bool:
        """スキーマ説明に日本語コメントを載せるか。"""
        return self.schema_desc == "comment"

    @property
    def uses_hints(self) -> bool:
        """コメントの代わりに、短い注意事項をプロンプトへ載せるか。

        `hint` はコメントを外し、データ固有の注意だけを目立つ位置に短く置く。
        長いコメントの中に注意が埋もれている、という観測（issue_081412）への対応。
        """
        return self.schema_desc == "hint"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def normalize(raw: dict) -> dict:
        """画面由来の生 dict を、型の揃った dict にする。

        キーと値のクォートを外し、数値と明確な真偽語だけを変換する。
        `"none"` のような未指定らしき語は**文字列のまま残す**。
        どのキーで未指定を意味するかは型を見ないと決まらないため、
        判断は `from_dict` に任せる。
        """
        result: dict = {}
        for key, value in (raw or {}).items():
            clean_key = str(key).replace('"', "").replace("'", "").strip()
            if isinstance(value, str):
                text = value.replace('"', "").replace("'", "").strip()
                lowered = text.lower()
                if lowered in _TRUE_TEXT:
                    result[clean_key] = True
                elif lowered in _FALSE_TEXT:
                    result[clean_key] = False
                elif text.lstrip("-").isdigit():
                    result[clean_key] = int(text)
                else:
                    try:
                        result[clean_key] = float(text)
                    except ValueError:
                        result[clean_key] = text
            else:
                result[clean_key] = value
        return result

    @classmethod
    def _field_types(cls) -> dict[str, str]:
        # `from __future__ import annotations` のため、注釈は文字列で入っている
        return {name: str(f.type) for name, f in cls.__dataclass_fields__.items()}

    @classmethod
    def from_dict(cls, raw: dict) -> Nl2SqlParams:
        """正規化済み dict から生成する。未知のキーは無視する。

        値の解釈はキーの型を見て決める。

        - `None` を取り得るキー … `"none"` / `""` を未指定として扱う
        - 真偽値のキー … `1` / `0` を真偽値に読み替える

        Raises:
            UnsupportedParameter: 未実装のパラメータを有効にしている場合
        """
        data = cls.normalize(raw)
        types = cls._field_types()

        kwargs = {}
        for key, value in data.items():
            if key not in types:
                continue
            hint = types[key]
            nullable = "None" in hint or "Optional" in hint

            if isinstance(value, str) and value.lower() in _NULL_TEXT:
                # 型が None を許すキーだけ未指定と解釈する。
                # 許さないキーではその文字列自体が値（例: schema_desc: none）
                if nullable:
                    continue
            if value is None:
                continue
            if hint.startswith("bool") and isinstance(value, int) and not isinstance(value, bool):
                value = bool(value)

            kwargs[key] = value

        params = cls(**kwargs)
        params.validate()
        return params

    def validate(self) -> None:
        if self.schema_mode not in ("full", "retrieved"):
            raise ValueError(f"schema_mode が不正です: {self.schema_mode!r}")
        if self.schema_desc not in ("comment", "none", "hint"):
            raise ValueError(f"schema_desc が不正です: {self.schema_desc!r}")
        if self.retry_k < 0:
            raise ValueError("retry_k は0以上で指定してください")
        if self.few_shot < 0:
            raise ValueError("few_shot は0以上で指定してください")

        # 未実装のものは黙って無視せず落とす
        if self.schema_mode == "retrieved":
            raise UnsupportedParameter(
                "schema_mode: retrieved は未実装です。テーブルの選定処理がありません"
            )
        if self.value_hint:
            raise UnsupportedParameter(
                "value_hint は未実装です。値候補の収集処理がありません"
            )

    def summary_line(self) -> str:
        """一覧に貼れる1行表記。移植元の RAG 実験ツールの書式に合わせる。"""
        parts = [
            f"schema_mode: {self.schema_mode}",
            f"schema_desc: {self.schema_desc}",
            f"few_shot: {self.few_shot}",
            f"model: {self.model}",
            f"temperature: {self.temperature}",
            f"self_correct: {str(self.self_correct).lower()}",
        ]
        if self.self_correct:
            parts.append(f"retry_k: {self.retry_k}")
        parts.append(f"max_rows: {self.max_rows}")
        return ", ".join(parts)
