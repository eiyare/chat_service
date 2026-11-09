"""Public home page, sign-up and free chatbot creation (one per account)."""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from siteconfig import conf
from siteconfig.models import PlatformSettings
from tenants.models import Membership, Tenant

User = get_user_model()


def platform(**values):
    PlatformSettings(pk=1, **{**conf.defaults(), **values}).save()


class LandingTests(TestCase):
    def setUp(self):
        cache.clear()
        conf.invalidate()  # settings cache outlives the rolled-back rows of earlier tests

    def test_public_home_promotes_the_service_and_offers_sign_in(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        for text in ("Your documents,", "Grounded, not guessing", "On your website in one line",
                     "Speaks like your customers", "See what works", "Create your free chatbot", "Sign in"):
            self.assertContains(page, text)
        self.assertContains(page, 'action="/accounts/login/"')
        self.assertContains(page, 'href="/signup/"')

    def test_sign_up_can_be_closed_in_admin(self):
        platform(allow_signup=False)
        self.assertNotContains(self.client.get("/"), 'href="/signup/"')
        self.assertRedirects(self.client.get("/signup/"), "/")


class SignupAndCreateTests(TestCase):
    def setUp(self):
        cache.clear()
        conf.invalidate()

    def signup(self, username="ada", email="ada@example.com", **extra):
        return self.client.post("/signup/", {"username": username, "email": email, "password1": "Str0ng-pass-123",
                                             "password2": "Str0ng-pass-123", **extra})

    def test_sign_up_then_create_free_chatbot(self):
        resp = self.signup()
        self.assertRedirects(resp, "/create/", fetch_redirect_response=False)
        user = User.objects.get(username="ada")
        self.assertEqual(user.email, "ada@example.com")
        self.assertContains(self.client.get("/create/"), "Create your chatbot")

        resp = self.client.post("/create/", {"workspace_name": "Ada Bakery", "chatbot_name": "Bisi"})
        tenant = Tenant.objects.get(created_by=user)
        self.assertRedirects(resp, f"/t/{tenant.slug}/admin/", fetch_redirect_response=False)
        self.assertEqual((tenant.name, tenant.slug, tenant.chatbot_name), ("Ada Bakery", "ada-bakery", "Bisi"))
        self.assertEqual(Membership.objects.get(user=user, tenant=tenant).role, "admin")
        self.assertEqual(self.client.get(f"/t/{tenant.slug}/admin/").status_code, 200)

        home = self.client.get("/")
        self.assertContains(home, "Ada Bakery")
        self.assertContains(home, "You've created your free chatbot")
        self.assertNotContains(home, 'href="/create/"')

    def test_one_free_chatbot_per_account(self):
        self.signup()
        self.client.post("/create/", {"workspace_name": "First", "chatbot_name": "Tosin"})
        self.assertRedirects(self.client.get("/create/"), "/")
        self.client.post("/create/", {"workspace_name": "Second", "chatbot_name": "Tosin"})
        self.assertEqual(Tenant.objects.count(), 1)

    def test_limit_is_an_admin_setting_and_superusers_are_unlimited(self):
        platform(free_chatbots_per_account=2)
        self.signup()
        for name in ("One", "Two", "Three"):
            self.client.post("/create/", {"workspace_name": name, "chatbot_name": "Tosin"})
        self.assertEqual(Tenant.objects.count(), 2)
        boss = User.objects.create_superuser("boss", "b@example.com", "pw")
        self.client.force_login(boss)
        for name in ("Four", "Five"):
            self.client.post("/create/", {"workspace_name": name, "chatbot_name": "Tosin"})
        self.assertEqual(Tenant.objects.filter(created_by=boss).count(), 2)

    def test_unique_slugs(self):
        Tenant.objects.create(name="Acme", slug="acme")
        self.signup()
        self.client.post("/create/", {"workspace_name": "Acme", "chatbot_name": "Tosin"})
        self.assertTrue(Tenant.objects.filter(slug="acme-2").exists())

    def test_validation_spam_trap_and_rate_limit(self):
        resp = self.signup(email="not-an-email")
        self.assertContains(resp, "Enter a valid email address")
        self.signup(username="bot", email="bot@example.com", website="http://spam.example")
        self.assertFalse(User.objects.filter(username="bot").exists())
        User.objects.create_user("taken", email="dup@example.com", password="x")
        self.assertContains(self.signup(username="other", email="DUP@example.com"), "already exists")
        cache.clear()  # failed attempts count too; start the hourly window fresh
        for i in range(6):
            self.client.logout()
            resp = self.signup(username=f"user{i}", email=f"u{i}@example.com")
        self.assertContains(resp, "Too many sign-ups")
        self.assertEqual(User.objects.filter(username__startswith="user").count(), 5)

    def test_signed_in_users_skip_sign_up(self):
        self.signup()
        self.assertRedirects(self.client.get("/signup/"), "/create/", fetch_redirect_response=False)
