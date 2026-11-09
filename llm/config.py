"""The effective LLM configuration: environment defaults (Django settings)
overlaid with what staff configured in the Django admin.

Rules (admin wins):
- Chat models: if the admin has any chat model rows, they are the list
  (enabled ones whose provider is enabled and complete, in sort order);
  otherwise LLM_CHAT_MODELS.
- Embedding model: if the admin has any embedding model rows, the enabled one
  (with its prefixes); otherwise LLM_EMBED_MODEL and RAG_*_PREFIX.
- Admin models are routed through their provider's connection fields, which
  fall back field by field to the provider type's environment variables.

The result is cached per process for LLM_CONFIG_CACHE_SECONDS and invalidated
immediately when admin rows change in this process.
"""
import copy
import threading
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.db import DatabaseError


@dataclass(frozen=True)
class Config:
    chat_models: tuple = ()
    labels: dict = field(default_factory=dict)
    default_chat_model: str | None = None
    embed_model: str = ""
    query_prefix: str = ""
    document_prefix: str = ""
    # Relevance cut-offs for the active embedding model (None = settings default)
    min_score: float | None = None
    faq_min_score: float | None = None
    faq_context_min_score: float | None = None
    # Admin models: model ID -> (LiteLLM model string, LiteLLM kwargs)
    routes: dict = field(default_factory=dict)
    # Environment models: provider prefix -> kwargs, and per-model params
    providers: dict = field(default_factory=dict)
    model_params: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)  # what came from "admin" vs "environment"

    def label(self, model: str) -> str:
        return self.labels.get(model) or model

    # Fallbacks come from the platform settings (Django admin > Settings).
    @property
    def doc_cutoff(self) -> float:
        from siteconfig import conf
        return conf.get().min_score if self.min_score is None else self.min_score

    @property
    def faq_cutoff(self) -> float:
        from siteconfig import conf
        return conf.get().faq_min_score if self.faq_min_score is None else self.faq_min_score

    @property
    def faq_context_cutoff(self) -> float:
        from siteconfig import conf
        return conf.get().faq_context_min_score if self.faq_context_min_score is None else self.faq_context_min_score


def build(db_providers=(), db_models=()) -> Config:
    """Combine settings with admin rows (pure function; no I/O).

    db_models must have their provider loaded (select_related("provider")).
    """
    from .providers import TYPES

    usable = {
        p.pk: p for p in db_providers
        if p.enabled and p.provider_type in TYPES and not p.missing_required()
    }
    routes = {}
    for m in db_models:
        provider = usable.get(m.provider_id)
        if provider is None or provider.type.prefix(m.kind) is None:
            continue
        routes[m.identifier] = (m.litellm_model, {**provider.kwargs(), **(m.params or {})})

    def offered(m):
        return m.enabled and m.identifier in routes

    chat_rows = sorted((m for m in db_models if m.kind == "chat"),
                       key=lambda m: (m.sort_order, m.provider.name, m.name))
    embed_rows = [m for m in db_models if m.kind == "embedding"]
    sources = {}

    if chat_rows:
        chat_models = [m.identifier for m in chat_rows if offered(m)]
        labels = {m.identifier: m.label for m in chat_rows if m.label}
        default = next((m.identifier for m in chat_rows if m.is_default and m.identifier in chat_models), None)
        sources["chat_models"] = "admin"
    else:
        chat_models = list(settings.LLM_CHAT_MODELS)
        labels = {}
        default = settings.LLM_DEFAULT_CHAT_MODEL if settings.LLM_DEFAULT_CHAT_MODEL in chat_models else None
        sources["chat_models"] = "environment"
    if default is None and chat_models:
        default = chat_models[0]

    if embed_rows:
        active = next((m for m in embed_rows if offered(m)), None)
        cutoffs = {f: getattr(active, f, None) for f in ("min_score", "faq_min_score", "faq_context_min_score")}
        embed_model = active.identifier if active else ""
        query_prefix = active.query_prefix if active else ""
        document_prefix = active.document_prefix if active else ""
        sources["embed_model"] = "admin"
    else:
        embed_model = settings.LLM_EMBED_MODEL
        query_prefix, document_prefix = settings.RAG_QUERY_PREFIX, settings.RAG_DOCUMENT_PREFIX
        cutoffs = {}
        sources["embed_model"] = "environment"

    return Config(
        chat_models=tuple(chat_models), labels=labels, default_chat_model=default,
        embed_model=embed_model, query_prefix=query_prefix, document_prefix=document_prefix, **cutoffs,
        routes=routes, providers=copy.deepcopy(settings.LLM_PROVIDERS),
        model_params=copy.deepcopy(settings.LLM_MODEL_PARAMS), sources=sources,
    )


# --- caching -------------------------------------------------------------------

_lock = threading.Lock()
_cached: tuple[float, Config] | None = None


def invalidate(**kwargs):
    global _cached
    with _lock:
        _cached = None


def _fresh() -> Config | None:
    with _lock:
        if _cached and time.monotonic() < _cached[0]:
            return _cached[1]
    return None


def _store(config: Config) -> Config:
    global _cached
    ttl = settings.LLM_CONFIG_CACHE_SECONDS
    if ttl > 0:
        with _lock:
            _cached = (time.monotonic() + ttl, config)
    return config


def get_config(use_db: bool = True) -> Config:
    """Effective configuration (sync contexts)."""
    if not use_db:
        return build()
    cached = _fresh()
    if cached:
        return cached
    from .models import LLMModel, Provider
    try:
        config = build(list(Provider.objects.all()), list(LLMModel.objects.select_related("provider")))
    except DatabaseError:  # e.g. before migrations have run
        return build()
    return _store(config)


async def aget_config() -> Config:
    """Effective configuration (async contexts)."""
    cached = _fresh()
    if cached:
        return cached
    from .models import LLMModel, Provider
    try:
        config = build([p async for p in Provider.objects.all()],
                       [m async for m in LLMModel.objects.select_related("provider")])
    except DatabaseError:
        return build()
    return _store(config)
