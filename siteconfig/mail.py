"""Outgoing email through the SMTP server set in Django admin (Platform settings > Email (SMTP)).

settings.EMAIL_BACKEND points here, so everything that sends mail (the contact form, Django's
sendtestemail and error emails) uses the admin settings, read when each batch is sent: changes
apply without a restart. With no SMTP server set, emails are written to the server log instead.
"""
import logging
from email.utils import parseaddr

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.console import EmailBackend as ConsoleBackend
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend

from . import conf

logger = logging.getLogger(__name__)


def sender(cfg=None) -> str:
    """The From address: "Send emails from", else the SMTP username if it is an address."""
    cfg = cfg or conf.get()
    if cfg.email_from.strip():
        return cfg.email_from.strip()
    username = cfg.smtp_username.strip()
    if "@" in parseaddr(username)[1]:
        return f"{cfg.site_name} <{username}>"
    return f"{cfg.site_name} <noreply@localhost>"


def configured(cfg=None) -> bool:
    return bool((cfg or conf.get()).smtp_host.strip())


class AdminSettingsBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        cfg = conf.get()
        self.from_email = sender(cfg)
        if configured(cfg):
            self.backend = SMTPBackend(
                host=cfg.smtp_host.strip(), port=cfg.smtp_port, username=cfg.smtp_username.strip() or None,
                password=cfg.smtp_password or None, use_tls=cfg.smtp_security == "starttls",
                use_ssl=cfg.smtp_security == "ssl", timeout=cfg.smtp_timeout, fail_silently=fail_silently,
            )
        else:
            logger.warning("No SMTP server is set (Platform settings > Email (SMTP)): writing emails to the log")
            self.backend = ConsoleBackend(fail_silently=fail_silently)

    def open(self):
        return self.backend.open()

    def close(self):
        return self.backend.close()

    def send_messages(self, email_messages):
        for message in email_messages:
            # Code that doesn't choose a sender gets the one set in admin.
            if message.from_email in (None, "", settings.DEFAULT_FROM_EMAIL, settings.SERVER_EMAIL):
                message.from_email = self.from_email
        return self.backend.send_messages(email_messages)
