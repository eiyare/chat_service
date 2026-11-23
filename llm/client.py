"""LiteLLM adapter. Which models exist and how each provider is reached comes
from the effective configuration (llm/config.py): Django settings / environment
variables, overridden by what staff configure in the Django admin."""
import asyncio
import logging
import os
import random
import time
from typing import AsyncIterator

from django.conf import settings

from .config import Config, aget_config

# Use LiteLLM's bundled model metadata instead of fetching it over the network on import.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm  # noqa: E402

litellm.suppress_debug_info = True
litellm.drop_params = True  # silently drop params a provider doesn't support

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """A model call failed (unreachable provider, unknown model, timeout, ...).

    str(error) is the short message for users. ``kind`` names the failure (see KINDS),
    ``detail`` is the provider's own error for logs, admin and DEBUG, ``retryable`` says
    whether trying again may work, and ``attempts`` how many calls were made.
    """

    def __init__(self, message: str, kind: str = "other", detail: str = "", retryable: bool = False):
        super().__init__(message)
        self.kind, self.detail, self.retryable = kind, detail, retryable
        self.attempts = 1
        self.partial = False  # part of the reply had already been streamed

    def public(self) -> str:
        """The message for users; with DEBUG on, followed by the provider's error."""
        message = str(self)
        if settings.DEBUG and self.detail:
            message += f" [{self.kind}, {self.attempts} attempt{'s' if self.attempts != 1 else ''}: {self.detail}]"
        return message

    def summary(self) -> str:
        """One line for the message's error field in the admin."""
        tries = f" after {self.attempts} attempts" if self.attempts > 1 else ""
        cut = " (reply cut off)" if self.partial else ""
        return f"{self.kind}{tries}{cut}: {self.detail or self}"[:500]


# kind -> (message for users, worth retrying)
KINDS = {
    "timeout": ("The model provider timed out. Please try again.", True),
    "unreachable": ("Cannot reach the model provider. Please try again shortly.", True),
    "rate_limited": ("The assistant is busy right now. Please try again in a moment.", True),
    "unavailable": ("The assistant is busy right now. Please try again in a moment.", True),
    "not_found": ("Model not found: {model}", False),
    "auth": ("The model provider rejected the credentials", False),
    "too_long": ("The conversation is too long for this model. Please start a new chat.", False),
    "content_filter": ("The model provider declined to answer this question.", False),
    "bad_request": ("The model provider rejected the request", False),
    "other": ("The model provider returned an error", False),
}
# Seconds before the first retry; doubled for each further one, with some jitter.
RETRY_DELAY = 1.0


def provider_of(model: str) -> str:
    """The provider prefix of a LiteLLM model string ("anthropic/x" -> "anthropic")."""
    if "/" in model:
        return model.split("/", 1)[0]
    try:  # bare names (e.g. "gpt-4o-mini") are inferred by LiteLLM
        return litellm.get_llm_provider(model)[1]
    except Exception:
        return ""


def resolve(model: str, config: Config, timeout: float | None = None) -> tuple[str, dict]:
    """Map a model ID to (LiteLLM model string, call kwargs).

    Admin models use their provider connection (see llm/config.py). Environment
    models: common settings -> the provider's LLM_PROVIDERS entry -> the
    model's LLM_MODEL_PARAMS; an entry with "litellm_provider" is an alias.
    """
    # ``timeout``: the admin setting "Model timeout" (callers pass it); else the code default.
    kwargs = {"timeout": settings.LLM_TIMEOUT if timeout is None else timeout}
    if settings.LLM_NUM_RETRIES:
        kwargs["num_retries"] = settings.LLM_NUM_RETRIES
    if model in config.routes:  # configured in the admin
        litellm_model, route_kwargs = config.routes[model]
        kwargs.update({k: v for k, v in route_kwargs.items() if v not in (None, "")})
        return litellm_model, kwargs

    provider_config = dict(config.providers.get(provider_of(model), {}))
    target = provider_config.pop("litellm_provider", None)
    if target and "/" in model:
        model_for_litellm = f"{target}/{model.split('/', 1)[1]}"
    else:
        model_for_litellm = model
    kwargs.update({k: v for k, v in provider_config.items() if v not in (None, "")})
    kwargs.update(config.model_params.get(model, {}))
    return model_for_litellm, kwargs


