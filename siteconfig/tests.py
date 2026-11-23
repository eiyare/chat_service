"""Admin-managed settings: platform-wide values and per-workspace overrides."""
import json
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import override_settings

import llm
from chat.models import Message
from saas_chat.testing import TestCase
from tenants.models import Tenant

from . import conf
from .models import PlatformSettings, WorkspaceSettings
from .spec import SPECS


def platform(**values):
    row = PlatformSettings(pk=1, **{**conf.defaults(), **values})
    row.save()
    return row


class ResolutionTests(TestCase):
    def setUp(self):
        self.a = Tenant.objects.create(name="Acme", slug="acme", chatbot_name="Ada")
        self.b = Tenant.objects.create(name="Beta", slug="beta")

    def test_defaults_then_platform_then_workspace(self):
        self.assertEqual(conf.get(self.a).no_answer_message, conf.defaults()["no_answer_message"])
        platform(no_answer_message="Platform: not found", top_k=7, small_talk=True)
        self.assertEqual((conf.get(self.a).no_answer_message, conf.get(self.a).top_k), ("Platform: not found", 7))
        WorkspaceSettings.objects.create(tenant=self.a, no_answer_message="Acme: not found", small_talk=False)
        a, b = conf.get(self.a), conf.get(self.b)
        self.assertEqual((a.no_answer_message, a.top_k, a.small_talk), ("Acme: not found", 7, False))
        self.assertEqual((b.no_answer_message, b.small_talk), ("Platform: not found", True))  # other workspace
        self.assertEqual(a.overridden, frozenset({"no_answer_message", "small_talk"}))

    def test_blank_overrides_inherit(self):
        platform(greeting_reply="Hello from the platform")
        WorkspaceSettings.objects.create(tenant=self.a, greeting_reply="", temperature=None, small_talk=None)
        a = conf.get(self.a)
        self.assertEqual(a.greeting_reply, "Hello from the platform")
        self.assertEqual(a.overridden, frozenset())

    def test_async_access_and_placeholders(self):
        platform(greeting_reply="Hi, I'm {assistant} of {workspace}")
        cfg = async_to_sync(conf.aget)(self.a)
        self.assertEqual(conf.fill(cfg.greeting_reply, cfg), "Hi, I'm Ada of Acme")

    def test_every_setting_has_a_working_default(self):
        values = conf.defaults()
        self.assertEqual(set(values), {s.name for s in SPECS})
        platform()  # all defaults are valid model values


@override_settings(LLM_CHAT_MODELS=["p/chat"], LLM_DEFAULT_CHAT_MODEL="p/chat", LLM_EMBED_MODEL="p/embed",
                   RAG_QUERY_PREFIX="", RAG_DOCUMENT_PREFIX="", LLM_CONFIG_CACHE_SECONDS=0)
@mock.patch("llm.embed", mock.AsyncMock(side_effect=lambda texts, purpose="document": [[0.0, 1.0] for _ in texts]))
class BehaviourTests(TestCase):
    def setUp(self):
        llm.invalidate()
        self.a = Tenant.objects.create(name="Acme", slug="acme")
        self.b = Tenant.objects.create(name="Beta", slug="beta")

    def ask(self, slug, message):
        resp = self.client.post(f"/t/{slug}/chat/api/send/", json.dumps({"message": message, "model": "p/chat"}),
                                content_type="application/json")
        from widget.tests import _read
        events = [json.loads(l) for l in async_to_sync(_read)(resp).decode().splitlines()]
        return events[2]["text"]

    def test_admin_changes_apply_to_replies_immediately(self):
        platform(empty_kb_message="Nothing uploaded in {workspace} yet.",
                 greeting_reply="Hello! I am {assistant}.")
        WorkspaceSettings.objects.create(tenant=self.a, greeting_reply="Welcome to Acme, I'm {assistant}!")
        self.assertEqual(self.ask("acme", "Hi Tosin"), "Welcome to Acme, I'm Tosin!")
        self.assertEqual(self.ask("beta", "Hi Tosin"), "Hello! I am Tosin.")
        self.assertEqual(self.ask("beta", "What is the leave policy?"), "Nothing uploaded in Beta yet.")
        WorkspaceSettings.objects.filter(tenant=self.a).update(small_talk=False)
        conf.invalidate()  # .update() skips signals; admin saves invalidate automatically
        self.assertEqual(self.ask("acme", "Hi Tosin"), "Nothing uploaded in Acme yet.")

    def test_platform_defaults_drive_workspace_limits(self):
        platform(default_max_documents=1, default_allowed_file_types="pdf", default_token_allowance=500)
        policy, tokens = self.a.upload_policy, self.a.token_policy
        self.assertEqual((policy.max_documents, policy.extensions, tokens.allowance), (1, ("pdf",), 500))
        self.a.max_documents = 5  # the workspace's own limit still wins
        self.assertEqual(self.a.upload_policy.max_documents, 5)


