"""Confirming a new account's email address before it can be used.

Sign-up creates the user inactive with an EmailVerification row and emails a link. The link
is signed (it can't be forged), expires (admin: "Confirmation links last"), and names the
address it was sent to, so it stops working if the account's email changes. Clicking it
activates the account. Settings: Django admin > Platform settings > Site and sign-up.
"""
import logging

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from siteconfig import conf
from siteconfig.mail import sender

from .models import EmailVerification

logger = logging.getLogger(__name__)
SALT = "portal.email-verification"
RESEND_AFTER_SECONDS = 60  # between links to the same account
PURGE_EVERY = 3600


def make_token(user) -> str:
    return signing.dumps({"u": user.pk, "e": user.email.lower()}, salt=SALT, compress=True)


def read_token(token: str, max_age_hours: int):
    """(user or None, problem). problem is "", "expired" or "invalid". An expired link still
    names its user, so the "send a new link" page can be filled in."""
    User = get_user_model()
    try:
        data = signing.loads(token, salt=SALT, max_age=max_age_hours * 3600)
        problem = ""
    except signing.SignatureExpired:
        data, problem = signing.loads(token, salt=SALT), "expired"
    except signing.BadSignature:
        return None, "invalid"
    user = User.objects.filter(pk=data.get("u")).first()
    if user is None or user.email.lower() != data.get("e"):
        return None, "invalid"
    return user, problem


def link(request, user) -> str:
    path = reverse("portal:verify_email", args=[make_token(user)])
    base = (conf.get().site_url or "").rstrip("/")
    return base + path if base else request.build_absolute_uri(path)


def pending(user) -> EmailVerification | None:
    record = EmailVerification.objects.filter(user=user).first()
    return record if record and record.pending else None


def start(user, request) -> EmailVerification:
    """Mark a new account as waiting for confirmation and send the link."""
    user.is_active = False
    user.save(update_fields=["is_active"])
    record, _ = EmailVerification.objects.update_or_create(user=user, defaults={"email": user.email})
    send(record, request)
    return record


def send(record: EmailVerification, request) -> bool:
    """Email the confirmation link; records when, how often and any failure."""
    cfg = conf.get()
    context = {"user": record.user, "link": link(request, record.user), "site_name": cfg.site_name,
               "hours": cfg.verification_link_hours}
    message = EmailMultiAlternatives(
        subject=f"Confirm your email for {cfg.site_name}",
        body=render_to_string("portal/email/verify.txt", context),
        from_email=sender(cfg),
        to=[record.email],
    )
    message.attach_alternative(render_to_string("portal/email/verify.html", context), "text/html")
    record.sent_at, record.sends = timezone.now(), record.sends + 1
    try:
        message.send(fail_silently=False)
        record.send_error = ""
        ok = True
    except Exception as exc:
        record.send_error = " ".join(f"{type(exc).__name__}: {exc}".split())[:500]
        logger.error("Confirmation email to %s failed: %s", record.email, record.send_error)
        ok = False
    record.save(update_fields=["sent_at", "sends", "send_error"])
    return ok


def can_resend(record: EmailVerification) -> bool:
    return record.sent_at is None or (timezone.now() - record.sent_at).total_seconds() >= RESEND_AFTER_SECONDS


def confirm(user) -> None:
    user.is_active = True
    user.save(update_fields=["is_active"])
    EmailVerification.objects.filter(user=user, verified_at__isnull=True).update(verified_at=timezone.now())


def purge_unconfirmed(force: bool = False) -> int:
    """Delete accounts never confirmed within the admin setting's days (hourly unless forced).
    Only sign-up accounts that are still inactive and own nothing are removed."""
    days = conf.get().unverified_account_days
    if days <= 0 or not (force or cache.add("portal:purge-unconfirmed", 1, timeout=PURGE_EVERY)):
        return 0
    cutoff = timezone.now() - timezone.timedelta(days=days)
    User = get_user_model()
    stale = User.objects.filter(is_active=False, is_staff=False, email_verification__verified_at__isnull=True,
                                email_verification__created_at__lt=cutoff)
    from tenants.models import Tenant

    stale = stale.exclude(pk__in=Tenant.objects.filter(created_by__isnull=False).values("created_by"))
    deleted = 0
    for user in stale:
        user.delete()
        deleted += 1
    if deleted:
        logger.info("Deleted %s accounts never confirmed within %s days", deleted, days)
    return deleted
