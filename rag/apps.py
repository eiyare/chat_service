from django.apps import AppConfig


class RagConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rag"
    verbose_name = "Knowledge base"

    def ready(self):
        from . import signals  # noqa: F401
