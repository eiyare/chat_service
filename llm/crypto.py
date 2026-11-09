"""Encryption at rest for provider secrets stored in the database."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet() -> Fernet:
    key = settings.LLM_ENCRYPTION_KEY
    if not key:
        # Derive a stable key from SECRET_KEY. Rotating SECRET_KEY then makes
        # stored secrets unreadable; set LLM_ENCRYPTION_KEY to decouple them.
        key = base64.urlsafe_b64encode(hashlib.sha256(f"llm:{settings.SECRET_KEY}".encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        # Wrong/rotated key: treat as unset rather than crash every request.
        return ""
