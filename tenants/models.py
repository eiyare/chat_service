from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse


@dataclass(frozen=True)
class UploadPolicy:
    """Effective knowledge-base upload limits for a workspace."""
    max_file_mb: int
    max_documents: int
    extensions: tuple

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


@dataclass(frozen=True)
class TokenPolicy:
    """Effective chat token limits for a workspace. None means unlimited."""
    allowance: int | None
    period: str  # "day" | "week" | "month"
    max_output_tokens: int | None

    def period_bounds(self, now=None):
        """(start, reset) of the current period, in the site's time zone."""
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.localtime(now or timezone.now())
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.period == "week":
            start -= timedelta(days=start.weekday())  # Monday
            reset = start + timedelta(days=7)
        elif self.period == "month":
            start = start.replace(day=1)
            reset = (start + timedelta(days=32)).replace(day=1)
        else:
            reset = start + timedelta(days=1)
        return start, reset


TOKEN_PERIODS = [("day", "Per day"), ("week", "Per week"), ("month", "Per month")]


def _normalise_types(value: str) -> list[str]:
    return [e.strip().lower().lstrip(".") for e in (value or "").split(",") if e.strip()]


class Tenant(models.Model):
    """A customer organisation with its own independent workspace on the platform."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(
        max_length=63,  # fits a DNS label, so slugs can later double as subdomains
        unique=True,
        help_text="URL identifier for the workspace, e.g. 'acme' -> /t/acme/",
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_workspaces",
        help_text="The account that created this workspace through self-service sign-up (counts toward its free limit).",
    )
    chatbot_name = models.CharField(
        max_length=60, default="Tosin",
        help_text="What the assistant calls itself in this workspace's chat and website widget.",
    )

    # Knowledge-base limits; blank = platform default (RAG_MAX_UPLOAD_MB, RAG_MAX_DOCUMENTS,
    # RAG_ALLOWED_EXTENSIONS in settings).
    max_file_mb = models.PositiveIntegerField(
        "Max file size (MB)", null=True, blank=True, help_text="Largest file a workspace user may upload.",
    )
    max_documents = models.PositiveIntegerField(
        null=True, blank=True, help_text="Most documents the workspace may store (including failed ones).",
    )
    allowed_file_types = models.CharField(
        max_length=100, blank=True,
        help_text="Comma-separated extensions, e.g. 'txt,pdf'. Supported: txt, pdf, md, csv, docx.",
    )

    # Chat token limits; blank = platform default (CHAT_TOKEN_ALLOWANCE, CHAT_TOKEN_PERIOD,
    # CHAT_MAX_OUTPUT_TOKENS in settings). 0 means unlimited.
    token_allowance = models.PositiveIntegerField(
        "Token allowance", null=True, blank=True,
        help_text="Total chat tokens (input + output) the workspace may use per period. 0 = unlimited.",
    )
    token_period = models.CharField(
        "Allowance period", max_length=8, choices=TOKEN_PERIODS, blank=True,
        help_text="When the allowance resets (at midnight; weeks start on Monday).",
    )
    max_output_tokens = models.PositiveIntegerField(
        "Max output tokens per answer", null=True, blank=True,
        help_text="Caps the length of each model answer. 0 = model default.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("tenants:home", kwargs={"tenant_slug": self.slug})

    @property
    def upload_policy(self) -> UploadPolicy:
        """This workspace's limits; blank fields use the platform settings (Django admin)."""
        from siteconfig import conf

        d = conf.get()
        types = _normalise_types(self.allowed_file_types) or _normalise_types(d.default_allowed_file_types)
        return UploadPolicy(
            max_file_mb=self.max_file_mb if self.max_file_mb is not None else d.default_max_upload_mb,
            max_documents=self.max_documents if self.max_documents is not None else d.default_max_documents,
            extensions=tuple(dict.fromkeys(types)),
        )

    @property
    def token_policy(self) -> TokenPolicy:
        """This workspace's token limits; blank fields use the platform settings (Django admin)."""
        from siteconfig import conf

        d = conf.get()
        allowance = self.token_allowance if self.token_allowance is not None else d.default_token_allowance
        max_out = self.max_output_tokens if self.max_output_tokens is not None else d.default_max_output_tokens
        return TokenPolicy(
            allowance=allowance or None,
            period=self.token_period or d.default_token_period,
            max_output_tokens=max_out or None,
        )

    def clean(self):
        from rag.extract import SUPPORTED_EXTENSIONS

        self.chatbot_name = (self.chatbot_name or "").strip()
        if not self.chatbot_name:
            raise ValidationError({"chatbot_name": "Give the assistant a name."})  # rag depends on tenants; import lazily

        unsupported = [e for e in _normalise_types(self.allowed_file_types) if e not in SUPPORTED_EXTENSIONS]
        if unsupported:
            raise ValidationError({"allowed_file_types": (
                f"Not supported: {', '.join(unsupported)}. Choose from {', '.join(SUPPORTED_EXTENSIONS)}."
            )})
        if self.max_file_mb == 0:
            raise ValidationError({"max_file_mb": "Must be at least 1 MB (leave blank for the platform default)."})
        self.allowed_file_types = ",".join(dict.fromkeys(_normalise_types(self.allowed_file_types)))


class Membership(models.Model):
    """Gives a user access to a workspace. Superusers can access every workspace."""

    class Role(models.TextChoices):
        MEMBER = "member", "Member (chat)"
        ADMIN = "admin", "Workspace admin (chat, documents, performance, embed)"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships", verbose_name="workspace")
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["tenant", "user"]
        constraints = [models.UniqueConstraint(fields=["user", "tenant"], name="tenants_one_membership_per_workspace")]

    def __str__(self):
        return f"{self.user} in {self.tenant} ({self.get_role_display()})"
