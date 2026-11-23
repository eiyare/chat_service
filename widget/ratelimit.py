"""Fixed-window rate limit per widget and visitor IP (Django cache), and the visitor's IP."""
import ipaddress
import time

from django.core.cache import cache

from siteconfig import conf


def _address(value: str) -> str:
    """A valid IP address from a header entry ("1.2.3.4", "1.2.3.4:5678", "[2001:db8::1]:443"), else ""."""
    value = (value or "").strip()
    if value.startswith("["):  # [IPv6]:port
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:  # IPv4:port
        value = value.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


def _from_proxy(remote: str) -> bool:
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def client_ip(request) -> str:
    """The visitor's IP address, for rate limits and the Traffic dashboard.

    Behind proxies (admin setting "Proxies in front of the app", e.g. 1 for nginx on the same
    server), the request arrives from the proxy and the visitor's address is in X-Forwarded-For.
    Each proxy appends the address it received from, so the visitor is the Nth entry from the
    right; entries further left were sent by the visitor and could be faked. The header is only
    used when the request comes from a private or loopback address (the proxy), so it can't be
    faked by reaching the app directly. X-Real-IP is used if X-Forwarded-For is missing.
    """
    remote = request.META.get("REMOTE_ADDR", "")
    proxies = conf.get().trusted_proxies
    if proxies <= 0 or not _from_proxy(remote):
        return remote
    forwarded = [p for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    if forwarded:
        candidate = forwarded[-proxies] if len(forwarded) >= proxies else forwarded[0]
    else:
        candidate = request.META.get("HTTP_X_REAL_IP", "")
    return _address(candidate) or remote


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
