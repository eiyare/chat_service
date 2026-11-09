from django.apps import AppConfig


class SiteconfigConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "siteconfig"
    verbose_name = "Settings"

    def ready(self):
        from django.core.signals import setting_changed
        from django.db.models.signals import post_delete, post_save

        from .conf import invalidate
        from .models import PlatformSettings, WorkspaceSettings

        for model in (PlatformSettings, WorkspaceSettings):
            post_save.connect(invalidate, sender=model, dispatch_uid=f"siteconfig-save-{model.__name__}")
            post_delete.connect(invalidate, sender=model, dispatch_uid=f"siteconfig-delete-{model.__name__}")
        setting_changed.connect(invalidate, dispatch_uid="siteconfig-setting")
