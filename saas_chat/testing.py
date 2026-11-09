"""Test helpers. Every page requires login, so tests sign in by default."""
from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase


class TestCase(DjangoTestCase):
    """A TestCase whose ``client`` and ``async_client`` are signed in as a superuser
    (``self.signed_in_user``), who can open every workspace. Tests about access
    rules log in as someone else or call ``self.client.logout()``."""

    @classmethod
    def _pre_setup(cls):
        super()._pre_setup()
        user = get_user_model().objects.create_superuser("tester", "tester@example.com", "test-password")
        cls.signed_in_user = user
        cls.client.force_login(user)
        cls.async_client.force_login(user)
        # Tests configure behaviour with override_settings(RAG_*/CHAT_*): drop the platform
        # settings row (seeded by a migration) so those settings are the source; tests about
        # the admin settings create it themselves. Rolled back after each test.
        from siteconfig import conf
        from siteconfig.models import PlatformSettings

        PlatformSettings.objects.all().delete()
        conf.invalidate()
