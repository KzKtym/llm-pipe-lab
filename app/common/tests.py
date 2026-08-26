"""common/llm_client。プロバイダの解決と例外の切り分け。

例外の2分類（中断すべきか、そのクエリだけ諦めればよいか）を間違えると、
評価ランナーが認証エラーのまま数十件ぶん叩き続ける。そこを実際の SDK 例外で試す。
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from app.common import llm_client
from app.common.llm_client import (
    FakeLLMClient,
    LLMFatalError,
    LLMResponse,
    LLMTransientError,
    LLMTruncatedError,
    OpenAIClient,
    available_providers,
    get_client,
    split_model,
)


def _api_status_error(status: int, body=None):
    """実物の openai.APIStatusError を組み立てる。

    例外クラス名ごとに捕まえず基底クラス1つで受ける実装にしているので、
    テスト側も実物を使って status_code の分岐だけを見る。
    """
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(f"status {status}", response=response, body=body)


class SplitModelTests(SimpleTestCase):
    def test_explicit_provider(self):
        self.assertEqual(split_model("openai:gpt-4.1-mini"), ("openai", "gpt-4.1-mini"))

    def test_provider_is_inferred_from_prefix(self):
        self.assertEqual(split_model("gpt-4.1-mini"), ("openai", "gpt-4.1-mini"))

    def test_provider_name_is_case_insensitive(self):
        self.assertEqual(split_model("OpenAI:gpt-4.1-mini"), ("openai", "gpt-4.1-mini"))

    def test_unknown_bare_model_raises(self):
        with self.assertRaises(ValueError):
            split_model("llama-3-70b")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            split_model("")

    def test_missing_model_part_raises(self):
        with self.assertRaises(ValueError):
            split_model("openai:")


class GetClientTests(SimpleTestCase):
    def test_returns_fake_client(self):
        client = get_client("fake:echo")
        self.assertIsInstance(client, FakeLLMClient)
        self.assertEqual(client.model, "echo")

    def test_returns_openai_client_without_requiring_key(self):
        """キーが無くても生成だけはできること（解決は complete まで遅延する）。"""
        client = get_client("openai:gpt-4.1-mini")
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.model, "gpt-4.1-mini")

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            get_client("bedrock:claude")

    def test_openai_and_fake_are_registered(self):
        self.assertIn("openai", available_providers())
        self.assertIn("fake", available_providers())


class FakeClientTests(SimpleTestCase):
    def test_returns_responses_in_order(self):
        client = FakeLLMClient(responses=["one", "two"])
        self.assertEqual(client.complete("a").text, "one")
        self.assertEqual(client.complete("b").text, "two")

    def test_repeats_last_response_when_exhausted(self):
        client = FakeLLMClient(responses=["only"])
        client.complete("a")
        self.assertEqual(client.complete("b").text, "only")

    def test_records_prompts(self):
        client = FakeLLMClient(responses=["x"])
        client.complete("問い合わせ")
        self.assertEqual(client.calls, ["問い合わせ"])


class LLMResponseTests(SimpleTestCase):
    def test_total_tokens(self):
        res = LLMResponse(text="a", model="m", prompt_tokens=10, completion_tokens=5)
        self.assertEqual(res.total_tokens, 15)

    def test_defaults_are_zero(self):
        self.assertEqual(LLMResponse(text="a", model="m").total_tokens, 0)


class OpenAIClientTests(SimpleTestCase):
    def test_missing_api_key_is_fatal(self):
        with patch.object(llm_client, "config", return_value=""):
            with self.assertRaises(LLMFatalError):
                OpenAIClient().complete("hi")

    def test_success_maps_usage_and_text(self):
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="  SELECT 1  "),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3),
        )
        stub = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: completion)
            )
        )

        client = OpenAIClient(model="gpt-4.1-mini", api_key="dummy")
        with patch.object(client, "_get_client", return_value=stub):
            res = client.complete("質問")

        self.assertEqual(res.text, "SELECT 1")
        self.assertEqual(res.model, "gpt-4.1-mini")
        self.assertEqual(res.prompt_tokens, 12)
        self.assertEqual(res.completion_tokens, 3)
        self.assertEqual(res.finish_reason, "stop")

    def test_missing_usage_defaults_to_zero(self):
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok"), finish_reason="stop"
                )
            ],
            usage=None,
        )
        stub = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: completion)
            )
        )
        client = OpenAIClient(api_key="dummy")
        with patch.object(client, "_get_client", return_value=stub):
            res = client.complete("q")
        self.assertEqual(res.total_tokens, 0)

    def _client_raising(self, exc):
        def _create(**kwargs):
            raise exc

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        )

    def test_rate_limit_is_transient_after_retries(self):
        """429 のうちレート上限は、待てば直る。再試行し、尽きたら transient。

        以前は 429 を一括で Fatal にしていたため、レート上限に当たっただけで
        実験全体が止まっていた。
        """
        client = OpenAIClient(api_key="dummy", max_retries=2, sleep=lambda _s: None)
        with patch.object(client, "_get_client", return_value=self._client_raising(_api_status_error(429))):
            with self.assertRaises(LLMTransientError):
                client.complete("q")

    def test_quota_exhausted_is_fatal(self):
        """同じ 429 でも課金枠切れは待っても直らない。即座に中断させる。"""
        exc = _api_status_error(429, body={"error": {"code": "insufficient_quota"}})
        client = OpenAIClient(api_key="dummy", max_retries=2, sleep=lambda _s: None)
        with patch.object(client, "_get_client", return_value=self._client_raising(exc)):
            with self.assertRaises(LLMFatalError):
                client.complete("q")

    def test_unauthorized_is_fatal(self):
        client = OpenAIClient(api_key="dummy")
        with patch.object(client, "_get_client", return_value=self._client_raising(_api_status_error(401))):
            with self.assertRaises(LLMFatalError):
                client.complete("q")

    def test_server_error_is_transient(self):
        client = OpenAIClient(api_key="dummy", max_retries=1, sleep=lambda _s: None)
        with patch.object(client, "_get_client", return_value=self._client_raising(_api_status_error(500))):
            with self.assertRaises(LLMTransientError):
                client.complete("q")

    def test_bad_request_is_transient(self):
        """400 はそのプロンプト固有の問題。次のクエリは通り得るので継続させる。"""
        client = OpenAIClient(api_key="dummy")
        with patch.object(client, "_get_client", return_value=self._client_raising(_api_status_error(400))):
            with self.assertRaises(LLMTransientError):
                client.complete("q")

    def test_connection_error_is_transient(self):
        import httpx
        import openai

        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        exc = openai.APIConnectionError(request=request)

        client = OpenAIClient(api_key="dummy", max_retries=1, sleep=lambda _s: None)
        with patch.object(client, "_get_client", return_value=self._client_raising(exc)):
            with self.assertRaises(LLMTransientError):
                client.complete("q")


class RetryTests(SimpleTestCase):
    """一時的な失敗はこの層が再試行する。

    再試行してなお駄目なものだけを呼び出し側へ渡す。パイプラインの `retry_k`
    （SQLを書き直させる再試行）とは別物。
    """

    def _flaky_client(self, failures: int, completion):
        """最初の `failures` 回だけ 503 を投げ、その後は成功する stub。"""
        state = {"n": 0}

        def _create(**kwargs):
            state["n"] += 1
            if state["n"] <= failures:
                raise _api_status_error(503)
            return completion

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        ), state

    def _completion(self):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )

    def test_recovers_on_second_attempt(self):
        waits = []
        stub, state = self._flaky_client(1, self._completion())
        client = OpenAIClient(api_key="dummy", max_retries=2, sleep=waits.append)
        with patch.object(client, "_get_client", return_value=stub):
            res = client.complete("q")
        self.assertEqual(res.text, "ok")
        self.assertEqual(state["n"], 2)      # 1回失敗して1回やり直した
        self.assertEqual(len(waits), 1)      # 待ちは1回だけ

    def test_gives_up_after_max_retries(self):
        waits = []
        stub, state = self._flaky_client(99, self._completion())
        client = OpenAIClient(api_key="dummy", max_retries=2, sleep=waits.append)
        with patch.object(client, "_get_client", return_value=stub):
            with self.assertRaises(LLMTransientError):
                client.complete("q")
        self.assertEqual(state["n"], 3)      # 初回 + 再試行2回
        self.assertEqual(len(waits), 2)

    def test_no_retry_when_disabled(self):
        stub, state = self._flaky_client(99, self._completion())
        client = OpenAIClient(api_key="dummy", max_retries=0)
        with patch.object(client, "_get_client", return_value=stub):
            with self.assertRaises(LLMTransientError):
                client.complete("q")
        self.assertEqual(state["n"], 1)

    def test_fatal_status_is_not_retried(self):
        """認証エラーで叩き続けない。この区別が無いと全件ぶん無駄に叩く。"""
        stub = self._client_raising_401 = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **k: (_ for _ in ()).throw(_api_status_error(401))))
        )
        waits = []
        client = OpenAIClient(api_key="dummy", max_retries=3, sleep=waits.append)
        with patch.object(client, "_get_client", return_value=stub):
            with self.assertRaises(LLMFatalError):
                client.complete("q")
        self.assertEqual(waits, [])

    def test_retry_after_header_wins(self):
        """サーバが待ち時間を指定してきたら、こちらの計算より優先する。"""
        import httpx
        import openai

        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        response = httpx.Response(429, request=request, headers={"retry-after": "7"})
        exc = openai.APIStatusError("slow down", response=response, body=None)

        waits = []
        client = OpenAIClient(api_key="dummy", max_retries=1, sleep=waits.append)
        stub = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **k: (_ for _ in ()).throw(exc)))
        )
        with patch.object(client, "_get_client", return_value=stub):
            with self.assertRaises(LLMTransientError):
                client.complete("q")
        self.assertEqual(waits, [7.0])


class TruncationTests(SimpleTestCase):
    """`max_tokens` で切れた応答を、そのまま返さない。

    切り詰めを返すと、呼び出し側のパーサが「◯◯行が見つかりません」という
    無関係な失敗として報告し、原因に辿り着けなくなる。
    """

    def test_length_finish_reason_raises(self):
        completion = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="SELECT * FRO"), finish_reason="length")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=300),
        )
        stub = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: completion))
        )
        client = OpenAIClient(api_key="dummy")
        with patch.object(client, "_get_client", return_value=stub):
            with self.assertRaises(LLMTruncatedError) as ctx:
                client.complete("q", max_tokens=300)
        self.assertIn("max_tokens=300", str(ctx.exception))

    def test_truncated_is_still_transient(self):
        """呼び出し側は「その1問の失敗」として従来どおり扱える。"""
        self.assertTrue(issubclass(LLMTruncatedError, LLMTransientError))


class PricingTests(SimpleTestCase):
    """金額は保存せず、トークン数から導く。単価表は改定される。"""

    def test_known_model(self):
        from app.common.pricing import estimate_cost_usd

        # gpt-4.1-mini: 入力 $0.40 / 1M
        self.assertAlmostEqual(estimate_cost_usd("gpt-4.1-mini", 1_000_000, 0), 0.40)

    def test_provider_prefix_is_stripped(self):
        from app.common.pricing import estimate_cost_usd

        self.assertEqual(
            estimate_cost_usd("openai:gpt-4.1-mini", 1000, 500),
            estimate_cost_usd("gpt-4.1-mini", 1000, 500),
        )

    def test_dated_suffix_falls_back_to_base(self):
        from app.common.pricing import estimate_cost_usd

        self.assertEqual(
            estimate_cost_usd("gpt-4.1-mini-2025-04-14", 1000, 500),
            estimate_cost_usd("gpt-4.1-mini", 1000, 500),
        )

    def test_unknown_model_returns_none(self):
        """近いモデルの単価で代用しない。根拠のない数字を出さない。"""
        from app.common.pricing import estimate_cost_usd

        self.assertIsNone(estimate_cost_usd("some-unreleased-model", 100, 100))

    def test_unknown_is_not_displayed_as_zero(self):
        from app.common.pricing import format_cost_usd

        self.assertNotIn("0", format_cost_usd(None))
