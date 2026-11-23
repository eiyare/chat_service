"""Resolved settings: workspace override -> platform setting -> .env default.

    from siteconfig import conf
    cfg = conf.get(tenant)          # sync
    cfg = await conf.aget(tenant)   # async
    cfg.no_answer_message, cfg.top_k, ...

Cached per process for SITECONFIG_CACHE_SECONDS; saving either model clears it.
"""
import asyncio
import threading
import time
from types import SimpleNamespace

from django.conf import settings
from django.db import DatabaseError

from .spec import BOOL, CHOICE, GROUNDING, LINE, PERIOD, SECRET, SPECS, TEXT, WORKSPACE_SPECS

_lock = threading.Lock()
_platform = None        # (expires, values dict)
_last_platform = None   # the last values loaded, kept after they expire (see get())
_workspaces = {}        # tenant_id -> (expires, overrides dict)


def invalidate(**kwargs):
    global _platform, _last_platform
    with _lock:
        _platform = None
        _last_platform = None  # saved settings must never be shadowed by older ones
        _workspaces.clear()


def defaults() -> dict:
    return {s.name: s.default() for s in SPECS}


def _row_values(row, specs, overrides: bool) -> dict:
    values = {}
    for s in specs:
        value = getattr(row, s.name)
        if s.kind == SECRET:
            from llm import crypto

            value = crypto.decrypt(value or "")
        if overrides and (value is None or (s.kind in (TEXT, LINE, PERIOD, GROUNDING, CHOICE, SECRET)
                                            and value == "")):
            continue  # blank override: inherit
        values[s.name] = value
    return values


def _ttl():
    return settings.SITECONFIG_CACHE_SECONDS


def _fresh(entry):
    return entry if entry and entry[0] > time.monotonic() else None


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _load_platform_sync() -> dict:
    from .models import PlatformSettings
    try:
        row = PlatformSettings.objects.filter(pk=1).first()
    except DatabaseError:  # before migrations
        return defaults()
    return {**defaults(), **(_row_values(row, SPECS, False) if row else {})}


def _load_workspace_sync(tenant_id) -> dict:
    from .models import WorkspaceSettings
    try:
        row = WorkspaceSettings.objects.filter(tenant_id=tenant_id).first()
    except DatabaseError:
        return {}
    return _row_values(row, WORKSPACE_SPECS, True) if row else {}


def _resolve(platform: dict, overrides: dict, tenant):
    values = {**platform, **overrides}
    values["overridden"] = frozenset(overrides)
    values["tenant"] = tenant
    return SimpleNamespace(**values)


def get(tenant=None):
    """Sync access. Inside an event loop with a cold cache it can't query the database, so it
    uses the last values loaded (or the code defaults before any load); async code should use
    aget()."""
    global _platform, _last_platform
    with _lock:
        platform = _fresh(_platform)
        overrides = _fresh(_workspaces.get(getattr(tenant, "pk", None))) if tenant else (0, {})
    if platform is None or overrides is None:
        if _in_event_loop():
            fallback = platform[1] if platform else (_last_platform or defaults())
            return _resolve(fallback, overrides[1] if overrides else {}, tenant)
        if platform is None:
            platform = (time.monotonic() + _ttl(), _load_platform_sync())
            _last_platform = platform[1]
            with _lock:
                _platform = platform if _ttl() > 0 else None
        if overrides is None:
            overrides = (time.monotonic() + _ttl(), _load_workspace_sync(tenant.pk))
            with _lock:
                if _ttl() > 0:
                    _workspaces[tenant.pk] = overrides
    return _resolve(platform[1], overrides[1], tenant)


async def aget(tenant=None):
    """Async access (loads from the database when the cache is cold)."""
    global _platform, _last_platform
    from asgiref.sync import sync_to_async

    with _lock:
        platform = _fresh(_platform)
        overrides = _fresh(_workspaces.get(getattr(tenant, "pk", None))) if tenant else (0, {})
    if platform is None:
        platform = (time.monotonic() + _ttl(), await sync_to_async(_load_platform_sync)())
        _last_platform = platform[1]
        with _lock:
            _platform = platform if _ttl() > 0 else None
    if overrides is None:
        overrides = (time.monotonic() + _ttl(), await sync_to_async(_load_workspace_sync)(tenant.pk))
        with _lock:
            if _ttl() > 0:
                _workspaces[tenant.pk] = overrides
    return _resolve(platform[1], overrides[1], tenant)


def static(tenant=None, **overrides):
    """Resolved settings from the .env defaults (plus overrides), without the database."""
    return _resolve({**defaults(), **overrides}, {}, tenant)


def fill(template: str, cfg) -> str:
    """Substitute {assistant} and {workspace} for the workspace in cfg."""
    t = cfg.tenant
    if t is None:
        return template
    return template.replace("{assistant}", t.chatbot_name).replace("{workspace}", t.name)
