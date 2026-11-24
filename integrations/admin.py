import json

from django.contrib import admin, messages
from django.utils.html import format_html

from .models import Integration


@admin.register(Integration)
class IntegrationAdmin(admin.ModelAdmin):
    """Apps connected to workspaces. Workspace admins manage their own under Admin > Integrations."""
    list_display = ("name", "tenant", "kind", "enabled", "signed_users", "content", "tools", "share_documents",
                    "last_seen_at")
    list_filter = ("kind", "enabled", "content", "tools")
    search_fields = ("name", "tenant__name", "key", "site_url")
    readonly_fields = ("key", "secret_shown", "manifest_shown", "manifest_updated_at", "last_seen_at", "created_at",
                       "updated_at")
    fields = ("tenant", "kind", "name", "site_url", "enabled", "signed_users", "content", "tools", "share_documents",
              "key", "secret_shown", "manifest_shown", "manifest_updated_at", "last_seen_at", "created_at",
              "updated_at")
    actions = ["regenerate_secret"]

    @admin.display(description="Secret")
    def secret_shown(self, obj):
        if not obj or not obj.pk:
            return "Generated when saved."
        return format_html('<code style="user-select:all">{}</code><br><span class="help">The app sends it to call '
                           'saaschat and signs its users with it. For Moodle it is the plugin\'s "shared code". '
                           'Keep it secret.</span>', obj.secret)

    @admin.display(description="Tools the app offers")
    def manifest_shown(self, obj):
        if not obj or not obj.manifest_tools:
            return "None yet (the app sends them to /i/<key>/manifest/)."
        return format_html("<pre style=\"white-space:pre-wrap\">{}</pre>", json.dumps(obj.manifest, indent=2))

    @admin.action(description="Regenerate the secret (the app must be updated)")
    def regenerate_secret(self, request, queryset):
        for integration in queryset:
            integration.regenerate_secret()
            integration.save(update_fields=["secret_encrypted", "updated_at"])
        self.message_user(request, "New secrets generated. Update them in the connected apps.", messages.SUCCESS)
