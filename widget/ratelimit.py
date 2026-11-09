"""Fixed-window rate limit per widget and visitor IP (Django cache)."""
import time

from django.conf import settings
from django.core.cache import cache


def client_ip(request) -> str:
    if settings.WIDGET_TRUST_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


async def allow(widget, request) -> bool:
    window = int(time.time() // 60)
    key = f"widget-rl:{widget.pk}:{client_ip(request)}:{window}"
    if await cache.aadd(key, 1, timeout=70):
        count = 1
    else:
        try:
            count = await cache.aincr(key)
        except ValueError:  # expired between add and incr
            await cache.aset(key, 1, timeout=70)
            count = 1
    return count <= widget.rate_limit_per_minute
