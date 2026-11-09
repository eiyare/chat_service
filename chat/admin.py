from django.contrib import admin

from .models import Conversation, Message


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("role", "content", "model", "response_ms", "prompt_tokens", "completion_tokens", "created_at")
    readonly_fields = ("response_ms", "prompt_tokens", "completion_tokens", "created_at")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("title", "tenant", "model", "updated_at")
    list_filter = ("tenant", "model")
    search_fields = ("title",)
    inlines = [MessageInline]
