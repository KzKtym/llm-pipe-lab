"""LLMプロバイダの薄い抽象化層。

パイプラインから見た LLM は「プロンプトを渡すとテキストと使用量が返るもの」でしかない。
どのベンダーのAPIを叩くかは実験パラメータとして振りたいので、呼び出し側がプロバイダを
知らずに済むよう、ここで1枚だけかぶせる。

    client = get_client("openai:gpt-4.1-mini")
    res = client.complete("こんにちは")
    res.text, res.total_tokens, res.latency_ms

モデルは `"provider:model"` 形式で指定する。provider を省略した場合は
`_PREFIX_PROVIDERS` の前方一致で推定する（`"gpt-4.1-mini"` → openai）。

金額換算はここでは行わない。単価は変動するため、この層はトークン数と所要時間だけを
返し、コストの計算は記録層の責務とする（`app/common/pricing.py` と、
実験モデルの `cost_usd` プロパティ）。**保存せず、読むたびに単価表から導く。**
保存すると単価改定のたびに過去の記録が実態と食い違う。

例外は「即座に中断すべきもの」と「そのクエリだけ諦めれば続行できるもの」の2種に分ける。
評価ランナーは数十件のクエリをループで回すため、この区別が無いと
認証エラーで全件ぶん無駄に叩き続けることになる。

一時的な失敗（通信断・5xx・レート制限）は、この層が指数バックオフで再試行する。
**再試行してなお駄目なものだけを呼び出し側へ渡す。** パイプラインの `retry_k`
（SQLを書き直させる再試行）とは別物で、あちらは「LLMの答えが悪かった」場合、
こちらは「LLMに届かなかった」場合を扱う。
"""
from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from decouple import config

_DEFAULT_MAX_TOKENS = 1000
_DEFAULT_TIMEOUT = 60.0

# 呼び出し側が即座に中断すべき HTTP ステータス。
# 認証・権限は、次のクエリを投げても同じ結果になる。
#
# **429 はここに入れない。** 429 には性質の違う2つが混ざっている——
# 課金枠の枯渇（待っても直らない＝Fatal）と、レート上限（待てば直る＝再試行）。
# 一括で Fatal にすると、レート上限に当たっただけで実験全体が止まる。
# 本文の内容で切り分ける（`_is_quota_exhausted`）。
_FATAL_STATUS = {401, 403}

#: 待てば直る見込みのあるステータス。指数バックオフで再試行する
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

_DEFAULT_MAX_RETRIES = 2          # 初回を含めて最大3回叩く
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_CAP_SEC = 30.0


class LLMError(Exception):
    """LLM呼び出しに関する基底例外。"""


class LLMFatalError(LLMError):
    """継続しても同じ結果になる失敗。呼び出し側はループを中断すること。"""


class LLMTransientError(LLMError):
    """そのクエリだけ諦めれば続行できる失敗。"""


class LLMTruncatedError(LLMTransientError):
    """`max_tokens` に達して応答が途中で切れた。

    再試行しても同じ場所で切れるため、この層では再試行しない。
    `LLMTransientError` を継承しているので、呼び出し側は「その1問の失敗」として
    従来どおり扱える。**独立した型にしてあるのは、原因を取り違えないため。**
    切り詰めをそのまま返すと、呼び出し側のパーサが
    「◯◯行が見つかりません」という無関係な失敗として報告してしまう。
    """


@dataclass
class LLMResponse:
    """1回の呼び出しの結果。使用量はプロバイダが返さない場合 0 になる。"""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BaseLLMClient(ABC):
    """プロバイダ実装の共通インタフェース。

    追加のプロバイダは、このクラスを継承して `register_provider` で登録する。
    """

    #: `get_client` が解決した際のモデル名（provider 接頭辞を除いたもの）
    model: str

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """プロンプトを渡して応答テキストを得る。

        Raises:
            LLMFatalError: 認証・権限・レート制限など、継続しても解決しない失敗
            LLMTransientError: 通信断・タイムアウト・単発のリクエスト不備
        """


def _is_quota_exhausted(exc) -> bool:
    """429 のうち「待っても直らない」ほうか。

    課金枠切れは `insufficient_quota` を返す。レート上限（`rate_limit_exceeded`）
    とは扱いを変える必要があるが、**SDK のバージョンで所在が変わる**ため、
    code / type / 本文のいずれからも拾えるようにしてある。
    """
    parts = [str(exc), str(getattr(exc, "code", "") or "")]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            parts += [str(err.get("code") or ""), str(err.get("type") or "")]
    text = " ".join(parts).lower()
    return "insufficient_quota" in text or "billing" in text


