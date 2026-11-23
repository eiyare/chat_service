"""Login is required everywhere; access to a workspace comes from memberships."""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from chat.models import Conversation
from chat.views import grounding_prompt
from siteconfig import conf
from tenants.models import Membership, Tenant
from widget.models import Widget

User = get_user_model()


class AccessTests(TestCase):
    def setUp(self):
        self.acme = Tenant.objects.create(name="Acme", slug="acme")
        self.other = Tenant.objects.create(name="Other", slug="other")
        self.member = User.objects.create_user("mia", password="pw-123456")
        self.admin = User.objects.create_user("ade", password="pw-123456")
        Membership.objects.create(user=self.member, tenant=self.acme, role=Membership.Role.MEMBER)
        Membership.objects.create(user=self.admin, tenant=self.acme, role=Membership.Role.ADMIN)
        Membership.objects.create(user=self.admin, tenant=self.other, role=Membership.Role.MEMBER)

    def test_anonymous_users_are_sent_to_login(self):
        for url in ("/create/", "/t/acme/", "/t/acme/chat/", "/t/acme/admin/", "/t/acme/admin/performance/",
                    "/t/acme/admin/embed/"):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertTrue(resp["Location"].startswith("/accounts/login/?next="), url)
        self.assertEqual(self.client.get("/accounts/login/").status_code, 200)

    def test_login_and_logout(self):
        resp = self.client.post("/accounts/login/", {"username": "mia", "password": "wrong"})
        self.assertContains(resp, "didn't match")
        resp = self.client.post("/accounts/login/", {"username": "mia", "password": "pw-123456", "next": "/"})
        self.assertRedirects(resp, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.post("/accounts/logout/").status_code, 302)
        self.assertEqual(self.client.get("/t/acme/chat/").status_code, 302)

    def test_member_can_chat_but_not_manage(self):
        self.client.force_login(self.member)
        chat = self.client.get("/t/acme/chat/")
        self.assertEqual(chat.status_code, 200)
        self.assertNotContains(chat, 'href="/t/acme/admin/"')  # no Admin switch for members
        self.assertContains(chat, "Sign out")
        for url in ("/t/acme/admin/", "/t/acme/admin/performance/", "/t/acme/admin/embed/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.post("/t/acme/admin/documents/upload/").status_code, 403)

    def test_non_members_get_not_found(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get("/t/other/chat/").status_code, 404)
        self.assertEqual(self.client.get("/t/missing/chat/").status_code, 404)

    def test_workspace_admin_can_manage_only_where_admin(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/t/acme/chat/"), 'href="/t/acme/admin/"')
        for url in ("/t/acme/admin/", "/t/acme/admin/performance/", "/t/acme/admin/embed/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.client.get("/t/other/admin/").status_code, 403)

    def test_dashboard_lists_workspaces_with_the_right_actions(self):
        self.client.force_login(self.admin)
        home = self.client.get("/")
        self.assertContains(home, "Acme")
        self.assertContains(home, "Workspace admin")
        self.assertContains(home, "Other")
        self.assertContains(home, 'href="/t/acme/admin/"')       # Manage where admin
        self.assertNotContains(home, 'href="/t/other/admin/"')   # only chat where member
        self.client.force_login(self.member)
        self.assertContains(self.client.get("/"), 'href="/t/acme/chat/"')

    def test_conversations_are_private_to_their_owner(self):
        mine = Conversation.objects.create(tenant=self.acme, title="Mia's secret", model="m", user=self.member)
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get("/t/acme/chat/"), "Mia&#x27;s secret")
        self.assertEqual(self.client.get(f"/t/acme/chat/c/{mine.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"/t/acme/chat/api/c/{mine.pk}/delete/").status_code, 404)
        self.client.force_login(self.member)
        self.assertContains(self.client.get(f"/t/acme/chat/c/{mine.pk}/"), "Mia&#x27;s secret")

    def test_widget_stays_public(self):
        w = Widget.objects.create(tenant=self.acme, enabled=True, allowed_domains="acme.com")
        self.assertEqual(self.client.get(f"/w/{w.public_key}/").status_code, 200)
        self.assertEqual(self.client.get(f"/w/{w.public_key}/config.json").json()["enabled"], True)
        resp = self.client.post(f"/w/{w.public_key}/api/history/",
                                json.dumps({"visitor_id": "visitor-aaaaaaaaaaaaaaaa", "conversation_id": 1}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 404)  # reached the view (no login redirect)

    def test_django_admin_shows_memberships(self):
        boss = User.objects.create_superuser("boss", "b@example.com", "pw-123456")
        self.client.force_login(boss)
        self.assertContains(self.client.get(f"/admin/tenants/tenant/{self.acme.pk}/change/"), "Members (workspace access)")
        self.assertContains(self.client.get(f"/admin/auth/user/{self.member.pk}/change/"), "Workspace access")
        self.assertContains(self.client.get("/admin/auth/user/"), "Acme (member)")


class ChatbotNameTests(TestCase):
    def setUp(self):
        self.t = Tenant.objects.create(name="Acme", slug="acme")
        self.user = User.objects.create_superuser("boss", "b@example.com", "pw-123456")
        self.client.force_login(self.user)

    def test_default_name_is_tosin_and_used_everywhere(self):
        self.assertEqual(self.t.chatbot_name, "Tosin")
        self.assertTrue(grounding_prompt(conf.static(self.t)).startswith("You are Tosin, the assistant for Acme."))
        chat = self.client.get("/t/acme/chat/")
        self.assertContains(chat, "Hi, I'm Tosin")
        self.assertContains(chat, "Ask Tosin about your documents")
        self.client.get("/t/acme/admin/embed/")
        self.assertEqual(Widget.objects.get(tenant=self.t).title, "Ask Tosin")

    def test_rename_in_django_admin(self):
        data = {"name": "Acme", "slug": "acme", "is_active": "on", "chatbot_name": "  Ada ",
                "max_file_mb": "", "max_documents": "", "allowed_file_types": "",
                "token_allowance": "", "token_period": "", "max_output_tokens": "",
                "memberships-TOTAL_FORMS": "0", "memberships-INITIAL_FORMS": "0",
                "memberships-MIN_NUM_FORMS": "0", "settings_override-TOTAL_FORMS": "0", "settings_override-INITIAL_FORMS": "0", "settings_override-MIN_NUM_FORMS": "0", "settings_override-MAX_NUM_FORMS": "1", "memberships-MAX_NUM_FORMS": "1000"}
        page = self.client.get(f"/admin/tenants/tenant/{self.t.pk}/change/")
        self.assertContains(page, "Chatbot name")
        self.assertEqual(self.client.post(f"/admin/tenants/tenant/{self.t.pk}/change/", data).status_code, 302)
        self.t.refresh_from_db()
        self.assertEqual(self.t.chatbot_name, "Ada")
        self.assertContains(self.client.get("/t/acme/chat/"), "Hi, I'm Ada")
        resp = self.client.post(f"/admin/tenants/tenant/{self.t.pk}/change/", {**data, "chatbot_name": "  "})
        self.assertContains(resp, "This field is required")

    @override_settings(RAG_SYSTEM_PROMPT="I am {assistant} of {workspace}. {no_answer}", RAG_NO_ANSWER_MESSAGE="NOPE")
    def test_custom_prompt_placeholder(self):
        self.t.chatbot_name = "Bola"
        self.assertEqual(grounding_prompt(conf.static(self.t)), "I am Bola of Acme. NOPE")


class UseCasePageTests(TestCase):
    """Public use-case pages, driven by portal/use_cases.py."""

    def setUp(self):
        self.client.logout()  # these pages are for visitors

    def test_index_lists_every_use_case(self):
        from .use_cases import USE_CASES

        resp = self.client.get("/use-cases/")
        self.assertEqual(resp.status_code, 200)
        for case in USE_CASES:
            self.assertContains(resp, case.headline)
            self.assertContains(resp, f'href="/use-cases/{case.slug}/"')

    def test_every_use_case_page_renders(self):
        from .use_cases import USE_CASES

        for case in USE_CASES:
            with self.subTest(case.slug):
                resp = self.client.get(f"/use-cases/{case.slug}/")
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, case.name)  # breadcrumb
                self.assertContains(resp, "portal/usecase.css")

    def test_moodle_page_ties_each_feature_to_a_problem(self):
        resp = self.client.get("/use-cases/moodle/")
        content = resp.content.decode()
        self.assertEqual(content.count('class="problem"'), content.count('class="solution"'))
        self.assertGreaterEqual(content.count('class="problem"'), 6)
        self.assertEqual(content.count("<svg class=\"art\""), 8)
        self.assertEqual(content.count("<svg class=\"art\""), content.count("<title id=\"art-"))  # every image described
        for text in ("Quiz attempts", "Brain map", "Moodle 4.0", "What's my grade so far?"):
            self.assertContains(resp, text)

    def test_unknown_use_case_404(self):
        self.assertEqual(self.client.get("/use-cases/nope/").status_code, 404)

    def test_landing_and_nav_link_to_use_cases(self):
        resp = self.client.get("/")
        self.assertContains(resp, 'href="/use-cases/moodle/"')
        self.assertContains(resp, 'href="/use-cases/" class="nav-link"')


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DEFAULT_FROM_EMAIL="SaaS Chat <noreply@example.com>")
class ContactTests(TestCase):
    """Book a demo / Contact us: saved, then emailed to the helpdesk from the admin settings."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.client.logout()
        self.set_helpdesk("helpdesk@example.com")

    def set_helpdesk(self, address):
        from siteconfig.models import PlatformSettings

        PlatformSettings.objects.update_or_create(pk=1, defaults={**conf.defaults(), "helpdesk_email": address})
        conf.invalidate()

    def post(self, **data):
        payload = {"kind": "demo", "name": "Ada Obi", "email": "ada@school.edu.ng", "organisation": "Unity College",
                   "role": "Principal", "phone": "", "message": "We run Moodle 4.3.", "use_case": "moodle",
                   "website": ""}
        payload.update(data)
        return self.client.post("/contact/", payload)

    def test_demo_request_is_saved_and_emailed_to_the_helpdesk(self):
        from django.core import mail

        from portal.models import Enquiry

        resp = self.post()
        self.assertRedirects(resp, "/contact/thanks/", fetch_redirect_response=False)
        enquiry = Enquiry.objects.get()
        self.assertEqual((enquiry.kind, enquiry.use_case, enquiry.delivery, enquiry.sent_to),
                         ("demo", "moodle", "sent", "helpdesk@example.com"))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["helpdesk@example.com"])
        self.assertEqual(sent.reply_to, ["ada@school.edu.ng"])  # reply goes straight to them
        self.assertIn("Book a demo: Ada Obi (Unity College)", sent.subject)
        self.assertIn("We run Moodle 4.3.", sent.body)
        self.assertIn(f"/admin/portal/enquiry/{enquiry.pk}/change/", sent.body)
        self.assertContains(self.client.get("/contact/thanks/"), "find a time for your demo")

    def test_kept_even_when_email_fails_or_no_helpdesk_is_set(self):
        from django.core import mail

        from portal.models import Enquiry

        with mock.patch("django.core.mail.EmailMessage.send", side_effect=OSError("Connection refused")):
            self.assertRedirects(self.post(), "/contact/thanks/")
        failed = Enquiry.objects.get()
        self.assertEqual((failed.delivery, failed.delivery_error), ("failed", "OSError: Connection refused"))
        self.set_helpdesk("")
        self.assertRedirects(self.post(email="b@example.com"), "/contact/thanks/")
        self.assertEqual(Enquiry.objects.get(email="b@example.com").delivery, "no_helpdesk")
        self.assertEqual(len(mail.outbox), 0)

    def test_contact_needs_a_message_and_demo_does_not(self):
        resp = self.post(kind="contact", message="")
        self.assertContains(resp, "Tell us how we can help.")
        self.assertRedirects(self.post(kind="demo", message=""), "/contact/thanks/")

    def test_spam_trap_rate_limit_and_unknown_use_case(self):
        from portal.models import Enquiry

        self.assertRedirects(self.post(website="http://spam"), "/contact/thanks/")
        self.assertFalse(Enquiry.objects.exists())  # silently dropped
        self.post(use_case="not-a-page")
        self.assertEqual(Enquiry.objects.get().use_case, "")
        for _ in range(5):
            self.post()
        self.assertContains(self.post(), "Too many messages")

    def test_links_and_prefill(self):
        page = self.client.get("/contact/?topic=demo&use_case=moodle")
        self.assertRegex(page.content.decode(), r'<input[^>]*value="demo"[^>]*checked')
        self.assertContains(page, 'value="moodle"')
        self.assertContains(self.client.get("/use-cases/moodle/"), "/contact/?topic=demo&amp;use_case=moodle")
        self.assertContains(self.client.get("/"), 'href="/contact/"')

    def test_helpdesk_email_shown_only_when_chosen(self):
        from siteconfig.models import PlatformSettings

        self.assertNotContains(self.client.get("/contact/"), "helpdesk@example.com")
        PlatformSettings.objects.filter(pk=1).update(show_helpdesk_email=True)
        conf.invalidate()
        self.assertContains(self.client.get("/contact/"), "mailto:helpdesk@example.com")

    def test_admin_lists_and_resends(self):
        from django.core import mail

        from portal.models import Enquiry

        with mock.patch("django.core.mail.EmailMessage.send", side_effect=OSError("down")):
            self.post()
        admin_user = User.objects.create_superuser("boss", "boss@example.com", "pw-123456")
        self.client.force_login(admin_user)
        self.assertContains(self.client.get("/admin/portal/enquiry/"), "Unity College")
        enquiry = Enquiry.objects.get()
        self.client.post("/admin/portal/enquiry/", {"action": "send_again", "_selected_action": [enquiry.pk]})
        enquiry.refresh_from_db()
        self.assertEqual(enquiry.delivery, "sent")
        self.assertEqual(len(mail.outbox), 1)