def _kind(exc: Exception) -> str:
    e = litellm.exceptions
    if isinstance(exc, (e.Timeout, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, e.APIConnectionError):
        return "unreachable"
    if isinstance(exc, e.NotFoundError):
        return "not_found"
    if isinstance(exc, (e.AuthenticationError, e.PermissionDeniedError)):
        return "auth"
    if isinstance(exc, e.RateLimitError):
        return "rate_limited"
    if isinstance(exc, e.ContextWindowExceededError):  # before BadRequestError: it is one
        return "too_long"
    if isinstance(exc, e.ContentPolicyViolationError):
        return "content_filter"
    if isinstance(exc, (e.InternalServerError, e.ServiceUnavailableError, e.BadGatewayError)):
        return "unavailable"
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status >= 500 or status == 529):  # 529: Anthropic "overloaded"
        return "unavailable"
    if "overloaded" in str(exc).lower():
        return "unavailable"
    if isinstance(exc, (e.BadRequestError, e.UnprocessableEntityError)):
        return "bad_request"
    return "other"


def _error(exc: Exception, model: str) -> LLMError:
    """Turn a LiteLLM/provider exception into a short, user-presentable error."""
    kind = _kind(exc)
    message, retryable = KINDS[kind]
    detail = " ".join(f"{type(exc).__name__}: {exc}".split())[:1000]
    logger.debug("LLM call to %s failed", model, exc_info=exc)
    return LLMError(message.format(model=model), kind, detail, retryable)


async def _pause(attempt: int, error: LLMError, model: str) -> None:
    delay = RETRY_DELAY * 2 ** (attempt - 1) * random.uniform(0.75, 1.25)
    logger.info("LLM call to %s failed (%s), retrying in %.1fs: %s", model, error.kind, delay, error.detail)
    await asyncio.sleep(delay)


def _give_up(error: LLMError, model: str, attempt: int) -> LLMError:
    error.attempts = attempt
    logger.warning("LLM call to %s failed (%s, attempt %d%s): %s", model, error.kind, attempt,
                   ", reply cut off" if error.partial else "", error.detail or error)
    return error


