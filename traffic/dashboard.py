"""Figures for the admin Traffic dashboard, for a period, optionally one workspace, and
humans, bots or both."""
from datetime import timedelta

from django.db.models import Count, Q
from django.db.models.functions import TruncDay, TruncHour
from django.utils import timezone

from .models import RequestLog

PERIODS = {"24h": ("Last 24 hours", timedelta(hours=24)), "7d": ("Last 7 days", timedelta(days=7)),
           "30d": ("Last 30 days", timedelta(days=30)), "90d": ("Last 90 days", timedelta(days=90))}
AUDIENCES = {"all": "Everyone", "humans": "People only", "bots": "Bots only"}
PLATFORM = "platform"  # the workspace filter's value for requests outside any workspace
TOP = 10


def requests(period: str, tenant: str = "", audience: str = "all"):
    """The filtered requests. ``tenant`` is a workspace id, PLATFORM, or "" for everything."""
    since = timezone.now() - PERIODS[period][1]
    qs = RequestLog.objects.filter(created_at__gte=since)
    if tenant == PLATFORM:
        qs = qs.filter(tenant__isnull=True)
    elif tenant:
        qs = qs.filter(tenant_id=tenant)
    if audience == "humans":
        qs = qs.filter(is_bot=False)
    elif audience == "bots":
        qs = qs.filter(is_bot=True)
    return qs


def _median(qs, field: str):
    n = qs.count()
    if not n:
        return None
    return qs.order_by(field).values_list(field, flat=True)[n // 2]


def _top(qs, field: str, limit: int = TOP, **extra):
    qs = qs.exclude(**{f"{field}__isnull": True})
    if field != "ip":  # an empty IP address is stored as NULL, and ip="" would match nothing in SQL
        qs = qs.exclude(**{field: ""})
    rows = list(qs.values(field)
                .annotate(requests=Count("id"), visitors=Count("visitor", distinct=True), **extra)
                .order_by("-requests")[:limit])
    most = max((r["requests"] for r in rows), default=0)
    for r in rows:
        r["value"] = r[field]
        r["share"] = round(100 * r["requests"] / most, 1) if most else 0
    return rows


def _series(qs, period: str):
    """Requests and visitors per hour (24h) or day, with empty buckets filled in."""
    hourly = period == "24h"
    trunc = TruncHour("created_at") if hourly else TruncDay("created_at")
    counts = {r["bucket"]: r for r in qs.annotate(bucket=trunc).values("bucket")
              .annotate(requests=Count("id"), visitors=Count("visitor", distinct=True))}
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    now = timezone.localtime()
    end = now.replace(minute=0, second=0, microsecond=0) if hourly else now.replace(hour=0, minute=0, second=0,
                                                                                    microsecond=0)
    buckets = int(PERIODS[period][1] / step)
    out = []
    for i in range(buckets - 1, -1, -1):
        at = end - i * step
        row = counts.get(at)  # aware datetimes for the same instant are equal
        out.append({"at": at, "requests": row["requests"] if row else 0, "visitors": row["visitors"] if row else 0,
                    "label": at.strftime("%H:00") if hourly else at.strftime("%a %d %b")})
    most = max((b["requests"] for b in out), default=0)
    for b in out:
        b["height"] = round(100 * b["requests"] / most, 1) if most else 0
    return out


def figures(period: str = "7d", tenant: str = "", audience: str = "all") -> dict:
    qs = requests(period, tenant, audience)
    totals = qs.aggregate(
        requests=Count("id"), visitors=Count("visitor", distinct=True), ips=Count("ip", distinct=True),
        https=Count("id", filter=Q(scheme="https")), server_errors=Count("id", filter=Q(status__gte=500)),
        client_errors=Count("id", filter=Q(status__gte=400, status__lt=500)), bots=Count("id", filter=Q(is_bot=True)),
        signed_in=Count("user", distinct=True),
    )
    n = totals["requests"]

    def pct(x):
        return round(100 * x / n, 1) if n else None

    statuses = list(qs.values("status").annotate(requests=Count("id")).order_by("status"))
    by_tenant = []
    if not tenant:
        by_tenant = list(qs.values("tenant_id", "tenant__name").annotate(
            requests=Count("id"), visitors=Count("visitor", distinct=True),
            errors=Count("id", filter=Q(status__gte=500))).order_by("-requests"))
        most = max((r["requests"] for r in by_tenant), default=0)
        for r in by_tenant:
            r["share"] = round(100 * r["requests"] / most, 1) if most else 0
            r["filter"] = r["tenant_id"] or PLATFORM
            r["name"] = r["tenant__name"] or "Platform (no workspace)"
    return {
        "totals": {**totals, "https_pct": pct(totals["https"]), "server_error_pct": pct(totals["server_errors"]),
                   "client_error_pct": pct(totals["client_errors"]), "bot_pct": pct(totals["bots"]),
                   "median_ms": _median(qs, "duration_ms")},
        "series": _series(qs, period),
        "pages": _top(qs, "path", errors=Count("id", filter=Q(status__gte=500))),
        "ips": _top(qs, "ip"),
        "browsers": _top(qs, "browser"),
        "systems": _top(qs, "os"),
        "devices": _top(qs, "device"),
        "referrers": _top(qs, "referrer"),
        "areas": _top(qs, "area"),
        "statuses": statuses,
        "by_tenant": by_tenant,
    }
