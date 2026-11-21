from django.contrib import admin

from .models import Conversation, Message


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("role", "content", "model", "outcome", "error", "response_ms", "prompt_tokens", "completion_tokens",
              "created_at")
    readonly_fields = ("outcome", "error", "response_ms", "prompt_tokens", "completion_tokens", "created_at")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("title", "tenant", "model", "updated_at")
    list_filter = ("tenant", "model")
    search_fields = ("title",)
    inlines = [MessageInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    """Assistant replies: filter by outcome (e.g. Error) to see what failed and why."""
    list_display = ("created_at", "tenant", "conversation", "outcome", "model", "error", "response_ms")
    list_filter = ("outcome", "model", "conversation__tenant", "conversation__source")
    search_fields = ("error", "content", "search_query")
    list_select_related = ("conversation__tenant",)
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in Message._meta.fields]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(role=Message.Role.ASSISTANT)

    @admin.display(ordering="conversation__tenant")
    def tenant(self, obj):
        return obj.conversation.tenant

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
