from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Count

from siteconfig.admin import WorkspaceSettingsInline

from .models import Membership, Tenant


class MembershipInline(admin.TabularInline):
    """Who can use a workspace, and whether they can manage it."""
    model = Membership
    extra = 1
    autocomplete_fields = ("user",)
    verbose_name = "member"
    verbose_name_plural = "Members (workspace access)"


class UserMembershipInline(admin.TabularInline):
    model = Membership
    fk_name = "user"
    extra = 1
    verbose_name = "workspace"
    verbose_name_plural = "Workspace access"


User = get_user_model()
admin.site.unregister(User)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [UserMembershipInline]
    list_display = ("username", "email", "first_name", "last_name", "is_active", "is_staff", "workspaces")

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("memberships__tenant")

    @admin.display(description="Workspaces")
    def workspaces(self, obj):
        if obj.is_superuser:
            return "all (superuser)"
        return ", ".join(f"{m.tenant.name} ({m.role})" for m in obj.memberships.all()) or "-"


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    inlines = [MembershipInline, WorkspaceSettingsInline]
    list_display = ("name", "slug", "is_active", "chatbot_name", "member_count", "documents_used", "file_limit", "file_types",
                    "tokens_used", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    fieldsets = (
        (None, {"fields": ("name", "slug", "is_active")}),
        ("Assistant", {"fields": ("chatbot_name",)}),
        ("Knowledge base limits", {
            "fields": ("max_file_mb", "max_documents", "allowed_file_types"),
            "description": "Leave a field blank to use the platform default (set in the environment). "
                           "Existing documents are not affected by new limits.",
        }),
        ("Chat token limits", {
            "fields": ("token_allowance", "token_period", "max_output_tokens"),
            "description": "Leave a field blank to use the platform default. When the allowance is used up, "
                           "questions that need the model get a message saying when it resets; FAQ answers "
                           "still work. Tokens are counted as reported by the model provider.",
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _documents=Count("documents", distinct=True), _members=Count("memberships", distinct=True)
        )

    @admin.display(description="Members", ordering="_members")
    def member_count(self, obj):
        return obj._members

    @admin.display(description="Documents", ordering="_documents")
    def documents_used(self, obj):
        return f"{obj._documents} / {obj.upload_policy.max_documents}"

    @admin.display(description="Max file size")
    def file_limit(self, obj):
        suffix = "" if obj.max_file_mb is not None else " (default)"
        return f"{obj.upload_policy.max_file_mb} MB{suffix}"

    @admin.display(description="File types")
    def file_types(self, obj):
        suffix = "" if obj.allowed_file_types else " (default)"
        return ", ".join(obj.upload_policy.extensions) + suffix

    @admin.display(description="Chat tokens (this period)")
    def tokens_used(self, obj):
        from django.db.models import Sum
        from django.db.models.functions import Coalesce

        from chat.models import Message

        policy = obj.token_policy
        start, _ = policy.period_bounds()
        totals = Message.objects.filter(
            conversation__tenant=obj, role=Message.Role.ASSISTANT, created_at__gte=start
        ).aggregate(p=Coalesce(Sum("prompt_tokens"), 0), c=Coalesce(Sum("completion_tokens"), 0))
        used = totals["p"] + totals["c"]
        limit = f"{policy.allowance:,}" if policy.allowance else "unlimited"
        return f"{used:,} / {limit} per {policy.period}"

    def get_form(self, request, obj=None, **kwargs):
        from django.conf import settings

        form = super().get_form(request, obj, **kwargs)
        from siteconfig import conf

        d = conf.get()
        defaults = {
            "token_allowance": f"{d.default_token_allowance:,}" if d.default_token_allowance else "unlimited",
            "token_period": f"per {d.default_token_period}",
            "max_output_tokens": f"{d.default_max_output_tokens:,}" if d.default_max_output_tokens
            else "model default",
            "max_file_mb": f"{d.default_max_upload_mb} MB",
            "max_documents": str(d.default_max_documents),
            "allowed_file_types": d.default_allowed_file_types.replace(",", ", "),
        }
        for name, value in defaults.items():
            form.base_fields[name].help_text += f" Platform default: {value} (Settings > Platform settings)."
        return form
