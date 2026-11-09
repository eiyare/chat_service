from django.apps import AppConfig


class LlmConfig(AppConfig):
    name = "llm"
    verbose_name = "LLM models and providers"

    def ready(self):
        from django.core.signals import setting_changed
        from django.db.models.signals import post_delete, post_save

        from . import checks  # noqa: F401  (registers system checks)
        from .config import invalidate
        from .models import LLMModel, Provider

        # Admin edits take effect immediately in this process (others within
        # LLM_CONFIG_CACHE_SECONDS); settings overrides (tests) do too.
        for model in (Provider, LLMModel):
            post_save.connect(invalidate, sender=model, dispatch_uid=f"llm-invalidate-save-{model.__name__}")
            post_delete.connect(invalidate, sender=model, dispatch_uid=f"llm-invalidate-delete-{model.__name__}")
        setting_changed.connect(invalidate, dispatch_uid="llm-invalidate-setting")
