"""The Anthropic backend's request shape and provider/model routing (no network)."""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from knowledge.services import llm


def fake_response(text="مرحبا", *, stop_reason="end_turn", model="claude-opus-5-5"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        model=model,
        usage=SimpleNamespace(input_tokens=120, output_tokens=30),
    )


@mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key", "LLM_PROVIDER": "anthropic", "LLM_EFFORT": ""})
class AnthropicBackendTests(SimpleTestCase):
    def setUp(self):
        llm._backends.clear()
        self.addCleanup(llm._backends.clear)
        self.meter = mock.patch("knowledge.services.billing.meter").start()
        self.addCleanup(mock.patch.stopall)

    def backend(self, model=None, response=None):
        backend = llm.get_backend(model)
        backend._client = mock.Mock()
        backend._client.beta.messages.create.return_value = response or fake_response()
        return backend

    def test_defaults_to_current_opus_with_refusal_fallback_and_no_sampling(self):
        backend = self.backend()
        result = backend.complete_with_usage("system", "question", temperature=0)
        kwargs = backend._client.beta.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertNotIn("temperature", kwargs)
        self.assertEqual(kwargs["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(kwargs["extra_body"], {"fallbacks": "default"})
        self.assertEqual(result.text, "مرحبا")  # thinking block skipped
        self.meter.assert_called_once_with("claude-opus-5-5", 120, 30)

    def test_openai_model_names_fall_back_to_the_claude_default(self):
        # e.g. a plan whose llm_model is still "gpt-4o"
        self.assertEqual(self.backend("gpt-4o").model, "claude-opus-5-5")
        self.assertEqual(self.backend("claude-sonnet-5-5").model, "claude-sonnet-5-5")

    def test_haiku_gets_no_fallback_parameter(self):
        backend = self.backend("claude-haiku-5-5")
        backend.complete("s", "u")
        kwargs = backend._client.beta.messages.create.call_args.kwargs
        self.assertNotIn("betas", kwargs)
        self.assertNotIn("extra_body", kwargs)

    def test_effort_is_sent_only_when_configured(self):
        with mock.patch.dict("os.environ", {"LLM_EFFORT": "low"}):
            backend = self.backend()
        backend.complete("s", "u")
        self.assertEqual(backend._client.beta.messages.create.call_args.kwargs["output_config"], {"effort": "low"})

    def test_refusal_raises_llm_error(self):
        backend = self.backend(response=fake_response("", stop_reason="refusal"))
        with self.assertRaises(llm.LLMError):
            backend.complete("s", "u")

    def test_fallback_answer_is_metered_at_the_serving_model(self):
        backend = self.backend(response=fake_response(model="claude-opus-4-8"))
        self.assertEqual(backend.complete_with_usage("s", "u").model, "claude-opus-4-8")


class ProviderModelTests(SimpleTestCase):
    def test_model_names_stay_with_their_provider(self):
        self.assertIsNone(llm._provider_model("anthropic", "gpt-4o-mini"))
        self.assertIsNone(llm._provider_model("openai", "claude-opus-5-5"))
        self.assertEqual(llm._provider_model("openai", "gpt-4o"), "gpt-4o")
        self.assertIsNone(llm._provider_model("anthropic", ""))
