from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from llm.models import LLMModel, Provider
from llm.providers import PREFIX_TO_TYPE, TYPES, env_entry


class Command(BaseCommand):
    help = (
        "Copy the environment-based LLM configuration (.env / settings) into the admin tables so it can be "
        "edited in the Django admin. Existing rows are left unchanged. Keys and secrets stay in the "
        "environment unless --include-keys is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("--include-keys", action="store_true",
                            help="Also copy keys/secrets into the database (stored encrypted).")

    @transaction.atomic
    def handle(self, *args, include_keys=False, **options):
        created = []

        def provider_for(model_string):
            prefix = model_string.split("/", 1)[0]
            ptype = PREFIX_TO_TYPE.get(prefix)
            if ptype is None and settings.LLM_PROVIDERS.get(prefix, {}).get("litellm_provider"):
                ptype = "openai_compatible" if prefix == "openai_compatible" else None
            if ptype is None:
                self.stderr.write(f"  skipped {model_string}: unknown provider '{prefix}'")
                return None
            provider = Provider.objects.filter(provider_type=ptype).order_by("pk").first()
            if provider:
                return provider
            t = TYPES[ptype]
            env = env_entry(ptype)
            provider = Provider(name=ptype, provider_type=ptype)
            to_standard = {t.kwarg(f): f for f in t.fields}
            opts = {}
            for kwarg, value in env.items():
                std = to_standard.get(kwarg)
                if std in ("key", "secret"):
                    if include_keys:
                        setattr(provider, std, str(value))
                elif std:
                    setattr(provider, std, str(value))
                else:
                    opts[kwarg] = value
            provider.options = opts
            provider.save()
            created.append(f"provider {provider.name}")
            return provider

        def add_model(model_string, kind, **extra):
            provider = provider_for(model_string)
            if provider is None:
                return
            name = model_string.split("/", 1)[1]
            if LLMModel.objects.filter(provider=provider, name=name).exists():
                return
            LLMModel.objects.create(provider=provider, name=name, kind=kind,
                                    params=settings.LLM_MODEL_PARAMS.get(model_string, {}), **extra)
            created.append(f"{kind} model {provider.name}/{name}")

        if not LLMModel.objects.filter(kind=LLMModel.Kind.CHAT).exists():
            for order, model in enumerate(settings.LLM_CHAT_MODELS):
                add_model(model, LLMModel.Kind.CHAT, sort_order=order,
                          is_default=(model == settings.LLM_DEFAULT_CHAT_MODEL))
        if settings.LLM_EMBED_MODEL and not LLMModel.objects.filter(kind=LLMModel.Kind.EMBEDDING).exists():
            add_model(settings.LLM_EMBED_MODEL, LLMModel.Kind.EMBEDDING,
                      query_prefix=settings.RAG_QUERY_PREFIX, document_prefix=settings.RAG_DOCUMENT_PREFIX)

        if created:
            for item in created:
                self.stdout.write(f"  created {item}")
            self.stdout.write(self.style.SUCCESS(f"Imported {len(created)} item(s)."))
        else:
            self.stdout.write("Nothing to import (already present or nothing configured).")
