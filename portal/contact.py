"""Enquiries from the public site ("Book a demo", "Contact us"), emailed to the helpdesk.

The address is the admin setting "Helpdesk email" (Platform settings > Contact); mail goes out
through the SMTP server set in Django admin (Platform settings > Email (SMTP)). The enquiry is saved first, so it is
never lost: if the email can't be sent, staff still find it under Portal > Enquiries.
"""
import logging

from django.core.mail import EmailMessage
from django.urls import reverse

from siteconfig import conf
from siteconfig.mail import sender

from .models import Enquiry

logger = logging.getLogger(__name__)


def _one_line(text: str, limit: int = 80) -> str:
    return " ".join((text or "").split())[:limit]


def email_body(enquiry: Enquiry, admin_url: str = "") -> str:
    lines = [
        f"{enquiry.get_kind_display()} from the {conf.get().site_name} website",
        "",
        f"Name:          {enquiry.name}",
        f"Email:         {enquiry.email}",
        f"Organisation:  {enquiry.organisation or '-'}",
        f"Role:          {enquiry.role or '-'}",
        f"Phone:         {enquiry.phone or '-'}",
        f"Use case:      {enquiry.use_case or '-'}",
        "",
        "Message:",
        enquiry.message or "(none)",
        "",
        "Reply to this email to answer them directly.",
    ]
    if admin_url:
        lines.append(f"In Django admin: {admin_url}")
    return "\n".join(lines)


def deliver(enquiry: Enquiry, request=None) -> None:
    """Email the enquiry to the helpdesk and record how that went on the enquiry."""
    to = (conf.get().helpdesk_email or "").strip()
    if not to:
        enquiry.delivery = Enquiry.Delivery.NO_HELPDESK
        enquiry.save(update_fields=["delivery"])
        logger.warning("Enquiry %s not emailed: no helpdesk email is set (Platform settings > Contact)", enquiry.pk)
        return
    admin_url = ""
    if request is not None:
        admin_url = request.build_absolute_uri(reverse("admin:portal_enquiry_change", args=[enquiry.pk]))
    who = _one_line(enquiry.name) + (f" ({_one_line(enquiry.organisation)})" if enquiry.organisation else "")
    message = EmailMessage(
        subject=f"[{conf.get().site_name}] {enquiry.get_kind_display()}: {who}",
        body=email_body(enquiry, admin_url),
        from_email=sender(),
        to=[to],
        reply_to=[enquiry.email],
    )
    try:
        message.send(fail_silently=False)
    except Exception as exc:  # SMTP refused, unreachable, bad credentials ...
        enquiry.delivery = Enquiry.Delivery.FAILED
        enquiry.delivery_error = " ".join(f"{type(exc).__name__}: {exc}".split())[:500]
        logger.error("Enquiry %s could not be emailed to %s: %s", enquiry.pk, to, enquiry.delivery_error)
    else:
        enquiry.delivery = Enquiry.Delivery.SENT
    enquiry.sent_to = to
    enquiry.save(update_fields=["delivery", "delivery_error", "sent_to"])
