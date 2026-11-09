"""Restructure: Provider = connection with standard fields (type, key, secret,
endpoint, region, API version, project); LLMModel = provider + model name.

Converts existing rows: merges "ollama" and "ollama_chat" into one Ollama
provider, maps old LiteLLM kwargs/secret settings onto the standard fields,
moves credentials typed into the endpoint into the encrypted Key, and turns a
model string typed into "litellm provider" into a model row.
"""
import json
import re

import django.db.models.deletion
from django.db import migrations, models

AWS_KEY = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{12,}$")


def forwards(apps, schema_editor):
    from llm import crypto
    from llm.providers import PREFIX_TO_TYPE, TYPES

    Provider = apps.get_model("llm", "Provider")
    LLMModel = apps.get_model("llm", "LLMModel")

    misplaced_models = []  # (provider, model name) typed into litellm_provider
    for p in Provider.objects.all():
        target = (p.litellm_provider or p.name).split("/")[0]
        ptype = PREFIX_TO_TYPE.get(target) or PREFIX_TO_TYPE.get(p.name) or "openai_compatible"
        t = TYPES[ptype]
        p.provider_type = ptype

        if p.endpoint and AWS_KEY.match(p.endpoint.strip()):
            if not p.key_encrypted:
                p.key_encrypted = crypto.encrypt(p.endpoint.strip())
            p.endpoint = ""

        try:
            secrets = json.loads(crypto.decrypt(p.secret_params_encrypted) or "{}")
        except ValueError:
            secrets = {}
        options = dict(p.options or {})
        to_standard = {t.kwarg(f): f for f in t.fields}
        for source in (secrets, options):
            for kwarg in list(source):
                std = to_standard.get(kwarg)
                if not std:
                    continue
                value = str(source.pop(kwarg))
                if std == "key":
                    p.key_encrypted = p.key_encrypted or crypto.encrypt(value)
                elif std == "secret":
                    p.secret_encrypted = crypto.encrypt(value)
                else:
                    setattr(p, std, value)
        p.options = options

        if p.litellm_provider and "/" in p.litellm_provider:
            misplaced_models.append((p.pk, p.litellm_provider.split("/", 1)[1]))
        p.save()

    # One Ollama connection serves both chat ("ollama_chat") and embeddings ("ollama").
    ollamas = list(Provider.objects.filter(provider_type="ollama").order_by("pk"))
    merged_into = {}
    if len(ollamas) > 1:
        keep = next((p for p in ollamas if p.name == "ollama"), ollamas[0])
        for other in ollamas:
            if other.pk == keep.pk:
                continue
            keep.enabled = keep.enabled or other.enabled
            for f in ("endpoint", "key_encrypted"):
                if not getattr(keep, f) and getattr(other, f):
                    setattr(keep, f, getattr(other, f))
            keep.options = {**(other.options or {}), **(keep.options or {})}
            merged_into[other.name] = keep
        keep.save()

    by_name = {p.name: p for p in Provider.objects.all()}
    for m in LLMModel.objects.all():
        prefix, _, model_name = m.model.partition("/")
        provider = merged_into.get(prefix) or by_name.get(prefix)
        if provider is None:
            ptype = PREFIX_TO_TYPE.get(prefix, "openai_compatible")
            same_type = [p for p in by_name.values() if p.provider_type == ptype and p.name not in merged_into]
            provider = same_type[0] if len(same_type) == 1 else None
            if provider is None:
                provider = Provider.objects.create(name=prefix or ptype, provider_type=ptype)
                by_name[provider.name] = provider
        m.provider = provider
        m.name = model_name or m.model
        m.save()

    for provider_pk, model_name in misplaced_models:
        if not LLMModel.objects.filter(provider_id=provider_pk, name=model_name).exists():
            LLMModel.objects.create(provider_id=provider_pk, name=model_name, kind="chat", enabled=True,
                                    is_default=False, sort_order=0, params={})

    for name in merged_into:
        Provider.objects.filter(name=name).delete()


class Migration(migrations.Migration):
    dependencies = [("llm", "0002_provider_secret_params_encrypted")]

    operations = [
        migrations.RenameField("provider", "api_base", "endpoint"),
        migrations.RenameField("provider", "api_key_encrypted", "key_encrypted"),
        migrations.RenameField("provider", "extra_params", "options"),
        migrations.AddField("provider", "provider_type", models.CharField(default="", max_length=32)),
        migrations.AddField("provider", "secret_encrypted", models.TextField(blank=True, editable=False)),
        migrations.AddField("provider", "region", models.CharField(blank=True, max_length=64)),
        migrations.AddField("provider", "api_version", models.CharField(blank=True, max_length=64, verbose_name="API version")),
        migrations.AddField("provider", "project", models.CharField(blank=True, max_length=200)),
        migrations.AddField(
            "llmmodel", "provider",
            models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="models", to="llm.provider"),
        ),
        migrations.AddField("llmmodel", "name", models.CharField(default="", max_length=255)),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField("provider", "litellm_provider"),
        migrations.RemoveField("provider", "secret_params_encrypted"),
        migrations.RemoveField("llmmodel", "model"),
        migrations.AlterField(
            "llmmodel", "provider",
            models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="models", to="llm.provider"),
        ),
        migrations.AddConstraint(
            "llmmodel", models.UniqueConstraint(fields=("provider", "name"), name="llm_unique_model_per_provider"),
        ),
    ]
