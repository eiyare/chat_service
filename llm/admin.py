"""Django admin for LLM providers (connections) and models.

Admin rows override the .env defaults; see llm/config.py.
"""
import json
import time

from asgiref.sync import async_to_sync
from django import forms
from django.conf import settings
from django.contrib import admin, messages

from . import client
from .config import get_config
from .models import LLMModel, Provider
from .providers import FIELD_LABELS, REQUIRED, TYPES, form_schema

STANDARD_FIELDS = ("key", "secret", "endpoint", "region", "api_version", "project")


class ProviderForm(forms.ModelForm):
    key = forms.CharField(
        label="Key", required=False, strip=True,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )
    clear_key = forms.BooleanField(required=False, label="Remove stored key")
    secret = forms.CharField(
        label="Secret", required=False, strip=True,
        widget=forms.Textarea(attrs={"rows": 2, "autocomplete": "off", "spellcheck": "false"}),
    )
    clear_secret = forms.BooleanField(required=False, label="Remove stored secret")

    class Meta:
        model = Provider
        fields = ["name", "provider_type", "enabled", "key", "clear_key", "secret", "clear_secret",
                  "endpoint", "region", "api_version", "project", "options"]
        widgets = {"options": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["provider_type"].widget.attrs["data-schema"] = json.dumps(form_schema())
        instance = self.instance
        for name in ("key", "secret"):
            stored = instance.pk and instance.has_value(name)
            self.fields[name].help_text = (
                "Stored encrypted; never shown. " + ("A value is stored: leave blank to keep it." if stored
                                                     else "Leave blank to use the environment variable, if any.")
            )
        self.fields["secret"].widget.attrs["placeholder"] = "Never displayed once saved"

    def clean(self):
        cleaned = super().clean()
        ptype = cleaned.get("provider_type")
        if ptype not in TYPES:
            return cleaned
        used = TYPES[ptype].fields
        # Only the fields this provider uses are kept; anything else is cleared.
        for name in ("endpoint", "region", "api_version", "project"):
            if name not in used:
                cleaned[name] = ""
                setattr(self.instance, name, "")
        for name in ("key", "secret"):
            if name not in used or cleaned.get(f"clear_{name}"):
                setattr(self.instance, name, "")
            elif cleaned.get(name):
                setattr(self.instance, name, cleaned[name])
        return cleaned

    class Media:
        js = ("llm/provider_form.js",)


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    form = ProviderForm
    list_display = ("name", "type_label", "enabled", "status", "credentials", "endpoint_display")
    list_editable = ("enabled",)
    list_filter = ("provider_type", "enabled")
    search_fields = ("name", "endpoint")
    fieldsets = (
        (None, {"fields": ("name", "provider_type", "enabled")}),
        ("Connection", {
            "fields": ("key", "clear_key", "secret", "clear_secret", "endpoint", "region", "api_version", "project"),
            "description": "Only the fields the selected provider uses are shown. Required ones are bold. "
                           "Blank fields fall back to the provider's environment variables.",
        }),
        ("Advanced (optional)", {"fields": ("options",), "classes": ("collapse",)}),
    )

    @admin.display(description="Provider", ordering="provider_type")
    def type_label(self, obj):
        return obj.type.label if obj.provider_type in TYPES else obj.provider_type

    @admin.display(description="Status")
    def status(self, obj):
        if not obj.enabled:
            return "Disabled"
        missing = obj.missing_required()
        return f"Missing: {', '.join(missing)}" if missing else "Ready"

    @admin.display(description="Credentials")
    def credentials(self, obj):
        if obj.provider_type not in TYPES:
            return "-"
        parts = []
        for name in ("key", "secret"):
            if name in obj.type.fields:
                state = "set" if obj.has_value(name) else ("environment" if obj.env_has(name) else "-")
                parts.append(f"{FIELD_LABELS[name]}: {state}")
        return "; ".join(parts) or "none needed"

    @admin.display(description="Endpoint")
    def endpoint_display(self, obj):
        if obj.endpoint:
            return obj.endpoint
        if obj.provider_type in TYPES and obj.env_has("endpoint"):
            return "environment"
        return "default" if obj.provider_type in TYPES and "endpoint" in obj.type.fields else "-"

    def changelist_view(self, request, extra_context=None):
        if request.method == "GET":
            for problem in _summary()[1]:
                self.message_user(request, problem, messages.WARNING)
        return super().changelist_view(request, extra_context)


class LLMModelForm(forms.ModelForm):
    class Meta:
        model = LLMModel
        fields = "__all__"
        widgets = {"params": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        examples = "; ".join(f"{t.label}: {t.model_example}" for t in TYPES.values() if t.model_example)
        self.fields["name"].help_text += f" Examples - {examples}."


def _summary() -> tuple[str, list[str]]:
    """(what is in use, problems to fix)."""
    config = get_config()
    chat = ", ".join(config.label(m) for m in config.chat_models) or "none"
    default = config.label(config.default_chat_model) if config.default_chat_model else "none"
    summary = (
        f"In use now - chat models ({config.sources.get('chat_models')}): {chat}; default: {default}; "
        f"embedding model ({config.sources.get('embed_model')}): {config.embed_model or 'none'}. "
        "Admin rows override .env; with no rows of a kind, .env is used."
    )
    problems = []
    for provider in Provider.objects.all():
        hidden = [m.identifier for m in provider.models.filter(enabled=True)]
        if not provider.enabled and hidden:
            problems.append(f"Provider '{provider.name}' is disabled, hiding: {', '.join(hidden)}.")
        elif provider.enabled and provider.missing_required():
            problems.append(
                f"Provider '{provider.name}' ({provider.type.label}) is missing "
                f"{', '.join(provider.missing_required())}"
                + (f", hiding: {', '.join(hidden)}." if hidden else ".")
            )
    if not config.chat_models:
        problems.append("No chat model is available, so the chat cannot answer. Add or enable a chat model.")
    if not config.embed_model:
        problems.append("No embedding model is available, so documents cannot be searched or uploaded.")
    return summary, problems


@admin.register(LLMModel)
class LLMModelAdmin(admin.ModelAdmin):
    form = LLMModelForm
    list_display = ("identifier_display", "label", "provider", "kind", "enabled", "is_default", "sort_order", "status")
    list_editable = ("label", "enabled", "is_default", "sort_order")
    list_filter = ("kind", "enabled", "provider")
    list_select_related = ("provider",)
    search_fields = ("name", "label", "provider__name")
    actions = ["test_models"]
    fieldsets = (
        (None, {"fields": ("provider", "name", "kind", "label", "enabled", "is_default", "sort_order")}),
        ("Embedding models only (optional)", {
            "fields": ("query_prefix", "document_prefix", "min_score", "faq_min_score", "faq_context_min_score"),
            "description": "Similarity scales differ between embedding models: calibrate the cut-offs per model. "
                           "Blank uses the platform defaults.",
        }),
        ("Advanced (optional)", {"fields": ("params",), "classes": ("collapse",)}),
    )

    @admin.display(description="Model ID", ordering="name")
    def identifier_display(self, obj):
        return obj.identifier

    @admin.display(description="Status")
    def status(self, obj):
        if not obj.enabled:
            return "Disabled"
        if not obj.provider.enabled:
            return "Hidden: provider disabled"
        missing = obj.provider.missing_required()
        return f"Hidden: provider missing {', '.join(missing)}" if missing else "Available"

    def changelist_view(self, request, extra_context=None):
        if request.method == "GET":
            summary, problems = _summary()
            self.message_user(request, summary, messages.INFO)
            for problem in problems:
                self.message_user(request, problem, messages.WARNING)
        return super().changelist_view(request, extra_context)

    def save_model(self, request, obj, form, change):
        embedding_changed = obj.kind == LLMModel.Kind.EMBEDDING and (
            not change or {"provider", "name", "enabled", "document_prefix", "kind"} & set(form.changed_data)
        )
        super().save_model(request, obj, form, change)
        if embedding_changed:
            self.message_user(
                request,
                "The embedding configuration changed: existing documents must be reindexed before they can be "
                "searched (Documents > select > 'Reindex selected documents', or manage.py reindex_documents).",
                messages.WARNING,
            )

    @admin.action(description="Test selected models (sends a tiny request)")
    def test_models(self, request, queryset):
        for obj in queryset.select_related("provider"):
            missing = obj.provider.missing_required()
            if missing:
                self.message_user(request, f"Test of {obj.identifier}: provider is missing {', '.join(missing)}",
                                  messages.ERROR)
                continue
            try:
                detail = async_to_sync(_probe)(obj)
            except client.LLMError as exc:
                self.message_user(request, f"Test of {obj.identifier}: failed - {exc}", messages.ERROR)
            else:
                self.message_user(request, f"Test of {obj.identifier}: OK - {detail}", messages.SUCCESS)


async def _probe(obj: LLMModel) -> str:
    """Call the model through its provider connection, even if it is disabled."""
    params = {"timeout": settings.LLM_TIMEOUT, **obj.provider.kwargs(), **(obj.params or {})}
    started = time.monotonic()
    try:
        if obj.kind == LLMModel.Kind.EMBEDDING:
            response = await client.litellm.aembedding(model=obj.litellm_model, input=["test"], **params)
            return f"{len(response.data[0]['embedding'])}-dimension vectors in {time.monotonic() - started:.1f} s"
        response = await client.litellm.acompletion(
            model=obj.litellm_model, messages=[{"role": "user", "content": "Reply with: OK"}],
            **{"max_tokens": 5, **params},
        )
    except Exception as exc:
        raise client._error(exc, obj.identifier) from exc
    text = (response.choices[0].message.content or "").strip()
    return f"replied {text[:40]!r} in {time.monotonic() - started:.1f} s"
