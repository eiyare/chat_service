"""Django admin > Traffic: the dashboard (a proxy model's page) and the request list."""
from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import reverse

from tenants.models import Tenant

from . import dashboard
from .models import RequestLog, TrafficDashboard

DEVICE_LABELS = {"desktop": "Desktop", "mobile": "Mobile", "tablet": "Tablet", "bot": "Bot", "other": "Other"}


class StatusFilter(admin.SimpleListFilter):
    title = "status"
    parameter_name = "status_class"

    def lookups(self, request, model_admin):
        return [("2", "2xx success"), ("3", "3xx redirect"), ("4", "4xx client error"), ("5", "5xx server error")]

    def queryset(self, request, queryset):
        if self.value() in ("2", "3", "4", "5"):
            low = int(self.value()) * 100
            return queryset.filter(status__gte=low, status__lt=low + 100)
        return queryset


@admin.register(RequestLog)
class RequestLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "method", "path", "status", "duration_ms", "tenant", "ip", "browser", "os",
                    "device", "scheme")
    list_filter = (StatusFilter, "area", "tenant", "device", "is_bot", "browser", "os", "scheme", "method")
    search_fields = ("path", "ip", "user_agent", "referrer", "visitor", "user__username")
    date_hierarchy = "created_at"
    list_select_related = ("tenant",)
    readonly_fields = [f.name for f in RequestLog._meta.fields]
    show_full_result_count = False  # counting a large log on every page is slow

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(TrafficDashboard)
class TrafficDashboardAdmin(admin.ModelAdmin):
    """Requests, visitors, browsers, IPs ... for the whole platform or one workspace."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("traffic.view_requestlog")

    def changelist_view(self, request, extra_context=None):
        period = request.GET.get("period") if request.GET.get("period") in dashboard.PERIODS else "7d"
        audience = request.GET.get("audience") if request.GET.get("audience") in dashboard.AUDIENCES else "all"
        tenant = request.GET.get("tenant", "")
        tenants = list(Tenant.objects.order_by("name").values_list("pk", "name"))
        if tenant not in ([dashboard.PLATFORM] + [str(pk) for pk, _ in tenants]):
            tenant = ""
        data = dashboard.figures(period, tenant, audience)
        for row in data["areas"]:
            row["value"] = RequestLog.Area(row["value"]).label
        for row in data["devices"]:
            row["value"] = DEVICE_LABELS.get(row["value"], row["value"])
        tenant_name = ("Platform (no workspace)" if tenant == dashboard.PLATFORM
                       else dict((str(pk), n) for pk, n in tenants).get(tenant, ""))
        log_url = reverse("admin:traffic_requestlog_changelist")
        log_filter = ("?tenant__isnull=True" if tenant == dashboard.PLATFORM
                      else f"?tenant__id__exact={tenant}" if tenant else "?")
        context = {
            **self.admin_site.each_context(request),
            "title": f"Traffic: {tenant_name}" if tenant_name else "Traffic: whole platform",
            "opts": self.model._meta,
            "data": data, "period": period, "audience": audience, "tenant": tenant, "tenant_name": tenant_name,
            "periods": [(k, v[0]) for k, v in dashboard.PERIODS.items()],
            "audiences": list(dashboard.AUDIENCES.items()),
            "tenants": tenants, "platform": dashboard.PLATFORM,
            "log_url": log_url, "log_filter": log_filter,
            "hourly": period == "24h",
        }
        return TemplateResponse(request, "admin/traffic/dashboard.html", context)
