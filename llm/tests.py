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
            (litellm.exceptions.InternalServerError("Overloaded", "anthropic", "openai/chat-a"), "busy right now"),
            (litellm.exceptions.BadRequestError("bad", "openai/chat-a", "openai"), "rejected the request"),
        ]
        for exc, message in cases:
            async def acompletion(**kwargs):
                raise exc

            with self.subTest(exc=type(exc).__name__), mock.patch("litellm.acompletion", acompletion):
                with self.assertRaisesMessage(llm.LLMError, message):
                    await self.collect(retries=0)

    def test_error_kinds(self):
        from .client import _error

        e = litellm.exceptions
        cases = [
            (e.Timeout("slow", "m", "p"), "timeout", True),
            (e.RateLimitError("slow down", "p", "m"), "rate_limited", True),
            (e.ServiceUnavailableError("down", "p", "m"), "unavailable", True),
            (e.InternalServerError("Overloaded", "p", "m"), "unavailable", True),
            (e.ContextWindowExceededError("long", "m", "p"), "too_long", False),
            (e.ContentPolicyViolationError("no", "m", "p"), "content_filter", False),
            (e.AuthenticationError("key", "p", "m"), "auth", False),
            (RuntimeError("Anthropic overloaded_error"), "unavailable", True),
            (RuntimeError("weird"), "other", False),
        ]
        for exc, kind, retryable in cases:
            with self.subTest(kind):
                error = _error(exc, "m")
                self.assertEqual((error.kind, error.retryable), (kind, retryable))
                self.assertIn(type(exc).__name__, error.detail)

    def test_provider_error_shown_only_with_debug(self):
        error = llm.LLMError("Busy", "unavailable", "InternalServerError: Overloaded")
        error.attempts = 3
        self.assertEqual(error.public(), "Busy")
        with self.settings(DEBUG=True):
            self.assertEqual(error.public(), "Busy [unavailable, 3 attempts: InternalServerError: Overloaded]")
        self.assertEqual(error.summary(), "unavailable after 3 attempts: InternalServerError: Overloaded")

    def failing_then(self, failures, chunks):
        """acompletion that raises each of ``failures`` in turn, then streams ``chunks``."""
        calls = []

        async def acompletion(**kwargs):
            calls.append(kwargs)
            if len(calls) <= len(failures):
                raise failures[len(calls) - 1]

            async def gen():
                for c in chunks:
                    yield c
            return gen()
        return acompletion, calls

    async def test_tool_calls_are_assembled_from_streamed_pieces(self):
        def call(index, id=None, name=None, arguments=None):
            return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments))

        pieces = [call(0, "c1", "get_my_grades", ""), call(0, arguments="{}"),
                  call(1, "c2", "search_course", '{"que'), call(1, arguments='ry": "essay"}')]
        chunks = [SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None, reasoning_content=None, tool_calls=[p]))]) for p in pieces]
        acompletion, calls = fake_acompletion(chunks)
        with mock.patch("litellm.acompletion", acompletion):
            events = [e async for e in llm.stream_chat("openai/chat-a", [], tools=[{"type": "function"}])]
        self.assertEqual(calls[0]["tools"], [{"type": "function"}])
        self.assertEqual(events[0], {"type": "tool_calls", "calls": [
            {"id": "c1", "name": "get_my_grades", "arguments": "{}"},
            {"id": "c2", "name": "search_course", "arguments": '{"query": "essay"}'}]})
        self.assertEqual(events[-1]["type"], "done")

    @mock.patch("llm.client.RETRY_DELAY", 0)
    async def test_temporary_failures_are_retried(self):
        busy = litellm.exceptions.InternalServerError("Overloaded", "anthropic", "openai/chat-a")
        acompletion, calls = self.failing_then([busy, busy], [chunk(content="Hi")])
        with mock.patch("litellm.acompletion", acompletion):
            events = await self.collect(retries=2)
        self.assertEqual(len(calls), 3)
        self.assertEqual((events[0]["text"], events[-1]["attempts"]), ("Hi", 3))

        acompletion, calls = self.failing_then([busy] * 3, [chunk(content="Hi")])
        with mock.patch("litellm.acompletion", acompletion):
            with self.assertRaises(llm.LLMError) as caught:
                await self.collect(retries=2)
        self.assertEqual((len(calls), caught.exception.attempts, caught.exception.kind), (3, 3, "unavailable"))

    @mock.patch("llm.client.RETRY_DELAY", 0)
    async def test_permanent_failures_and_cut_off_replies_are_not_retried(self):
        denied = litellm.exceptions.AuthenticationError("bad key", "openai", "openai/chat-a")
        acompletion, calls = self.failing_then([denied], [])
        with mock.patch("litellm.acompletion", acompletion):
            with self.assertRaises(llm.LLMError):
                await self.collect(retries=2)
        self.assertEqual(len(calls), 1)

        calls = []

        async def cut_off(**kwargs):
            calls.append(kwargs)

            async def gen():
                yield chunk(content="Hal")
                raise litellm.exceptions.InternalServerError("Overloaded", "anthropic", "openai/chat-a")
            return gen()

        events = []
        with mock.patch("litellm.acompletion", cut_off):
            with self.assertRaises(llm.LLMError) as caught:
                async for e in llm.stream_chat("openai/chat-a", [], retries=2):
                    events.append(e)
        self.assertEqual((len(calls), events, caught.exception.partial), (1, [{"type": "content", "text": "Hal"}], True))


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


class CloseClientsTests(SimpleTestCase):
    """close_clients() ends a short-lived loop cleanly (management commands)."""

    def test_closes_and_forgets_cached_clients(self):
        from asgiref.sync import async_to_sync

        litellm.in_memory_llm_clients_cache.set_cache("stale-client", object())
        close = mock.AsyncMock()
        with mock.patch("litellm.close_litellm_async_clients", close):
            async_to_sync(llm.close_clients)()
        close.assert_awaited_once()
        self.assertIsNone(litellm.in_memory_llm_clients_cache.get_cache("stale-client"))

    def test_close_failure_is_logged_not_raised(self):
        from asgiref.sync import async_to_sync

        litellm.in_memory_llm_clients_cache.set_cache("stale-client", object())
        close = mock.AsyncMock(side_effect=RuntimeError("boom"))
        with mock.patch("litellm.close_litellm_async_clients", close), \
                self.assertLogs("llm.client", "WARNING") as logs:
            async_to_sync(llm.close_clients)()
        self.assertIn("Closing LiteLLM clients failed", logs.output[0])
        self.assertIsNone(litellm.in_memory_llm_clients_cache.get_cache("stale-client"))
