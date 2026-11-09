"""Login is required everywhere; access to a workspace comes from memberships."""
import json

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
        self.assertTrue(grounding_prompt(conf.static(self.t)).startswith("You are Tosin, the document assistant for the Acme workspace."))
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
