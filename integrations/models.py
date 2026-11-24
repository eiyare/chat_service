"""Apps connected to a workspace: Moodle, an Odoo or WordPress site, a custom app.

Each integration has a key and a secret, and can be allowed to:
- sign in its users, so the assistant greets them, keeps their conversations and, with a
  scope, answers from that scope's content only (integrations.identity);
- send content into named scopes, or into the shared scope that adds to the workspace's
  knowledge (integrations.api);
- offer tools the assistant may call back on it, described by the app's manifest
  (integrations.tools);
- copy the workspace's document passages (the Moodle "passage copy").
See docs/integrations.md for the protocol.
"""
import secrets

from django.db import models

from llm import crypto
from tenants.models import Tenant


def new_key() -> str:
    return secrets.token_urlsafe(12)


class Integration(models.Model):
    class Kind(models.TextChoices):
        MOODLE = "moodle", "Moodle"
        APP = "app", "App or website (Odoo, WordPress, custom)"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="integrations",
                               verbose_name="workspace")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.APP)
    name = models.CharField(max_length=100, help_text="e.g. \"School Moodle\" or \"Company website (Odoo)\"")
    key = models.CharField(max_length=32, unique=True, default=new_key, editable=False,
                           help_text="Public identifier, used in the API address and as the token issuer")
    secret_encrypted = models.TextField(blank=True, editable=False)
    enabled = models.BooleanField(default=True)
    site_url = models.URLField("App address", blank=True,
                               help_text="e.g. https://www.ei.consulting. Tool calls are sent to this site.")
    signed_users = models.BooleanField("Signed-in users", default=True,
                                       help_text="Accept users signed by the app: greeted by name, with their own "
                                                 "conversations")
    content = models.BooleanField("Content from the app", default=False,
                                  help_text="Accept content the app sends (e.g. Moodle courses, website pages)")
    tools = models.BooleanField("Tools", default=False,
                                help_text="Let the assistant call the tools the app describes (e.g. look up an order)")
    share_documents = models.BooleanField("Let the app copy passages", default=False,
                                          help_text="The app may download the workspace's document passages")
    manifest = models.JSONField(default=dict, blank=True, help_text="Tools the app offers (sent by the app)")
    manifest_updated_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True, help_text="The app's last call to saaschat")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant"], condition=models.Q(kind="moodle"),
                                               name="integrations_one_moodle_per_workspace")]

    def __str__(self):
        return f"{self.name} ({self.get_kind_display()})"

    def save(self, *args, **kwargs):
        if not self.secret_encrypted:
            self.regenerate_secret()
        super().save(*args, **kwargs)

    @property
    def secret(self) -> str:
        return crypto.decrypt(self.secret_encrypted)

    def regenerate_secret(self):
        self.secret_encrypted = crypto.encrypt(secrets.token_urlsafe(32))

    @property
    def manifest_tools(self) -> list[dict]:
        return list((self.manifest or {}).get("tools") or [])

    @property
    def callback_url(self) -> str:
        path = (self.manifest or {}).get("callback") or ""
        return self.site_url.rstrip("/") + path if self.site_url and path.startswith("/") else ""
