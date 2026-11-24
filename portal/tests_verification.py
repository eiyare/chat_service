"""Confirming the email address of a new account before it can be used."""
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from siteconfig import conf
from siteconfig.models import PlatformSettings
from tenants.models import Tenant

from .models import EmailVerification

User = get_user_model()
PASSWORD = "Str0ng-pass-123"


def platform(**values):
    PlatformSettings(pk=1, **{**conf.defaults(), "site_url": "https://saaschat.test", "site_name": "Ei Learn",
                              **values}).save()
    conf.invalidate()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailVerificationTests(TestCase):
    def setUp(self):
        cache.clear()
        platform()

    def signup(self, username="ada", email="ada@example.com"):
        return self.client.post("/signup/", {"username": username, "email": email, "password1": PASSWORD,
                                             "password2": PASSWORD})

    def link(self, n=-1):
        return re.search(r"https://saaschat\.test(/verify/[^/\s]+/)", mail.outbox[n].body).group(1)

    def test_sign_up_confirm_then_create(self):
        resp = self.signup()
        self.assertRedirects(resp, "/signup/check-email/", fetch_redirect_response=False)
        user = User.objects.get(username="ada")
        self.assertFalse(user.is_active)
        self.assertTrue(EmailVerification.objects.get(user=user).pending)
        page = self.client.get("/signup/check-email/")
        self.assertContains(page, "a***@example.com")
        self.assertEqual(self.client.get("/create/").status_code, 302)  # not signed in

        sent = mail.outbox[0]
        self.assertEqual((sent.to, sent.subject), (["ada@example.com"], "Confirm your email for Ei Learn"))
        self.assertIn("Confirm my email", sent.alternatives[0][0])  # HTML version too
        self.assertIn("48 hours", sent.body)

        # Signing in before confirming says why, without "wrong password".
        resp = self.client.post("/accounts/login/", {"username": "ada", "password": PASSWORD})
        self.assertContains(resp, "Please confirm your email address first")
        self.assertContains(resp, "/verify/resend/")
        wrong = self.client.post("/accounts/login/", {"username": "ada", "password": "nope"})
        self.assertNotContains(wrong, "confirm your email")  # no hint without the right password

        resp = self.client.get(self.link())
        self.assertRedirects(resp, "/create/", fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertFalse(EmailVerification.objects.get(user=user).pending)
        self.assertContains(self.client.get("/create/"), "Create your chatbot")
        again = self.client.get(self.link(), follow=True)
        self.assertContains(again, "already confirmed")

    def test_expired_and_broken_links(self):
        self.signup()
        link = self.link()
        with mock.patch("time.time", return_value=__import__("time").time() + 49 * 3600):
            resp = self.client.get(link)
        self.assertContains(resp, "This link has expired", status_code=400)
        self.assertContains(resp, 'value="ada@example.com"', status_code=400)  # ready to send a new one
        self.assertContains(self.client.get(link[:-6] + "abcde/"), "doesn't work", status_code=400)
        User.objects.filter(username="ada").update(email="new@example.com")
        self.assertContains(self.client.get(link), "doesn't work", status_code=400)  # address changed
        self.assertFalse(User.objects.get(username="ada").is_active)

    def test_send_again_waits_a_minute(self):
        self.signup()
        self.client.post("/signup/check-email/")
        self.assertEqual(len(mail.outbox), 1)
        EmailVerification.objects.update(sent_at=timezone.now() - timedelta(minutes=2))
        self.client.post("/signup/check-email/")
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(EmailVerification.objects.get().sends, 2)

    def test_resend_by_address_does_not_reveal_accounts(self):
        self.signup()
        EmailVerification.objects.update(sent_at=timezone.now() - timedelta(minutes=2))
        self.client.logout()
        unknown = self.client.post("/verify/resend/", {"email": "nobody@example.com"})
        known = self.client.post("/verify/resend/", {"email": "ADA@example.com"})
        self.assertEqual(unknown.content.replace(b"nobody", b""), known.content.replace(b"ADA", b""))
        self.assertEqual(len(mail.outbox), 2)  # sign-up + the known address

    def test_signing_up_again_with_a_waiting_address(self):
        self.signup()
        resp = self.signup(username="ada2")
        self.assertContains(resp, "waiting to be confirmed")
        self.assertContains(resp, "/verify/resend/?email=ada%40example.com")

    def test_email_failure_is_shown_and_can_be_retried(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("535 auth failed")):
            self.signup()
        record = EmailVerification.objects.get()
        self.assertEqual(record.send_error, "OSError: 535 auth failed")
        self.assertContains(self.client.get("/signup/check-email/"), "couldn't send the confirmation email")

    def test_unconfirmed_accounts_are_deleted_after_the_set_days(self):
        self.signup()
        self.signup(username="bo", email="bo@example.com")
        self.signup(username="cy", email="cy@example.com")
        old = timezone.now() - timedelta(days=8)
        EmailVerification.objects.filter(user__username__in=["ada", "bo"]).update(created_at=old)
        Tenant.objects.create(name="Bo's", slug="bos", created_by=User.objects.get(username="bo"))
        from .verification import purge_unconfirmed

        self.assertEqual(purge_unconfirmed(force=True), 1)
        self.assertEqual(sorted(User.objects.values_list("username", flat=True)), ["bo", "cy"])
        platform(unverified_account_days=0)
        EmailVerification.objects.update(created_at=old)
        self.assertEqual(purge_unconfirmed(force=True), 0)  # 0 keeps them

    def test_switched_off_signs_in_straight_away(self):
        platform(require_email_verification=False)
        self.assertRedirects(self.signup(), "/create/", fetch_redirect_response=False)
        self.assertTrue(User.objects.get(username="ada").is_active)
        self.assertEqual(len(mail.outbox), 0)

    def test_existing_and_staff_made_accounts_are_unaffected(self):
        User.objects.create_user("old", "old@example.com", PASSWORD)
        resp = self.client.post("/accounts/login/", {"username": "old", "password": PASSWORD})
        self.assertEqual(resp.status_code, 302)

    def test_admin_can_confirm_or_resend(self):
        self.signup()
        boss = User.objects.create_superuser("boss", "boss@example.com", "pw")
        self.client.force_login(boss)
        record = EmailVerification.objects.get()
        self.assertContains(self.client.get("/admin/portal/emailverification/"), "ada@example.com")
        EmailVerification.objects.update(sent_at=None)
        self.client.post("/admin/portal/emailverification/", {"action": "send_again", "_selected_action": [record.pk]})
        self.assertEqual(len(mail.outbox), 2)
        self.client.post("/admin/portal/emailverification/", {"action": "confirm_now", "_selected_action": [record.pk]})
        self.assertTrue(User.objects.get(username="ada").is_active)

    def test_public_address_setting_is_validated(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "pw")
        self.client.force_login(boss)
        from siteconfig.tests import AdminTests

        data = AdminTests.form_data(None, site_url="https://saaschat.test/some/page")
        resp = self.client.post("/admin/siteconfig/platformsettings/1/change/", data)
        self.assertContains(resp, "Enter the address only")