def _retry_after_seconds(exc) -> float | None:
    """`Retry-After` ヘッダがあれば秒数で返す。無ければ None。

    サーバが待ち時間を指定してきたなら、こちらの計算より優先する。
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None  # HTTP-date 形式。解釈せず自前の待ちに任せる


class OpenAIClient(BaseLLMClient):
    """OpenAI Chat Completions を叩く実装。

    SDK の import はコンストラクタまで遅延させる。このモジュール自体は
    SDK 未インストールでも import できるようにしておきたい（登録機構と
    FakeLLMClient だけを使うテストのため）。
    """

    def __init__(
        self,
        model: str = "gpt-4.1-mini",
        *,
        api_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep          # テストが待たずに済むよう差し替え可能にする
        self._api_key = api_key
        self._client = None

    def _backoff_seconds(self, attempt: int, exc) -> float:
        """`attempt` 回目の失敗のあと、何秒待つか。

        サーバ指定（`Retry-After`）を最優先。無ければ指数バックオフに
        ゆらぎを足す。**ゆらぎが無いと、並行して走らせたときに
        同じ時刻へ再送が揃う。**
        """
        server = _retry_after_seconds(exc)
        if server is not None:
            return min(server, _BACKOFF_CAP_SEC)
        wait = min(_BACKOFF_BASE_SEC * (2 ** attempt), _BACKOFF_CAP_SEC)
        return wait * (0.5 + random.random() / 2)

    def _get_client(self):
        """OpenAI クライアントを遅延生成する。

        キーの解決もここで行う。生成時点で必須にすると、キーが無い環境で
        `get_client()` を呼ぶだけでも落ちてしまう。
        """
        if self._client is not None:
            return self._client

        api_key = self._api_key or config("OPENAI_API_KEY", default="")
        if not api_key:
            raise LLMFatalError("OPENAI_API_KEY が .env に設定されていません")

        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, timeout=self.timeout)
        return self._client

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> LLMResponse:
        import openai

        client = self._get_client()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        start = time.time()
        resp = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                break
            except openai.APIStatusError as e:
                # ステータスコードで分岐する。例外クラス名ごとに捕まえると
                # SDK のバージョン差で漏れるため、基底クラス1つで受ける。
                status = getattr(e, "status_code", None)
                message = f"OpenAI API error {status}: {e}"
                if status in _FATAL_STATUS:
                    raise LLMFatalError(message) from e
                if status == 429 and _is_quota_exhausted(e):
                    # 課金枠切れ。待っても直らないので即座に中断させる
                    raise LLMFatalError(f"{message}（課金枠の枯渇）") from e
                if status in _RETRYABLE_STATUS and attempt < self.max_retries:
                    self._sleep(self._backoff_seconds(attempt, e))
                    continue
                raise LLMTransientError(message) from e
            except openai.APIConnectionError as e:
                # タイムアウトもこの系統に含まれる
                if attempt < self.max_retries:
                    self._sleep(self._backoff_seconds(attempt, e))
                    continue
                raise LLMTransientError(f"OpenAI API connection error: {e}") from e

        latency_ms = int((time.time() - start) * 1000)

        choice = resp.choices[0] if resp.choices else None
        text = ""
        finish_reason = ""
        if choice is not None:
            text = (choice.message.content or "").strip()
            finish_reason = choice.finish_reason or ""

        if finish_reason == "length":
            # 切り詰めたまま返すと、呼び出し側のパーサが「◯◯行が見つかりません」
            # という別の失敗として報告し、原因に辿り着けなくなる。
            raise LLMTruncatedError(
                f"応答が max_tokens={max_tokens} で切り詰められました"
                f"（model={self.model}, 受信 {len(text)} 文字）"
            )

        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=text,
            model=self.model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )


@dataclass
class FakeLLMClient(BaseLLMClient):
    """テストとオフライン検証のためのスタブ。

    `responses` に積んだ文字列を順に返し、尽きたら最後の要素を返し続ける。
    評価ランナーを API を叩かずに通せるようにするためのもので、
    本番経路では使わない。
    """

    model: str = "fake"
    responses: list[str] = field(default_factory=lambda: [""])
    calls: list[str] = field(default_factory=list)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append(prompt)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        text = self.responses[index] if self.responses else ""
        return LLMResponse(text=text, model=self.model, latency_ms=0)


# --- プロバイダの登録と解決 -------------------------------------------------

_PROVIDERS: dict[str, Callable[..., BaseLLMClient]] = {}

# provider を省略されたときの推定用。前方一致で先に当たったものを採用する。
_PREFIX_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("fake", "fake"),
)


def register_provider(name: str, factory: Callable[..., BaseLLMClient]) -> None:
    """プロバイダを登録する。`factory(model=..., **kwargs)` で呼ばれる。"""
    _PROVIDERS[name] = factory


register_provider("openai", OpenAIClient)
register_provider("fake", FakeLLMClient)


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def split_model(spec: str) -> tuple[str, str]:
    """`"provider:model"` を分解する。provider 省略時は前方一致で推定する。

    Raises:
        ValueError: 空文字、または推定できない場合
    """
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("モデル指定が空です")

    if ":" in spec:
        provider, _, model = spec.partition(":")
        provider = provider.strip().lower()
        model = model.strip()
        if not provider or not model:
            raise ValueError(f"モデル指定の形式が不正です: {spec!r}")
        return provider, model

    lowered = spec.lower()
    for prefix, provider in _PREFIX_PROVIDERS:
        if lowered.startswith(prefix):
            return provider, spec

    raise ValueError(
        f"プロバイダを推定できません: {spec!r}. "
        f'"provider:model" 形式で指定してください（利用可能: {", ".join(available_providers())}）'
    )


def get_client(spec: str, **kwargs) -> BaseLLMClient:
    """モデル指定からクライアントを生成する。

    Args:
        spec: `"openai:gpt-4.1-mini"` または `"gpt-4.1-mini"`
        **kwargs: プロバイダのコンストラクタへそのまま渡す（api_key, timeout など）

    Raises:
        ValueError: 未知のプロバイダ、または形式不正
    """
    provider, model = split_model(spec)
    factory = _PROVIDERS.get(provider)
    if factory is None:
        raise ValueError(
            f"未知のプロバイダです: {provider!r}（利用可能: {', '.join(available_providers())}）"
        )
    return factory(model=model, **kwargs)
