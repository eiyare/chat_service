"""Tests for the LiteLLM adapter. LiteLLM itself is mocked; no network calls."""
from types import SimpleNamespace
from unittest import mock

import litellm
from django.test import SimpleTestCase, TestCase, override_settings

from saas_chat.testing import TestCase  # signed in by default

import llm

LLM_SETTINGS = dict(
    LLM_CHAT_MODELS=["openai/chat-a", "anthropic/chat-b"],
    LLM_DEFAULT_CHAT_MODEL="anthropic/chat-b",
    LLM_EMBED_MODEL="ollama/embed",
    LLM_TIMEOUT=12.0,
    LLM_NUM_RETRIES=0,
    LLM_TEMPERATURE=None,
    LLM_PROVIDERS={
        "openai": {"api_key": "sk-openai", "custom_option": 1},
        "anthropic": {"api_key": "sk-anthropic", "api_base": ""},
        "ollama": {"api_base": "http://ollama.test:11434", "num_ctx": 4096},
        "gpu_box": {"litellm_provider": "ollama_chat", "api_base": "http://gpu.test:11434"},
    },
    LLM_MODEL_PARAMS={"openai/chat-a": {"custom_option": 2, "max_tokens": 50}},
)


@override_settings(**LLM_SETTINGS)
class ResolveTests(SimpleTestCase):
    def test_provider_prefix(self):
        self.assertEqual(llm.provider_of("anthropic/x"), "anthropic")
        self.assertEqual(llm.provider_of("openrouter/vendor/x"), "openrouter")
        self.assertEqual(llm.provider_of("gpt-4o-mini"), "openai")  # bare names inferred by LiteLLM
        self.assertEqual(llm.provider_of("no-such-thing-xyz"), "")

    def test_each_model_gets_its_providers_settings(self):
        model, kwargs = llm.resolve("anthropic/chat-b", llm.get_config(use_db=False))
        self.assertEqual(model, "anthropic/chat-b")
        self.assertEqual(kwargs, {"timeout": 12.0, "api_key": "sk-anthropic"})  # empty values dropped

        model, kwargs = llm.resolve("ollama/embed", llm.get_config(use_db=False))
        self.assertEqual(kwargs, {"timeout": 12.0, "api_base": "http://ollama.test:11434", "num_ctx": 4096})

    def test_model_params_override_provider(self):
        _, kwargs = llm.resolve("openai/chat-a", llm.get_config(use_db=False))
        self.assertEqual(kwargs["api_key"], "sk-openai")
        self.assertEqual((kwargs["custom_option"], kwargs["max_tokens"]), (2, 50))

    def test_alias_provider_rewrites_prefix(self):
        model, kwargs = llm.resolve("gpu_box/big-model", llm.get_config(use_db=False))
        self.assertEqual(model, "ollama_chat/big-model")
        self.assertEqual(kwargs, {"timeout": 12.0, "api_base": "http://gpu.test:11434"})

    def test_unconfigured_provider_gets_common_settings_only(self):
        self.assertEqual(llm.resolve("groq/x", llm.get_config(use_db=False)), ("groq/x", {"timeout": 12.0}))

    @override_settings(LLM_NUM_RETRIES=3)
    def test_retries(self):
        self.assertEqual(llm.resolve("groq/x", llm.get_config(use_db=False))[1]["num_retries"], 3)


@override_settings(**LLM_SETTINGS)
class ChecksTests(SimpleTestCase):
    def ids(self, **overrides):
        from .checks import check_llm_settings
        with self.settings(**overrides):
            return sorted(issue.id for issue in check_llm_settings(None))

    def test_valid_configuration(self):
        self.assertEqual(self.ids(), [])

    def test_problems_reported(self):
        self.assertEqual(self.ids(LLM_CHAT_MODELS=[]), ["llm.W001"])
        self.assertEqual(self.ids(LLM_DEFAULT_CHAT_MODEL="openai/other"), ["llm.W002"])
        self.assertEqual(self.ids(LLM_EMBED_MODEL=""), ["llm.W003"])
        self.assertEqual(self.ids(LLM_CHAT_MODELS=["no-such-thing-xyz"], LLM_DEFAULT_CHAT_MODEL=""), ["llm.E001"])
        self.assertEqual(self.ids(LLM_CHAT_MODELS=["mistral/x"], LLM_DEFAULT_CHAT_MODEL=""), ["llm.W004"])

    def test_local_ollama_needs_no_credentials(self):
        self.assertEqual(self.ids(LLM_CHAT_MODELS=["ollama_chat/x"], LLM_DEFAULT_CHAT_MODEL="",
                                  LLM_PROVIDERS={}, LLM_EMBED_MODEL="ollama/e"), [])


def chunk(content=None, reasoning=None, usage=None, choices=True):
    """usage: (prompt_tokens, completion_tokens) or None."""
    delta = SimpleNamespace(content=content, reasoning_content=reasoning)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta)] if choices else [],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None,
    )


