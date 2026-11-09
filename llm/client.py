"""LiteLLM adapter. Which models exist and how each provider is reached comes
from the effective configuration (llm/config.py): Django settings / environment
variables, overridden by what staff configure in the Django admin."""
import logging
import os
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
    """A model call failed (unreachable provider, unknown model, timeout, ...)."""


def provider_of(model: str) -> str:
    """The provider prefix of a LiteLLM model string ("anthropic/x" -> "anthropic")."""
    if "/" in model:
        return model.split("/", 1)[0]
    try:  # bare names (e.g. "gpt-4o-mini") are inferred by LiteLLM
        return litellm.get_llm_provider(model)[1]
    except Exception:
        return ""


def resolve(model: str, config: Config) -> tuple[str, dict]:
    """Map a model ID to (LiteLLM model string, call kwargs).

    Admin models use their provider connection (see llm/config.py). Environment
    models: common settings -> the provider's LLM_PROVIDERS entry -> the
    model's LLM_MODEL_PARAMS; an entry with "litellm_provider" is an alias.
    """
    kwargs = {"timeout": settings.LLM_TIMEOUT}
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


def _error(exc: Exception, model: str) -> LLMError:
    """Turn a LiteLLM/provider exception into a short, user-presentable error."""
    logger.warning("LLM call to %s failed: %s", model, exc)
    if isinstance(exc, litellm.exceptions.Timeout):
        return LLMError("The model provider timed out")
    if isinstance(exc, litellm.exceptions.APIConnectionError):
        return LLMError("Cannot reach the model provider")
    if isinstance(exc, litellm.exceptions.NotFoundError):
        return LLMError(f"Model not found: {model}")
    if isinstance(exc, (litellm.exceptions.AuthenticationError, litellm.exceptions.PermissionDeniedError)):
        return LLMError("The model provider rejected the credentials")
    if isinstance(exc, litellm.exceptions.RateLimitError):
        return LLMError("The model provider is rate limiting requests; try again shortly")
    if isinstance(exc, litellm.exceptions.ContextWindowExceededError):
        return LLMError("The conversation is too long for this model")
    return LLMError("The model provider returned an error")


async def stream_chat(
    model: str, messages: list[dict], temperature: float | None = None, max_tokens: int | None = None
) -> AsyncIterator[dict]:
    """Stream a chat completion.

    Yields events:
      {"type": "thinking", "text": str}   reasoning tokens (models that expose them)
      {"type": "content",  "text": str}   answer tokens
      {"type": "done", "prompt_tokens": int|None, "completion_tokens": int|None,
       "duration_ms": int}
    Token counts are None when the provider doesn't report usage.
    Raises LLMError on failure.
    """
    litellm_model, params = resolve(model, await aget_config())
    if temperature is None:
        temperature = settings.LLM_TEMPERATURE
    if temperature is not None:
        params["temperature"] = temperature
    if max_tokens:
        params["max_tokens"] = max_tokens  # LiteLLM's standard output cap, mapped per provider

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
    except LLMError:
        raise
    except Exception as exc:  # LiteLLM raises many provider-specific types
        raise _error(exc, model) from exc

    # Generation time (excludes model load / queueing) so tokens/sec is meaningful.
    duration_ms = int((time.monotonic() - (first_token or started)) * 1000)
    yield {
        "type": "done",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "duration_ms": duration_ms,
    }


async def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts with the configured embedding model (no prefixes added here)."""
    if not texts:
        return []
    config = await aget_config()
    model = config.embed_model
    if not model:
        raise LLMError("No embedding model configured (set one in the admin or LLM_EMBED_MODEL)")
    litellm_model, params = resolve(model, config)
    try:
        response = await litellm.aembedding(model=litellm_model, input=texts, **params)
    except Exception as exc:
        raise _error(exc, model) from exc
    data = sorted(response.data, key=lambda d: d["index"])
    if len(data) != len(texts):
        raise LLMError(f"Embedding model returned {len(data)} vectors for {len(texts)} inputs")
    return [d["embedding"] for d in data]
