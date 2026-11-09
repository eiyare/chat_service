"""System checks (run by `manage.py check`, runserver and migrate) for LLM settings.

Checks the environment-based configuration; when Django runs database checks
(`manage.py check --database default`), admin-configured rows are included.
Admin rows are also validated when saved.
"""
from django.core.checks import Error, Tags, Warning, register

from .client import provider_of
from .config import get_config

# Providers that commonly run locally without credentials or a custom endpoint.
_NO_CREDENTIALS_NEEDED = {"ollama", "ollama_chat"}


@register(Tags.compatibility)
def check_llm_settings(app_configs, databases=None, **kwargs):
    config = get_config(use_db=bool(databases))
    issues = []
    chat_models = list(config.chat_models)
    where = "in the admin or LLM_CHAT_MODELS"

    if not chat_models:
        issues.append(Warning(
            "No chat models configured; the chat page cannot answer questions.",
            hint=f"Add chat models {where}, e.g. ollama_chat/<model>,openai/<model>.",
            id="llm.W001",
        ))
    if config.sources.get("chat_models") == "environment":
        from django.conf import settings
        default = settings.LLM_DEFAULT_CHAT_MODEL
        if chat_models and default and default not in chat_models:
            issues.append(Warning(
                f"LLM_DEFAULT_CHAT_MODEL {default!r} is not in LLM_CHAT_MODELS; {chat_models[0]!r} will be used instead.",
                id="llm.W002",
            ))
    if not config.embed_model:
        issues.append(Warning(
            "No embedding model configured; documents cannot be indexed or searched.",
            hint="Add an embedding model in the admin or set LLM_EMBED_MODEL, e.g. ollama/<model>.",
            id="llm.W003",
        ))

    for model in chat_models + ([config.embed_model] if config.embed_model else []):
        if model in config.routes:
            continue  # configured through an admin provider, which validates its own fields
        provider = provider_of(model)
        if not provider:
            issues.append(Error(
                f"Cannot tell which provider serves model {model!r}.",
                hint='Use a LiteLLM model string with a provider prefix, e.g. "anthropic/<model>".',
                id="llm.E001",
            ))
            continue
        entry = {k: v for k, v in config.providers.get(provider, {}).items() if k != "litellm_provider"}
        if not entry and provider not in _NO_CREDENTIALS_NEEDED:
            issues.append(Warning(
                f"Model {model!r} uses provider {provider!r}, which has no credentials or endpoint configured.",
                hint=f"Add the provider in the admin, or set its variables in .env (see .env.example).",
                id="llm.W004",
            ))
    return issues
