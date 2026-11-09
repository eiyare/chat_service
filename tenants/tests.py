from django.test import TestCase

from saas_chat.testing import TestCase  # signed in by default

from .models import Tenant


class TenantHomeTests(TestCase):
    def setUp(self):
        Tenant.objects.create(name="Acme", slug="acme")
        Tenant.objects.create(name="Dormant", slug="dormant", is_active=False)

    def test_active_tenant_home_redirects_to_chat(self):
        resp = self.client.get("/t/acme/")
        self.assertRedirects(resp, "/t/acme/chat/", fetch_redirect_response=False)

    def test_unknown_tenant_404(self):
        self.assertEqual(self.client.get("/t/missing/").status_code, 404)

    def test_inactive_tenant_404(self):
        self.assertEqual(self.client.get("/t/dormant/").status_code, 404)
