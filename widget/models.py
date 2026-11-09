"""Per-workspace settings for the embeddable website chat widget."""
import re
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from tenants.models import Tenant

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# A CSP host-source: optional scheme, optional "*." wildcard, host, optional port.
_DOMAIN = re.compile(r"^(https?://)?(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d{1,5})?$")


def new_key() -> str:
    return secrets.token_urlsafe(18)


def normalise_domain(entry: str) -> str:
    entry = entry.strip().lower().rstrip("/")
    m = re.match(r"^(https?://[^/]+|[^/]+)", entry)  # drop any path
    return m.group(1) if m else entry


def _hex_color(value):
    if not _HEX.match(value or ""):
        raise ValidationError("Use a hex colour like #2a78d6.")


def _luminance(hex_color: str) -> float:
    def channel(c):
        c = int(c, 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(hex_color[i:i + 2]) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


class Widget(models.Model):
    class Position(models.TextChoices):
        RIGHT = "right", "Bottom right"
        LEFT = "left", "Bottom left"

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="widget")
    enabled = models.BooleanField(default=False)
    public_key = models.CharField(max_length=64, unique=True, default=new_key, editable=False)
    title = models.CharField(max_length=60, default="Ask us")
    greeting = models.CharField(max_length=300, default="Hi! Ask me anything about our documents.")
    accent_color = models.CharField(max_length=7, default="#2a78d6", validators=[_hex_color])
    position = models.CharField(max_length=8, choices=Position.choices, default=Position.RIGHT)
    allowed_domains = models.TextField(
        blank=True,
        help_text="One per line, e.g. example.com, www.example.com, *.example.com, localhost:8000.",
    )
    rate_limit_per_minute = models.PositiveSmallIntegerField(
        default=10, help_text="Messages per minute from one visitor IP address.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Widget for {self.tenant}"

    def domains(self) -> list[str]:
        return list(dict.fromkeys(
            normalise_domain(d) for d in (self.allowed_domains or "").splitlines() if d.strip()
        ))

    @property
    def text_color(self) -> str:
        """White or near-black text on the accent colour, whichever has more contrast."""
        lum = _luminance(self.accent_color)
        white = 1.05 / (lum + 0.05)
        black = (lum + 0.05) / 0.0623
        return "#ffffff" if white >= black else "#0b0b0b"

    def frame_ancestors(self) -> str:
        """CSP frame-ancestors value: this site (for the preview) plus the allowed domains."""
        return " ".join(["'self'", *self.domains()])

    def regenerate_key(self):
        self.public_key = new_key()

    def clean(self):
        bad = [d for d in self.domains() if not _DOMAIN.match(d)]
        if bad:
            raise ValidationError({"allowed_domains": f"Not a valid domain: {', '.join(bad)}"})
        if "*" in self.domains() or any(d in ("http://*", "https://*") for d in self.domains()):
            raise ValidationError({"allowed_domains": "List specific domains; a lone * is not allowed."})
        if self.enabled and not self.domains():
            raise ValidationError({"allowed_domains": "Add at least one website before enabling the widget."})
        self.allowed_domains = "\n".join(self.domains())
        if not 1 <= self.rate_limit_per_minute <= 600:
            raise ValidationError({"rate_limit_per_minute": "Between 1 and 600."})
