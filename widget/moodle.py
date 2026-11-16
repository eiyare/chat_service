"""Moodle plugin support: signed student identity and the shared-code check.

The plugin signs the logged-in student with the widget's shared code:

    payload = base64url(JSON {"sub": <Moodle user id>, "name": <full name>, "exp": <unix time>})
    token   = payload + "." + hex(HMAC-SHA256(shared code, payload))

In PHP: hash_hmac('sha256', $payload, $code).
"""
import base64
import binascii
import hashlib
import hmac
import json
import time

MAX_NAME = 80


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_user(code: str, user_id, name: str, ttl: int = 12 * 3600) -> str:
    """Make a token the way the plugin does (used by tests and diagnostics)."""
    payload = _b64(json.dumps({"sub": user_id, "name": name, "exp": int(time.time()) + ttl}).encode())
    return payload + "." + hmac.new(code.encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify_user(code: str, token) -> dict | None:
    """{"id": "...", "name": "..."} for a valid, unexpired token, else None."""
    if not code or not isinstance(token, str) or token.count(".") != 1 or len(token) > 2000:
        return None
    payload, signature = token.split(".")
    expected = hmac.new(code.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        user_id, exp = str(data["sub"]), int(data["exp"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    if exp < time.time() or not user_id.isdigit():
        return None
    return {"id": user_id, "name": str(data.get("name") or "")[:MAX_NAME]}


def visitor_id(user: dict) -> str:
    """Stable widget visitor ID for a Moodle student (matches the visitor ID format)."""
    return f"moodle-user-{user['id'].zfill(8)}"


def check_shared_code(widget, request) -> bool:
    """Server-to-server calls from the plugin send `Authorization: Bearer <shared code>`."""
    code = widget.moodle_shared_code
    sent = request.headers.get("Authorization", "")
    return bool(code) and sent.startswith("Bearer ") and hmac.compare_digest(sent[7:].encode(), code.encode())
