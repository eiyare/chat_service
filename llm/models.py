"""Admin-editable providers (connections) and models.

A Provider is one connection to a model service: a provider type plus the
standard connection fields that type needs (key, secret, endpoint, region,
API version, project). A Model picks a Provider and names the model there.
Admin rows override the environment defaults; see llm/config.py.
"""
import re

from django.core.exceptions import ValidationError
from django.db import models

from . import crypto
from .providers import CHOICES, FIELD_LABELS, REQUIRED, TYPES, env_entry

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_LOOKS_LIKE_CREDENTIAL = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{12,}$|^sk-")


def _json_object(value):
    if not isinstance(value, dict):
        raise ValidationError("Must be a JSON object, e.g. {\"num_ctx\": 8192}.")


class Provider(models.Model):
    name = models.CharField(
        max_length=64, unique=True,
        help_text="Short name for this connection, used in model IDs, e.g. 'ollama', 'bedrock', 'openai-prod'.",
    )
    provider_type = models.CharField("Provider", max_length=32, choices=CHOICES)
    enabled = models.BooleanField(default=True, help_text="Disabled providers' models are hidden and refused.")

    key_encrypted = models.TextField(blank=True, editable=False)
    secret_encrypted = models.TextField(blank=True, editable=False)
    endpoint = models.CharField(max_length=500, blank=True)
    region = models.CharField(max_length=64, blank=True)
    api_version = models.CharField("API version", max_length=64, blank=True)
    project = models.CharField(max_length=200, blank=True)
    options = models.JSONField(
        default=dict, blank=True, validators=[_json_object],
        help_text="Advanced, optional: extra LiteLLM arguments for all of this provider's models, "
                  "e.g. {\"num_ctx\": 8192, \"keep_alive\": \"5m\"} for Ollama. Not for credentials.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "LLM provider"

    def __str__(self):
        return f"{self.name} ({self.type.label})" if self.provider_type in TYPES else self.name

    @property
    def type(self):
        return TYPES[self.provider_type]

    # --- encrypted credentials ---------------------------------------------------

    @property
    def key(self) -> str:
        return crypto.decrypt(self.key_encrypted)

    @key.setter
    def key(self, value: str):
        self.key_encrypted = crypto.encrypt(value)

    @property
    def secret(self) -> str:
        return crypto.decrypt(self.secret_encrypted)

    @secret.setter
    def secret(self, value: str):
        self.secret_encrypted = crypto.encrypt(value)

    # --- standard fields -----------------------------------------------------------

    def value(self, std_field: str) -> str:
        return {"key": self.key, "secret": self.secret}.get(std_field) or getattr(self, std_field, "")

    def has_value(self, std_field: str) -> bool:
        if std_field == "key":
            return bool(self.key_encrypted)
        if std_field == "secret":
            return bool(self.secret_encrypted)
        return bool(getattr(self, std_field, ""))

    def env_has(self, std_field: str) -> bool:
        return bool(env_entry(self.provider_type).get(self.type.kwarg(std_field)))

    def missing_required(self) -> list[str]:
        """Required fields with no value here and no environment fallback."""
        if self.provider_type not in TYPES:
            return []
        return [
            FIELD_LABELS[f] for f, need in self.type.fields.items()
            if need == REQUIRED and not self.has_value(f) and not self.env_has(f)
        ]

    def kwargs(self) -> dict:
        """LiteLLM keyword arguments: environment defaults, then this connection's fields and options."""
        t = self.type
        result = dict(env_entry(self.provider_type))
        for std_field in t.fields:
            value = self.value(std_field)
            if value:
                result[t.kwarg(std_field)] = value
        result.update(self.options or {})
        return result

    def clean(self):
        errors = {}
        if not _SLUG.match(self.name or "") or "/" in (self.name or ""):
            errors["name"] = "Use lowercase letters, digits, '-', '_' or '.' (no '/')."
        if self.provider_type not in TYPES:
            errors["provider_type"] = "Choose a provider."
        else:
            unused = [FIELD_LABELS[f] for f in ("endpoint", "region", "api_version", "project")
                      if getattr(self, f) and f not in self.type.fields]
            if unused:
                errors["provider_type"] = f"{self.type.label} doesn't use: {', '.join(unused)}. Clear them."
            missing = self.missing_required()
            if missing:
                errors["__all__"] = f"{self.type.label} requires: {', '.join(missing)}."
        if self.endpoint:
            if _LOOKS_LIKE_CREDENTIAL.match(self.endpoint.strip()):
                errors["endpoint"] = "This looks like a credential, not a URL. Put it in Key or Secret."
            elif not re.match(r"^https?://", self.endpoint):
                errors["endpoint"] = "Enter a URL starting with http:// or https://."
        for name in ("region", "api_version", "project"):
            if _LOOKS_LIKE_CREDENTIAL.match(getattr(self, name).strip()):
                errors[name] = "This looks like a credential. Put it in Key or Secret."
        secretish = [k for k in (self.options or {}) if re.search(r"key|secret|token|password|credential", k, re.I)]
        if secretish:
            errors["options"] = f"{', '.join(secretish)} look like credentials: use Key / Secret, which are encrypted."
        if errors:
            raise ValidationError(errors)


class LLMModel(models.Model):
    class Kind(models.TextChoices):
        CHAT = "chat", "Chat"
        EMBEDDING = "embedding", "Embedding"

    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name="models")
    name = models.CharField(
        "Model", max_length=255,
        help_text="The model's name at the provider, e.g. 'qwen2.5:3b', 'gpt-4o-mini', 'google.gemma-3-4b-it'.",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.CHAT)
    label = models.CharField(max_length=100, blank=True, help_text="Name shown in the chat's model picker (blank shows the model ID).")
    enabled = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False, help_text="Preselected chat model (one at most).")
    sort_order = models.PositiveIntegerField(default=0, help_text="Position in the model picker.")
    params = models.JSONField(
        default=dict, blank=True, validators=[_json_object],
        help_text="Advanced, optional: per-model LiteLLM arguments, e.g. {\"max_tokens\": 1024}.",
    )
    query_prefix = models.CharField(
        max_length=100, blank=True,
        help_text="Embedding models only, optional: prefix added to search queries if the model expects one "
                  "(e.g. 'search_query: ' for nomic-embed-text).",
    )
    document_prefix = models.CharField(
        max_length=100, blank=True,
        help_text="Embedding models only, optional: prefix added to document chunks (e.g. 'search_document: '). "
                  "Changing it requires reindexing documents.",
    )
    # Relevance cut-offs: similarity scales differ between embedding models, so they
    # are calibrated per model. Blank uses RAG_MIN_SCORE / RAG_FAQ_MIN_SCORE /
    # RAG_FAQ_CONTEXT_MIN_SCORE from settings.
    min_score = models.FloatField(
        "Document relevance cut-off", null=True, blank=True,
        help_text="Embedding models only: minimum similarity for a document passage to count as relevant.",
    )
    faq_min_score = models.FloatField(
        "FAQ match cut-off", null=True, blank=True,
        help_text="Embedding models only: minimum question similarity for a direct FAQ answer "
                  "(the wording must also match).",
    )
    faq_context_min_score = models.FloatField(
        "FAQ context cut-off", null=True, blank=True,
        help_text="Embedding models only: minimum similarity for a close FAQ to be given to the model as context.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "sort_order", "provider__name", "name"]
        verbose_name = "LLM model"
        constraints = [
            models.UniqueConstraint(fields=["provider", "name"], name="llm_unique_model_per_provider"),
            models.UniqueConstraint(
                fields=["kind"], condition=models.Q(kind="embedding", enabled=True),
                name="llm_one_enabled_embedding_model",
                violation_error_message="Only one embedding model can be enabled at a time.",
            ),
            models.UniqueConstraint(
                fields=["kind"], condition=models.Q(kind="chat", is_default=True),
                name="llm_one_default_chat_model",
                violation_error_message="Only one chat model can be the default.",
            ),
        ]

    def __str__(self):
        return self.label or self.identifier

    @property
    def identifier(self) -> str:
        """Stable ID used by the app (picker, conversations, document signatures)."""
        return f"{self.provider.name}/{self.name}"

    @property
    def litellm_model(self) -> str:
        return f"{self.provider.type.prefix(self.kind)}/{self.name}"

    def clean(self):
        errors = {}
        if not self.name or self.name != self.name.strip():
            errors["name"] = "Enter the model name without surrounding spaces."
        if self.provider_id and self.provider.provider_type in TYPES:
            if self.provider.type.prefix(self.kind) is None:
                errors["kind"] = f"{self.provider.type.label} has no {self.get_kind_display().lower()} models here."
        if self.kind == self.Kind.EMBEDDING and self.is_default:
            errors["is_default"] = "Only chat models can be the default."
        if self.kind == self.Kind.CHAT and (self.query_prefix or self.document_prefix):
            errors["query_prefix"] = "Prefixes apply to embedding models only."
        for field in ("min_score", "faq_min_score", "faq_context_min_score"):
            value = getattr(self, field)
            if value is not None and self.kind == self.Kind.CHAT:
                errors[field] = "Cut-offs apply to embedding models only."
            elif value is not None and not -1 <= value <= 1:
                errors[field] = "Use a similarity between -1 and 1."
        if errors:
            raise ValidationError(errors)