def fake_acompletion(chunks):
    calls = []

    async def acompletion(**kwargs):
        calls.append(kwargs)

        async def gen():
            for c in chunks:
                yield c

        return gen()

    return acompletion, calls


@override_settings(**LLM_SETTINGS)
class ChatModelTests(SimpleTestCase):
    def test_allowlist_and_default(self):
        config = llm.get_config(use_db=False)
        self.assertEqual(config.chat_models, ("openai/chat-a", "anthropic/chat-b"))
        self.assertEqual(config.default_chat_model, "anthropic/chat-b")

    @override_settings(LLM_DEFAULT_CHAT_MODEL="not-listed")
    def test_default_falls_back_to_first(self):
        self.assertEqual(llm.get_config(use_db=False).default_chat_model, "openai/chat-a")

    @override_settings(LLM_CHAT_MODELS=[], LLM_DEFAULT_CHAT_MODEL="")
    def test_no_models(self):
        self.assertIsNone(llm.get_config(use_db=False).default_chat_model)


@override_settings(**LLM_SETTINGS)
class StreamChatTests(TestCase):
    async def collect(self, **kwargs):
        return [e async for e in llm.stream_chat("openai/chat-a", [{"role": "user", "content": "q"}], **kwargs)]

    async def test_streams_events_and_passes_settings(self):
        acompletion, calls = fake_acompletion([
            chunk(reasoning="hmm"), chunk(content="Hel"), chunk(content="lo"),
            chunk(choices=False, usage=(40, 2)),
        ])
        with mock.patch("litellm.acompletion", acompletion):
            events = await self.collect(temperature=0.2)

        self.assertEqual(events[:3], [
            {"type": "thinking", "text": "hmm"},
            {"type": "content", "text": "Hel"},
            {"type": "content", "text": "lo"},
        ])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["prompt_tokens"], 40)
        self.assertEqual(events[-1]["completion_tokens"], 2)
        self.assertIsInstance(events[-1]["duration_ms"], int)

        kwargs = calls[0]
        self.assertEqual(kwargs["model"], "openai/chat-a")
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["api_key"], "sk-openai")
        self.assertEqual(kwargs["timeout"], 12.0)
        self.assertEqual(kwargs["temperature"], 0.2)
        self.assertEqual(kwargs["custom_option"], 2)  # per-model override

    async def test_usage_missing_is_none(self):
        acompletion, _ = fake_acompletion([chunk(content="x")])
        with mock.patch("litellm.acompletion", acompletion):
            done = (await self.collect())[-1]
        self.assertIsNone(done["prompt_tokens"])
        self.assertIsNone(done["completion_tokens"])

    @override_settings(LLM_TEMPERATURE=0.7)
    async def test_default_temperature_and_alias_routing(self):
        acompletion, calls = fake_acompletion([chunk(content="x")])
        with mock.patch("litellm.acompletion", acompletion):
            [e async for e in llm.stream_chat("gpu_box/big", [])]
        self.assertEqual(calls[0]["temperature"], 0.7)
        self.assertEqual(calls[0]["model"], "ollama_chat/big")
        self.assertEqual(calls[0]["api_base"], "http://gpu.test:11434")
        self.assertNotIn("api_key", calls[0])

    async def test_errors_are_mapped(self):
        cases = [
            (litellm.exceptions.APIConnectionError("boom", "openai", "openai/chat-a"), "Cannot reach"),
            (litellm.exceptions.NotFoundError("nope", "openai/chat-a", "openai"), "Model not found"),
            (litellm.exceptions.Timeout("slow", "openai/chat-a", "openai"), "timed out"),
            (RuntimeError("weird"), "returned an error"),
        ]
        for exc, message in cases:
            async def acompletion(**kwargs):
                raise exc

            with self.subTest(exc=type(exc).__name__), mock.patch("litellm.acompletion", acompletion):
                with self.assertRaisesMessage(llm.LLMError, message):
                    await self.collect()


@override_settings(**LLM_SETTINGS)
class EmbedTests(TestCase):
    async def test_embed_orders_by_index_and_uses_embed_settings(self):
        calls = []

        async def aembedding(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(data=[{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}])

        with mock.patch("litellm.aembedding", aembedding):
            vectors = await llm.embed(["a", "b"])
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(calls[0]["model"], "ollama/embed")
        self.assertEqual(calls[0]["api_base"], "http://ollama.test:11434")
        self.assertEqual(calls[0]["num_ctx"], 4096)
        self.assertNotIn("api_key", calls[0])

    async def test_empty_input(self):
        self.assertEqual(await llm.embed([]), [])

    @override_settings(LLM_EMBED_MODEL="")
    async def test_no_embed_model(self):
        with self.assertRaisesMessage(llm.LLMError, "No embedding model configured"):
            await llm.embed(["a"])

    async def test_count_mismatch(self):
        async def aembedding(**kwargs):
            return SimpleNamespace(data=[{"index": 0, "embedding": [1.0]}])

        with mock.patch("litellm.aembedding", aembedding):
            with self.assertRaisesMessage(llm.LLMError, "1 vectors for 2 inputs"):
                await llm.embed(["a", "b"])