class AdminTests(TestCase):
    def setUp(self):
        self.t = Tenant.objects.create(name="Acme", slug="acme")

    def form_data(self, **overrides):
        data = {k: ("on" if v is True else "" if v is False else v) for k, v in {**conf.defaults(), **overrides}.items()}
        return {k: v for k, v in data.items() if v != ""}

    def test_platform_page_is_a_single_editable_page(self):
        resp = self.client.get("/admin/siteconfig/platformsettings/")
        self.assertRedirects(resp, "/admin/siteconfig/platformsettings/1/change/")
        page = self.client.get("/admin/siteconfig/platformsettings/1/change/")
        for section in ("Assistant wording", "Persona (Nigerian Pidgin)", "Search and FAQs", "Workspace defaults"):
            self.assertContains(page, section)
        self.assertEqual(self.client.get("/admin/siteconfig/platformsettings/add/").status_code, 403)

        resp = self.client.post("/admin/siteconfig/platformsettings/1/change/",
                                self.form_data(no_answer_message="Sorry, not in our files."), follow=True)
        self.assertContains(resp, "Platform settings saved.")
        self.assertEqual(conf.get().no_answer_message, "Sorry, not in our files.")

    def test_platform_validation(self):
        self.client.get("/admin/siteconfig/platformsettings/")
        resp = self.client.post("/admin/siteconfig/platformsettings/1/change/",
                                self.form_data(chunk_size=200, chunk_overlap=300, default_allowed_file_types="exe"))
        self.assertContains(resp, "Must be smaller than the chunk size.")
        self.assertContains(resp, "Choose from")

    def test_workspace_overrides_on_tenant_page(self):
        platform(no_answer_message="Platform reply")
        page = self.client.get(f"/admin/tenants/tenant/{self.t.pk}/change/")
        self.assertContains(page, "Settings overrides (blank = use the platform settings)")
        self.assertContains(page, "Blank = platform setting: \u201cPlatform reply\u201d")
        data = {"name": "Acme", "slug": "acme", "is_active": "on", "chatbot_name": "Tosin",
                "memberships-TOTAL_FORMS": "0", "memberships-INITIAL_FORMS": "0",
                "memberships-MIN_NUM_FORMS": "0", "memberships-MAX_NUM_FORMS": "1000",
                "settings_override-TOTAL_FORMS": "1", "settings_override-INITIAL_FORMS": "0",
                "settings_override-MIN_NUM_FORMS": "0", "settings_override-MAX_NUM_FORMS": "1",
                "settings_override-0-no_answer_message": "Acme reply", "settings_override-0-small_talk": "unknown",
                "settings_override-0-tenant": str(self.t.pk)}
        resp = self.client.post(f"/admin/tenants/tenant/{self.t.pk}/change/", data)
        self.assertEqual(resp.status_code, 302)
        cfg = conf.get(Tenant.objects.get(pk=self.t.pk))
        self.assertEqual((cfg.no_answer_message, cfg.small_talk), ("Acme reply", conf.get().small_talk))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailSettingsTests(TestCase):
    """SMTP and the helpdesk, set in Django admin (Platform settings > Email (SMTP) and Contact)."""

    url = "/admin/siteconfig/platformsettings/1/change/"

    def form_data(self, **overrides):
        data = {k: ("on" if v is True else "" if v is False else v) for k, v in {**conf.defaults(), **overrides}.items()}
        return {k: v for k, v in data.items() if v != ""}

    def save(self, **values):
        self.client.get("/admin/siteconfig/platformsettings/")  # creates the row
        return self.client.post(self.url, self.form_data(**values), follow=True)

    def test_smtp_settings_are_in_admin_and_the_password_is_encrypted(self):
        page = self.client.get(self.client.get("/admin/siteconfig/platformsettings/").url)
        for text in ("Email (SMTP)", "SMTP server", "Encryption", "Send emails from", "Send a test email"):
            self.assertContains(page, text)
        self.save(smtp_host="smtp.example.com", smtp_port=587, smtp_security="starttls",
                  smtp_username="mailer@example.com", smtp_password="s3cret-pass", email_from="Acme <hi@example.com>")
        row = PlatformSettings.objects.get(pk=1)
        self.assertNotIn("s3cret-pass", row.smtp_password)  # encrypted at rest
        self.assertEqual(conf.get().smtp_password, "s3cret-pass")
        page = self.client.get(self.url)
        self.assertNotContains(page, "s3cret-pass")
        self.assertContains(page, "A value is saved. Leave blank to keep it.")
        # Saving again with the field blank keeps it; "Clear" removes it.
        self.save(smtp_host="smtp.example.com", smtp_username="mailer@example.com")
        self.assertEqual(conf.get().smtp_password, "s3cret-pass")
        self.save(smtp_host="smtp.example.com", smtp_password_clear=True)
        self.assertEqual(conf.get().smtp_password, "")

    def test_validation(self):
        resp = self.save(email_from="not an address", smtp_security="ssl", smtp_port=587, helpdesk_email="nope")
        self.assertContains(resp, "Enter an address")
        self.assertContains(resp, "Port 587 uses STARTTLS")
        self.assertContains(resp, "Enter one email address")

    def test_backend_uses_the_admin_settings(self):
        from django.core.mail.backends.console import EmailBackend as Console
        from django.core.mail.backends.smtp import EmailBackend as SMTP

        from .mail import AdminSettingsBackend, sender

        self.assertIsInstance(AdminSettingsBackend().backend, Console)  # nothing set: log only
        self.assertEqual(sender(), "SaaS Chat <noreply@localhost>")
        platform(smtp_host="smtp.example.com", smtp_port=465, smtp_security="ssl", smtp_username="u@example.com",
                 smtp_timeout=9)
        conf.invalidate()
        smtp = AdminSettingsBackend().backend
        self.assertIsInstance(smtp, SMTP)
        self.assertEqual((smtp.host, smtp.port, smtp.username, smtp.use_ssl, smtp.use_tls, smtp.timeout),
                         ("smtp.example.com", 465, "u@example.com", True, False, 9))
        self.assertEqual(sender(), "SaaS Chat <u@example.com>")  # no sender set: the username

        from django.core.mail import EmailMessage

        sent = []
        with mock.patch.object(SMTP, "send_messages", lambda self, msgs: sent.extend(msgs) or len(msgs)):
            EmailMessage("Hi", "Body", None, ["x@example.com"], connection=AdminSettingsBackend()).send()
        self.assertEqual(sent[0].from_email, "SaaS Chat <u@example.com>")  # default sender replaced

    def test_send_a_test_email_button(self):
        from django.core import mail

        self.save(helpdesk_email="helpdesk@example.com", smtp_host="smtp.example.com")
        resp = self.client.post("/admin/siteconfig/platformsettings/test-email/", follow=True)
        self.assertContains(resp, "Test email sent to helpdesk@example.com through smtp.example.com")
        self.assertEqual(mail.outbox[0].to, ["helpdesk@example.com"])
        with mock.patch("django.core.mail.EmailMessage.send", side_effect=OSError("535 Authentication failed")):
            resp = self.client.post("/admin/siteconfig/platformsettings/test-email/", follow=True)
        self.assertContains(resp, "The test email to helpdesk@example.com failed: OSError: 535 Authentication failed")
        self.assertEqual(self.client.get("/admin/siteconfig/platformsettings/test-email/").status_code, 405)
