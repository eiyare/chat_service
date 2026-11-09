from django.contrib import admin

from .models import Widget


@admin.register(Widget)
class WidgetAdmin(admin.ModelAdmin):
    list_display = ("tenant", "enabled", "title", "domain_list", "rate_limit_per_minute", "updated_at")
    list_editable = ("enabled",)
    list_filter = ("enabled",)
    search_fields = ("tenant__name", "allowed_domains")
    fields = ("tenant", "enabled", "title", "greeting", "accent_color", "position", "allowed_domains",
              "rate_limit_per_minute", "public_key")
    readonly_fields = ("tenant", "public_key")

    def has_add_permission(self, request):
        return False  # created from the workspace's Admin > Embed tab

    @admin.display(description="Allowed websites")
    def domain_list(self, obj):
        return ", ".join(obj.domains()) or "-"
