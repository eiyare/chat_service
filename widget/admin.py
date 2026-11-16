from django.contrib import admin, messages
from django.utils.html import format_html

from .models import Widget


@admin.register(Widget)
class WidgetAdmin(admin.ModelAdmin):
    list_display = ("tenant", "enabled", "title", "domain_list", "moodle_enabled", "chunk_storage",
                    "rate_limit_per_minute", "updated_at")
    list_editable = ("enabled",)
    list_filter = ("enabled", "moodle_enabled", "chunk_storage")
    search_fields = ("tenant__name", "allowed_domains")
    actions = ["regenerate_moodle_code"]
    fieldsets = (
        (None, {"fields": ("tenant", "enabled", "title", "greeting", "accent_color", "position", "allowed_domains",
                           "rate_limit_per_minute", "public_key")}),
        ("Moodle", {"fields": ("moodle_enabled", "moodle_shared_code", "chunk_storage")}),
    )

    def get_readonly_fields(self, request, obj=None):
        return ("tenant", "public_key", "moodle_shared_code") if obj else ("public_key", "moodle_shared_code")

    def has_add_permission(self, request):
        return False  # created from the workspace's Admin > Embed tab

    @admin.display(description="Allowed websites")
    def domain_list(self, obj):
        return ", ".join(obj.domains()) or "-"

    @admin.display(description="Shared code")
    def moodle_shared_code(self, obj):
        code = obj.moodle_shared_code if obj else ""
        if not code:
            return "Generated when the Moodle integration is turned on and saved."
        return format_html('<code style="user-select:all">{}</code><br>'
                           '<span class="help">Paste into the plugin\'s settings in Moodle, with the widget key '
                           '({}). Keep it secret.</span>', code, obj.public_key)

    @admin.action(description="Regenerate the Moodle shared code (the plugin must be updated)")
    def regenerate_moodle_code(self, request, queryset):
        for widget in queryset.filter(moodle_enabled=True):
            widget.regenerate_moodle_shared_code()
            widget.save(update_fields=["moodle_shared_code_encrypted", "updated_at"])
        self.message_user(request, "New shared codes generated for workspaces with Moodle turned on.",
                          messages.SUCCESS)
