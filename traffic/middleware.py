"""Records each request for the admin Traffic dashboard (traffic.models.RequestLog).

Which workspace a request belongs to comes from its URL: /<prefix>/<slug>/... (workspace
pages) or /w/<widget key>/... (widget, Moodle and other API calls). Everything else is
"Platform". Unique visitors are counted without cookies: a signed-in user is themselves,
anyone else is a hash of IP address, browser and the day (with a server secret), so the
same person counts once per day and can't be followed from day to day.

Settings: Django admin > Platform settings > Traffic. Recording never breaks a request:
any failure is logged and the response goes out as normal.
"""
import hashlib
import logging
import time
from urllib.parse import urlsplit

from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from siteconfig import conf

from . import agents

logger = logging.getLogger(__name__)
PURGE_EVERY = 3600  # seconds between deletions of logs older than the retention period
_tenants: dict[tuple, tuple] = {}  # (kind, value) -> (expires, tenant id or None)
TENANT_CACHE_SECONDS = 300


def _tenant_id(kind: str, value: str):
    """Tenant id for a workspace slug or widget key (cached in this process)."""
    entry = _tenants.get((kind, value))
    if entry and entry[0] > time.monotonic():
        return entry[1]
    if kind == "slug":
        from tenants.models import Tenant

        tid = Tenant.objects.filter(slug=value).values_list("pk", flat=True).first()
    else:
        from widget.models import Widget

        tid = Widget.objects.filter(public_key=value).values_list("tenant_id", flat=True).first()
    if len(_tenants) > 5000:
        _tenants.clear()
    _tenants[(kind, value)] = (time.monotonic() + TENANT_CACHE_SECONDS, tid)
    return tid


def _where(request):
    """(area, tenant id) of a request."""
    match = getattr(request, "resolver_match", None)
    kwargs = match.kwargs if match else {}
    tenant = getattr(request, "tenant", None)
    if match and match.namespace == "widget" and kwargs.get("key"):
        return "widget", _tenant_id("key", kwargs["key"])
    if kwargs.get("tenant_slug"):
        return "workspace", getattr(tenant, "pk", None) or _tenant_id("slug", kwargs["tenant_slug"])
    if request.path.startswith("/admin/"):
        return "admin", None
    return ("site", None) if match else ("other", None)


def _ip(request) -> str:
    from widget.ratelimit import client_ip  # the visitor's IP behind proxies (admin: Proxies in front of the app)

    return client_ip(request)


def _visitor(user_id, ip: str, user_agent: str) -> str:
    if user_id:
        return f"user:{user_id}"
    day = timezone.now().date().isoformat()
    raw = f"{day}|{ip}|{user_agent}|{settings.SECRET_KEY}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _excluded(path: str, cfg) -> bool:
    return any(path.startswith(p.strip()) for p in (cfg.traffic_exclude_paths or "").splitlines() if p.strip())


def _record(request, response, started: float, user_id, cfg) -> None:
    from .models import RequestLog

    area, tenant_id = _where(request)
    ua = request.META.get("HTTP_USER_AGENT", "")[:500]
    parsed = agents.parse(ua)
    ip = _ip(request)
    referrer = urlsplit(request.META.get("HTTP_REFERER", "")).hostname or ""
    RequestLog.objects.create(
        method=request.method[:8], scheme=request.scheme, host=request.get_host()[:255] if _host_ok(request) else "",
        path=request.path[:500], status=response.status_code, duration_ms=int((time.monotonic() - started) * 1000),
        area=area, tenant_id=tenant_id, user_id=user_id, ip=(ip or None) if cfg.traffic_store_ip else None,
        visitor=_visitor(user_id, ip, ua), user_agent=ua, referrer=referrer[:255], **parsed,
    )
    if cache.add("traffic:purged", 1, timeout=PURGE_EVERY):
        cutoff = timezone.now() - timezone.timedelta(days=cfg.traffic_retention_days)
        deleted, _ = RequestLog.objects.filter(created_at__lt=cutoff).delete()
        if deleted:
            logger.info("Deleted %s request logs older than %s days", deleted, cfg.traffic_retention_days)


def _host_ok(request) -> bool:
    try:
        request.get_host()
        return True
    except Exception:  # DisallowedHost: already answered with 400
        return False


def _user_id(user):
    return user.pk if user is not None and getattr(user, "is_authenticated", False) else None


class TrafficMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        started = time.monotonic()
        response = self.get_response(request)
        try:
            cfg = conf.get()
            if cfg.traffic_enabled and not _excluded(request.path, cfg):
                _record(request, response, started, _user_id(getattr(request, "user", None)), cfg)
        except Exception:
            logger.exception("Recording request %s failed", request.path)
        return response

    async def __acall__(self, request):
        started = time.monotonic()
        response = await self.get_response(request)
        try:
            cfg = await conf.aget()
            if cfg.traffic_enabled and not _excluded(request.path, cfg):
                user = await request.auser() if hasattr(request, "auser") else None
                await sync_to_async(_record)(request, response, started, _user_id(user), cfg)
        except Exception:
            logger.exception("Recording request %s failed", request.path)
        return response
