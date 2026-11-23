"""Request recording (middleware) and the admin Traffic dashboard."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.utils import timezone

from saas_chat.testing import TestCase
from siteconfig import conf
from siteconfig.models import PlatformSettings
from tenants.models import Tenant
from widget.models import Widget

from . import agents
from .middleware import _visitor
from .models import RequestLog

CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/17.5 Mobile/15E148 Safari/604.1")


def settings_row(**values):
    PlatformSettings.objects.update_or_create(pk=1, defaults={**conf.defaults(), **values})
    conf.invalidate()


class RecordingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.acme = Tenant.objects.create(name="Acme", slug="acme")
        self.widget = Widget.objects.create(tenant=self.acme, enabled=True, allowed_domains="acme.com")

    def test_public_page_is_recorded_with_browser_device_and_referrer(self):
        self.client.logout()
        self.client.get("/use-cases/", HTTP_USER_AGENT=IPHONE, HTTP_REFERER="https://www.google.com/search?q=x",
                        REMOTE_ADDR="203.0.113.7")
        log = RequestLog.objects.get(path="/use-cases/")
        self.assertEqual((log.method, log.status, log.area, log.tenant_id, log.user_id),
                         ("GET", 200, "site", None, None))
        self.assertEqual((log.browser, log.os, log.device, log.is_bot), ("Safari", "iOS", "mobile", False))
        self.assertEqual((log.ip, log.referrer, log.scheme), ("203.0.113.7", "www.google.com", "http"))
        self.assertEqual(len(log.visitor), 24)  # anonymous: a hash, no cookie
        self.assertNotIn("sessionid", self.client.cookies)

    def test_https_and_workspace_pages_are_attributed(self):
        self.client.get("/t/acme/chat/", secure=True, HTTP_USER_AGENT=CHROME)
        log = RequestLog.objects.get(path="/t/acme/chat/")
        self.assertEqual((log.scheme, log.area, log.tenant_id), ("https", "workspace", self.acme.pk))
        self.assertEqual((log.user_id, log.visitor), (self.signed_in_user.pk, f"user:{self.signed_in_user.pk}"))

    async def test_widget_and_api_calls_are_attributed_in_async_views(self):
        await self.async_client.get(f"/w/{self.widget.public_key}/config.json")
        log = await RequestLog.objects.aget(path=f"/w/{self.widget.public_key}/config.json")
        self.assertEqual((log.area, log.tenant_id, log.status), ("widget", self.acme.pk, 200))

    def test_settings_exclusions_ip_and_switch(self):
        settings_row(traffic_store_ip=False)
        self.client.get("/use-cases/", REMOTE_ADDR="203.0.113.7")
        self.client.get("/static/portal/portal.css")
        log = RequestLog.objects.get()
        self.assertIsNone(log.ip)  # not kept
        self.assertTrue(log.visitor)  # still counted
        settings_row(traffic_enabled=False)
        self.client.get("/use-cases/")
        self.assertEqual(RequestLog.objects.count(), 1)

    def test_visitors_are_counted_per_day_without_cookies(self):
        self.assertEqual(_visitor(None, "1.2.3.4", CHROME), _visitor(None, "1.2.3.4", CHROME))
        self.assertNotEqual(_visitor(None, "1.2.3.4", CHROME), _visitor(None, "1.2.3.4", IPHONE))
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(days=1)):
            tomorrow = _visitor(None, "1.2.3.4", CHROME)
        self.assertNotEqual(tomorrow, _visitor(None, "1.2.3.4", CHROME))

    def test_old_logs_are_deleted_hourly(self):
        settings_row(traffic_retention_days=30)
        old = RequestLog.objects.create(method="GET", scheme="http", path="/old/", status=200, duration_ms=1,
                                        area="site", visitor="x", browser="Chrome", os="Windows", device="desktop")
        RequestLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=31))
        self.client.get("/use-cases/")
        self.assertFalse(RequestLog.objects.filter(path="/old/").exists())

    def test_a_recording_failure_never_breaks_the_page(self):
        with mock.patch("traffic.models.RequestLog.objects.create", side_effect=RuntimeError("db down")):
            self.assertEqual(self.client.get("/use-cases/").status_code, 200)

    def test_bots_are_recognised(self):
        for ua, name in (("Mozilla/5.0 (compatible; Googlebot/2.1)", "Googlebot"), ("curl/8.5.0", "curl"),
                         ("MoodleBot/1.0", "MoodleBot")):
            self.assertEqual(agents.parse(ua)["browser"], name)
            self.assertTrue(agents.parse(ua)["is_bot"])


class DashboardTests(TestCase):
    url = "/admin/traffic/trafficdashboard/"

    def setUp(self):
        cache.clear()
        settings_row(traffic_exclude_paths="/admin/")  # keep the dashboard's own requests out of these figures
        self.acme = Tenant.objects.create(name="Acme", slug="acme")
        self.beta = Tenant.objects.create(name="Beta School", slug="beta")
        rows = [("/t/acme/chat/", self.acme, 200, "1.1.1.1", "Chrome", False),
                ("/t/acme/chat/", self.acme, 200, "1.1.1.1", "Chrome", False),
                ("/t/beta/chat/", self.beta, 500, "2.2.2.2", "Safari", False),
                ("/", None, 200, "3.3.3.3", "Googlebot", True)]
        for path, tenant, status, ip, browser, bot in rows:
            RequestLog.objects.create(method="GET", scheme="https", path=path, status=status, duration_ms=40,
                                      area="workspace" if tenant else "site", tenant=tenant, ip=ip,
                                      visitor=f"v-{ip}", browser=browser, os="Windows", device="bot" if bot else "desktop",
                                      is_bot=bot)

    def test_whole_platform_view(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        totals = page.context["data"]["totals"]
        self.assertEqual((totals["requests"], totals["visitors"], totals["ips"], totals["server_errors"]), (4, 3, 3, 1))
        self.assertEqual(totals["https_pct"], 100.0)
        for text in ("Traffic: whole platform", "By workspace", "Acme", "Beta School", "Platform (no workspace)",
                     "Googlebot", "/t/acme/chat/", "1.1.1.1"):
            self.assertContains(page, text)
        self.assertContains(page, f"?tenant={self.acme.pk}&amp;period=7d")  # drill down

    def test_drill_down_to_one_workspace_and_humans_only(self):
        page = self.client.get(self.url, {"tenant": self.acme.pk, "period": "24h"})
        self.assertEqual(page.context["data"]["totals"]["requests"], 2)
        self.assertContains(page, "Traffic: Acme")
        self.assertNotContains(page, "By workspace")
        self.assertNotContains(page, "2.2.2.2")
        self.assertContains(page, f"?tenant__id__exact={self.acme.pk}")  # browse just these
        platform = self.client.get(self.url, {"tenant": "platform"})
        self.assertEqual(platform.context["data"]["totals"]["requests"], 1)
        humans = self.client.get(self.url, {"audience": "humans"})
        self.assertEqual(humans.context["data"]["totals"]["requests"], 3)
        self.assertEqual(len(self.client.get(self.url, {"period": "24h"}).context["data"]["series"]), 24)

    def test_request_list_filters_work_from_dashboard_links(self):
        resp = self.client.get("/admin/traffic/requestlog/", {"tenant__id__exact": self.beta.pk})
        self.assertContains(resp, "/t/beta/chat/")
        self.assertEqual(self.client.get("/admin/traffic/requestlog/", {"ip__exact": "1.1.1.1"}).status_code, 200)
        self.assertEqual(self.client.get("/admin/traffic/requestlog/", {"status_class": "5"}).status_code, 200)

    def test_settings_section_and_staff_only(self):
        self.assertContains(self.client.get("/admin/siteconfig/platformsettings/1/change/") if
                            PlatformSettings.objects.exists() else
                            self.client.get(self.client.get("/admin/siteconfig/platformsettings/").url), "Traffic")
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)  # to the admin login


class ClientIPTests(TestCase):
    """The visitor's IP behind reverse proxies (admin: Proxies in front of the app)."""

    def ip(self, remote="127.0.0.1", forwarded=None, real_ip=None):
        from django.test import RequestFactory

        from widget.ratelimit import client_ip

        meta = {"REMOTE_ADDR": remote}
        if forwarded is not None:
            meta["HTTP_X_FORWARDED_FOR"] = forwarded
        if real_ip is not None:
            meta["HTTP_X_REAL_IP"] = real_ip
        return client_ip(RequestFactory().get("/", **meta))

    def test_behind_nginx_the_visitor_is_the_last_entry(self):
        self.assertEqual(self.ip(forwarded="203.0.113.7"), "203.0.113.7")
        # A visitor's own fake header comes first; nginx appends the real address last.
        self.assertEqual(self.ip(forwarded="6.6.6.6, 203.0.113.7"), "203.0.113.7")
        self.assertEqual(self.ip(forwarded="[2001:db8::5]:443"), "2001:db8::5")
        self.assertEqual(self.ip(forwarded="203.0.113.7:51234"), "203.0.113.7")
        self.assertEqual(self.ip(real_ip="203.0.113.9"), "203.0.113.9")  # nginx with X-Real-IP only
        self.assertEqual(self.ip(forwarded="not-an-ip"), "127.0.0.1")
        self.assertEqual(self.ip(), "127.0.0.1")  # no header: the proxy itself (or local use)

    def test_two_proxies_and_none(self):
        settings_row(trusted_proxies=2)  # e.g. Cloudflare, then nginx
        self.assertEqual(self.ip(forwarded="6.6.6.6, 203.0.113.7, 172.70.1.1"), "203.0.113.7")
        settings_row(trusted_proxies=0)  # visitors reach the app directly
        self.assertEqual(self.ip(forwarded="203.0.113.7"), "127.0.0.1")

    def test_header_ignored_when_not_from_a_proxy(self):
        # Someone reaching the app directly can't choose their IP with the header.
        self.assertEqual(self.ip(remote="8.8.4.4", forwarded="6.6.6.6"), "8.8.4.4")

    def test_recorded_in_traffic(self):
        self.client.get("/use-cases/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.7")
        self.assertEqual(RequestLog.objects.get(path="/use-cases/").ip, "203.0.113.7")
