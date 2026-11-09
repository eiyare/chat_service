"""Provider-agnostic model access. Every LLM and embedding call goes through here.

Models are addressed with LiteLLM model strings ("<provider>/<model>"). Which
models are offered and how each provider is reached comes from Django settings
(environment variables), overridden by the Django admin; see llm/config.py.
Changing provider or model is a configuration change, never a code change.
"""
from .client import LLMError, embed, provider_of, resolve, stream_chat
from .config import Config, aget_config, get_config, invalidate

__all__ = [
    "Config", "LLMError", "aget_config", "embed", "get_config", "invalidate",
    "provider_of", "resolve", "stream_chat",
]
