from django.conf import settings
from django.db import models


class RequestLog(models.Model):
    """One HTTP request to the app (traffic/middleware.py). Kept for the admin setting
    "Keep request logs for" days."""

    class Area(models.TextChoices):
        SITE = "site", "Public site"
        WORKSPACE = "workspace", "Workspace"
        WIDGET = "widget", "Widget and API"
        ADMIN = "admin", "Platform admin"
        OTHER = "other", "Other"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    method = models.CharField(max_length=8)
    scheme = models.CharField(max_length=5, help_text="http or https, as seen by the app")
    host = models.CharField(max_length=255, blank=True)
    path = models.CharField(max_length=500)
    status = models.PositiveSmallIntegerField()
    duration_ms = models.PositiveIntegerField(help_text="Until the response started (streamed replies run longer)")
    area = models.CharField(max_length=10, choices=Area.choices, db_index=True)
    tenant = models.ForeignKey("tenants.Tenant", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+", verbose_name="workspace")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    ip = models.GenericIPAddressField("IP address", null=True, blank=True, db_index=True)
    visitor = models.CharField(max_length=32, db_index=True,
                               help_text="The signed-in user, or a daily-changing hash of IP address and browser")
    user_agent = models.CharField(max_length=500, blank=True)
    browser = models.CharField(max_length=40)
    os = models.CharField("Operating system", max_length=40)
    device = models.CharField(max_length=10)
    is_bot = models.BooleanField(default=False)
    referrer = models.CharField(max_length=255, blank=True, help_text="The referring site (host only)")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "created_at"])]

    def __str__(self):
        return f"{self.method} {self.path} {self.status}"


class TrafficDashboard(RequestLog):
    """Proxy so the dashboard has its own entry in the admin's Traffic section."""

    class Meta:
        proxy = True
        verbose_name = "traffic dashboard"
        verbose_name_plural = "traffic dashboard"
