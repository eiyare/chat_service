from django.conf import settings
from django.db import models
from django.urls import reverse

from tenants.models import Tenant


class Conversation(models.Model):
    class Source(models.TextChoices):
        APP = "app", "Workspace chat"
        WIDGET = "widget", "Website widget"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="conversations")
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.APP, db_index=True)
    # Anonymous widget visitor (random ID kept in the visitor's browser); blank for the app.
    visitor_id = models.CharField(max_length=64, blank=True, db_index=True)
    # Owner of a workspace-chat conversation (each user sees only their own); null for the widget.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                             related_name="conversations")
    title = models.CharField(max_length=200, default="New chat")
    model = models.CharField(max_length=200, help_text="LiteLLM model string last used in this chat")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["tenant", "-updated_at"])]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse(
            "chat:conversation",
            kwargs={"tenant_slug": self.tenant.slug, "pk": self.pk},
        )


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField(blank=True)
    thinking = models.TextField(blank=True, help_text="Reasoning output from thinking models")
    model = models.CharField(max_length=200, blank=True)
    sources = models.JSONField(
        default=list, blank=True, help_text="Document passages the answer was grounded on"
    )
    # Per-answer metrics (assistant messages only). Token counts are null when the
    # provider doesn't report usage or no model was called.
    response_ms = models.PositiveIntegerField(
        null=True, blank=True, help_text="Server time from receiving the question to the end of the answer"
    )
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True, help_text="Input tokens")
    completion_tokens = models.PositiveIntegerField(null=True, blank=True, help_text="Output tokens")

    class Outcome(models.TextChoices):
        ANSWERED = "answered", "Answered"
        FAQ = "faq", "Answered from FAQ"                    # matched a workspace FAQ; model not called
        SMALL_TALK = "small_talk", "Small talk"             # greeting / who-are-you / thanks; model not called
        DECLINED = "declined", "Declined by model"        # passages found, model found no answer
        NOT_FOUND = "not_found", "Not found in documents"  # search found nothing; model not called
        UNAVAILABLE = "unavailable", "Unavailable"         # no documents / embedding model / needs reindex
        LIMITED = "limited", "Token allowance used up"      # workspace token allowance reached; model not called
        ERROR = "error", "Error"
        STOPPED = "stopped", "Stopped"

    # Retrieval and grounding metrics (assistant messages only).
    outcome = models.CharField(max_length=16, choices=Outcome.choices, blank=True, db_index=True)
    retrieval_ms = models.PositiveIntegerField(null=True, blank=True, help_text="Embedding + search time")
    top_score = models.FloatField(null=True, blank=True, help_text="Similarity of the best passage")
    passage_count = models.PositiveSmallIntegerField(null=True, blank=True)
    search_query = models.CharField(max_length=500, blank=True, help_text="Question as rewritten for search")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"

    @property
    def total_tokens(self):
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    def stats(self):
        return {
            "response_ms": self.response_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    def as_dict(self):
        return {
            "id": self.pk,
            "role": self.role,
            "content": self.content,
            "thinking": self.thinking,
            "model": self.model,
            "sources": self.sources,
            "stats": self.stats() if self.role == self.Role.ASSISTANT else None,
            "outcome": self.outcome,
        }
