"""Users signed in by a connected app, passed to the widget as data-user="<token>".

Token: base64url(JSON payload) + "." + hex HMAC-SHA256(integration secret, base64url part).
Payload (docs/integrations.md):
    iss    the integration's key (required, except for the Moodle plugin's older tokens)
    sub    the user's ID in the app (letters, digits, . _ @ -)
    name   their name, for the greeting
    exp    expiry, unix time
    scope  optional: answer from this scope's content only (e.g. "course:12", "client:acme")
    page   optional: the item the user is viewing in that scope (e.g. "cm:42")

The Moodle plugin's tokens have no "iss"; they carry "course", "cm" and "site", and are
checked with the workspace's Moodle integration (widget.moodle).
"""
import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field

from .models import Integration

MAX_NAME = 80
SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,99}$")
SUB = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


@dataclass
class Visitor:
    """A user signed in by a connected app."""
    integration: Integration
    user_id: str
    name: str = ""
    scope: str | None = None   # answer from this scope's content only
    page: str = ""             # the item they're viewing within the scope
    site: str = ""             # the app's address from the token (Moodle)
    token: str = field(default="", repr=False)

    @property
    def visitor_id(self) -> str:
        """The conversation owner. Each scope keeps its own conversations."""
        if self.integration.kind == Integration.Kind.MOODLE:  # unchanged from before integrations
            base = f"moodle-user-{self.user_id.zfill(8)}"
            course = self.scope.split(":", 1)[1] if self.scope and self.scope.startswith("course:") else ""
            return f"{base}-c{course}" if course else base
        # "." never appears in a browser's visitor ID (widget.views._VISITOR), so these can't be claimed.
        base = f"app.{self.integration.pk}.{self.user_id}"[:48]
        if self.scope:
            base += ".s" + hashlib.sha256(self.scope.encode()).hexdigest()[:12]
        return base


RESERVED_VISITOR_PREFIX = "moodle-"  # browsers may not claim Moodle students' visitor IDs


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(secret: str, payload: dict) -> str:
    """Make a token the way an app does (tests, docs and diagnostics)."""
    body = b64(json.dumps(payload).encode())
    return body + "." + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _payload(token) -> tuple[str, str, dict] | None:
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 4000:
        return None
    body, signature = token.split(".")
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, binascii.Error):
        return None
    return (body, signature, data) if isinstance(data, dict) else None


def _signed(secret: str, body: str, signature: str) -> bool:
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return bool(secret) and hmac.compare_digest(expected, signature)


def verify(tenant, token) -> Visitor | None:
    """The signed-in user for a token from one of the workspace's apps, else None."""
    parsed = _payload(token)
    if parsed is None:
        return None
    body, signature, data = parsed
    if not data.get("iss"):
        return _moodle(tenant, token)
    integration = Integration.objects.filter(tenant=tenant, key=str(data["iss"]), enabled=True,
                                             signed_users=True).first()
    if integration is None or not _signed(integration.secret, body, signature):
        return None
    try:
        user_id, exp = str(data["sub"]), int(data["exp"])
    except (KeyError, ValueError, TypeError):
        return None
    if exp < time.time() or not SUB.match(user_id):
        return None
    scope = data.get("scope") or None
    if scope is not None and (not isinstance(scope, str) or not SCOPE.match(scope)):
        return None
    page = data.get("page") or ""
    if not isinstance(page, str) or (page and not SCOPE.match(page)):
        page = ""
    if not integration.content:
        scope, page = None, ""
    return Visitor(integration, user_id, str(data.get("name") or "")[:MAX_NAME], scope, page if scope else "",
                   token=token)


def _moodle(tenant, token) -> Visitor | None:
    from widget import moodle

    integration = Integration.objects.filter(tenant=tenant, kind=Integration.Kind.MOODLE, enabled=True,
                                             signed_users=True).first()
    if integration is None:
        return None
    user = moodle.verify_user(integration.secret, token)
    if user is None:
        return None
    scope = f"course:{user['course']}" if user["course"] and integration.content else None
    page = f"cm:{user['activity']}" if scope and user["activity"] else ""
    return Visitor(integration, user["id"], user["name"], scope, page, user["site"], token=token)
