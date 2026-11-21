"""Moodle plugin support: signed student identity and the shared-code check.

The plugin signs the logged-in student with the widget's shared code:

    payload = base64url(JSON {"sub": <Moodle user id>, "name": <full name>, "exp": <unix time>,
                              "site": <the Moodle site's address, for student data lookups>,
                              "course": <course ID, only on the pages of a course the student can access>,
                              "cm": <course module ID, on an activity's pages within that course>})
    token   = payload + "." + hex(HMAC-SHA256(shared code, payload))

In PHP: hash_hmac('sha256', $payload, $code).
"""
import base64
import binascii
import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit

MAX_NAME = 80


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_user(code: str, user_id, name: str, ttl: int = 12 * 3600, course=None, activity=None, site=None) -> str:
    """Make a token the way the plugin does (used by tests and diagnostics)."""
    data = {"sub": user_id, "name": name, "exp": int(time.time()) + ttl}
    if site is not None:
        data["site"] = site
    if course is not None:
        data["course"] = course
    if activity is not None:
        data["cm"] = activity
    payload = _b64(json.dumps(data).encode())
    return payload + "." + hmac.new(code.encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify_user(code: str, token) -> dict | None:
    """{"id": "...", "name": "...", "course": int | None, "activity": int | None} for a valid,
    unexpired token, else None. "activity" (the course module the student is viewing) is
    only kept with a course. "site" is the Moodle address (http/https URL) or ""."""
    if not code or not isinstance(token, str) or token.count(".") != 1 or len(token) > 2000:
        return None
    payload, signature = token.split(".")
    expected = hmac.new(code.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        user_id, exp = str(data["sub"]), int(data["exp"])
        course = data.get("course")
        course = None if course in (None, "") else int(course)
        activity = data.get("cm")
        activity = None if activity in (None, "") or course is None else int(activity)
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
        return None
    if exp < time.time() or not user_id.isdigit() or (course is not None and course <= 0) \
            or (activity is not None and activity <= 0):
        return None
    site = str(data.get("site") or "").rstrip("/")
    if site and (urlsplit(site).scheme not in ("http", "https") or not urlsplit(site).hostname):
        site = ""
    return {"id": user_id, "name": str(data.get("name") or "")[:MAX_NAME], "course": course, "activity": activity,
            "site": site}


def visitor_id(user: dict) -> str:
    """Stable widget visitor ID for a Moodle student (matches the visitor ID format).
    Each course gets its own conversations (pass a user dict without "course" for general ones)."""
    base = f"moodle-user-{user['id'].zfill(8)}"
    return f"{base}-c{user['course']}" if user.get("course") else base


def check_shared_code(widget, request) -> bool:
    """Server-to-server calls from the plugin send `Authorization: Bearer <shared code>`."""
    code = widget.moodle_shared_code
    sent = request.headers.get("Authorization", "")
    return bool(code) and sent.startswith("Bearer ") and hmac.compare_digest(sent[7:].encode(), code.encode())