async def stream_chat(
    model: str, messages: list[dict], temperature: float | None = None, max_tokens: int | None = None,
    retries: int | None = None, tools: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Stream a chat completion.

    Yields events:
      {"type": "thinking", "text": str}   reasoning tokens (models that expose them)
      {"type": "content",  "text": str}   answer tokens
      {"type": "tool_calls", "calls": [{"id", "name", "arguments"}]}
                                          with ``tools`` (OpenAI format): the tools the model
                                          wants called, just before "done" ("arguments" is
                                          the raw JSON string)
      {"type": "done", "prompt_tokens": int|None, "completion_tokens": int|None,
       "duration_ms": int, "attempts": int}
    Token counts are None when the provider doesn't report usage.

    A temporary failure (timeout, overloaded, rate limited, ...) before anything has been
    streamed is retried up to ``retries`` times (default LLM_RETRIES; the chat passes the
    admin setting). Once text has been streamed a failure can't be retried: the LLMError
    then has ``partial`` set. Raises LLMError on failure.
    """
    if retries is None:
        from siteconfig import conf

        retries = (await conf.aget()).provider_retries
    retries = max(int(retries), 0)
    attempt = 0
    while True:
        attempt += 1
        streamed = False
        try:
            async for event in _stream_once(model, messages, temperature, max_tokens, tools):
                if event["type"] == "done":
                    event["attempts"] = attempt
                elif event["type"] in ("content", "thinking"):
                    streamed = True
                yield event
            return
        except LLMError as error:
            error.partial = streamed
            if streamed or not error.retryable or attempt > retries:
                raise _give_up(error, model, attempt)
            await _pause(attempt, error, model)


async def _stream_once(model, messages, temperature, max_tokens, tools=None):
    """One streamed completion (see stream_chat)."""
    from siteconfig import conf

    litellm_model, params = resolve(model, await aget_config(), (await conf.aget()).llm_timeout)
    if temperature is None:
        temperature = settings.LLM_TEMPERATURE
    if temperature is not None:
        params["temperature"] = temperature
    if max_tokens:
        params["max_tokens"] = max_tokens  # LiteLLM's standard output cap, mapped per provider
    if tools:
        params["tools"] = tools
    calls: dict[int, dict] = {}  # tool calls, assembled from streamed pieces by index

    started = time.monotonic()
    first_token = None
    prompt_tokens = completion_tokens = None
    try:
        response = await litellm.acompletion(
            model=litellm_model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            **params,
        )
        async for chunk in response:
            usage = getattr(chunk, "usage", None)
            if usage:
                prompt_tokens = getattr(usage, "prompt_tokens", None) or prompt_tokens
                completion_tokens = getattr(usage, "completion_tokens", None) or completion_tokens
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                first_token = first_token or time.monotonic()
                yield {"type": "thinking", "text": reasoning}
            if delta.content:
                first_token = first_token or time.monotonic()
                yield {"type": "content", "text": delta.content}
            for piece in getattr(delta, "tool_calls", None) or []:
                first_token = first_token or time.monotonic()
                index = getattr(piece, "index", None)
                if index is None:  # some providers omit it: a new id starts a new call
                    index = len(calls) if getattr(piece, "id", None) else max(calls, default=0)
                call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                call["id"] = getattr(piece, "id", None) or call["id"]
                function = getattr(piece, "function", None)
                if function is not None:
                    call["name"] = getattr(function, "name", None) or call["name"]
                    call["arguments"] += getattr(function, "arguments", None) or ""
    except LLMError:
        raise
    except Exception as exc:  # LiteLLM raises many provider-specific types
        raise _error(exc, model) from exc

    if calls:
        yield {"type": "tool_calls", "calls": [
            {**c, "id": c["id"] or f"call_{i}"} for i, c in sorted(calls.items()) if c["name"]]}
    # Generation time (excludes model load / queueing) so tokens/sec is meaningful.
    duration_ms = int((time.monotonic() - (first_token or started)) * 1000)
    yield {
        "type": "done",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "duration_ms": duration_ms,
    }


async def embed(texts: list[str], purpose: str = "document", config: Config | None = None) -> list[list[float]]:
    """Embed texts with the configured embedding model (no prefixes added here).

    purpose "query" or "document" adds that model's Query / Document options.
    ``config`` overrides the live configuration (the embedding test uses this to
    try a model without saving anything).
    """
    if not texts:
        return []
    config = config or await aget_config()
    model = config.embed_model
    if not model:
        raise LLMError("No embedding model configured (set one in the admin or LLM_EMBED_MODEL)")
    from siteconfig import conf

    platform = await conf.aget()
    litellm_model, params = resolve(model, config, platform.llm_timeout)
    params = {**params, **(config.embed_query_params if purpose == "query" else config.embed_document_params)}
    retries = platform.provider_retries
    attempt = 0
    while True:
        attempt += 1
        try:
            response = await litellm.aembedding(model=litellm_model, input=texts, **params)
            break
        except Exception as exc:
            error = _error(exc, model)
            if not error.retryable or attempt > retries:
                raise _give_up(error, model, attempt) from exc
            await _pause(attempt, error, model)
    data = sorted(response.data, key=lambda d: d["index"])
    if len(data) != len(texts):
        raise LLMError(f"Embedding model returned {len(data)} vectors for {len(texts)} inputs")
    return [d["embedding"] for d in data]


async def close_clients() -> None:
    """Close LiteLLM's cached HTTP clients and forget them.

    Call this at the end of a short-lived event loop (management commands run
    through ``async_to_sync``). LiteLLM's own exit hook closes the clients in a
    new loop, which fails with "Event loop is closed" for connections opened in
    the finished one. Clearing the cache means a later loop in the same process
    opens fresh clients instead of reusing closed ones.
    """
    try:
        await litellm.close_litellm_async_clients()
    except Exception:  # clean-up must never mask the command's own result
        logger.warning("Closing LiteLLM clients failed", exc_info=True)
    litellm.in_memory_llm_clients_cache.flush_cache()
