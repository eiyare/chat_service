"""Website widget: settings, embedding restrictions, visitor isolation, rate limits."""
import json
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from saas_chat.testing import TestCase  # signed in by default

import llm
from chat.models import Conversation, Message
from rag.models import FAQ
from tenants.models import Tenant

from .models import Widget

V1 = "visitor-aaaaaaaaaaaaaaaa"
V2 = "visitor-bbbbbbbbbbbbbbbb"


async def _read(resp):
    return b"".join([chunk async for chunk in resp.streaming_content])


class WidgetModelTests(TestCase):
    def setUp(self):
        self.t = Tenant.objects.create(name="Acme", slug="acme")

    def test_domains_normalised_and_validated(self):
        w = Widget(tenant=self.t, allowed_domains="Example.com/\nhttps://www.example.com/path\n*.example.org\nlocalhost:8000\n")
        w.full_clean()
        self.assertEqual(w.domains(), ["example.com", "https://www.example.com", "*.example.org", "localhost:8000"])
        self.assertEqual(w.frame_ancestors(), "'self' example.com https://www.example.com *.example.org localhost:8000")
        for bad in ("*", "exa mple.com", "javascript:alert(1)", "<script>"):
            with self.assertRaises(ValidationError):
                Widget(tenant=self.t, allowed_domains=bad).full_clean()

    def test_enabling_requires_a_domain(self):
        with self.assertRaisesMessage(ValidationError, "Add at least one website"):
            Widget(tenant=self.t, enabled=True).full_clean()

    def test_text_color_contrast(self):
        self.assertEqual(Widget(accent_color="#2a78d6").text_color, "#ffffff")
        self.assertEqual(Widget(accent_color="#fab219").text_color, "#0b0b0b")
        with self.assertRaises(ValidationError):
            Widget(tenant=self.t, accent_color="red").full_clean()


class EmbedTabTests(TestCase):
    def setUp(self):
        self.t = Tenant.objects.create(name="Acme", slug="acme")

    def post(self, **fields):
        data = {"title": "Ask Acme", "greeting": "Hi!", "accent_color": "#2a78d6", "position": "right",
                "allowed_domains": "acme.com", "rate_limit_per_minute": "10", **fields}
        return self.client.post("/t/acme/admin/embed/", data, follow=True)

    def test_tab_shows_snippet_and_saves_settings(self):
        page = self.client.get("/t/acme/admin/embed/")
        widget = Widget.objects.get(tenant=self.t)
        self.assertContains(page, 'aria-current="page">Embed')
        self.assertContains(page, f"data-key=&quot;{widget.public_key}&quot;")  # inside the textarea
        self.assertContains(page, "/static/widget/embed.js")
        self.assertContains(page, "The widget is off")
        self.assertNotContains(page, '<script src="http://testserver/static/widget/embed.js"')  # no preview while off

        resp = self.post(enabled="on")
        self.assertContains(resp, "Widget settings saved.")
        self.assertContains(resp, "Live")
        self.assertContains(resp, '<script src="http://testserver/static/widget/embed.js"')  # live preview
        widget.refresh_from_db()
        self.assertEqual((widget.enabled, widget.title, widget.domains()), (True, "Ask Acme", ["acme.com"]))

    def test_validation_errors_shown(self):
        resp = self.post(enabled="on", allowed_domains="", accent_color="blue")
        self.assertContains(resp, "Add at least one website")
        self.assertContains(resp, "Use a hex colour")
        self.assertFalse(Widget.objects.get(tenant=self.t).enabled)

    def test_regenerate_key(self):
        self.client.get("/t/acme/admin/embed/")
        old = Widget.objects.get(tenant=self.t).public_key
        resp = self.client.post("/t/acme/admin/embed/", {"action": "regenerate"}, follow=True)
        self.assertContains(resp, "New key generated")
        self.assertNotEqual(Widget.objects.get(tenant=self.t).public_key, old)


async def fake_embed(texts):
    return [[1.0, 0.0] for _ in texts]


@override_settings(LLM_CHAT_MODELS=["p/chat"], LLM_DEFAULT_CHAT_MODEL="p/chat", LLM_EMBED_MODEL="p/embed",
                   RAG_QUERY_PREFIX="", LLM_CONFIG_CACHE_SECONDS=0, RAG_NO_ANSWER_MESSAGE="NOT IN DOCS",
                   CHAT_TOKEN_ALLOWANCE=0)
@mock.patch("llm.embed", fake_embed)
class WidgetEndpointTests(TestCase):
    def setUp(self):
        llm.invalidate()
        cache.clear()
        self.t = Tenant.objects.create(name="Acme", slug="acme")
        self.w = Widget.objects.create(tenant=self.t, enabled=True, allowed_domains="acme.com", rate_limit_per_minute=5)
        FAQ.objects.create(tenant=self.t, question="What are your opening hours?", answer="9 to 5, Monday to Friday.")
        self.base = f"/w/{self.w.public_key}/"

    def send(self, message="What are your opening hours?", visitor=V1, conversation_id=None, **extra):
        payload = json.dumps({"message": message, "visitor_id": visitor, "conversation_id": conversation_id})
        resp = self.client.post(self.base + "api/send/", payload, content_type="application/json", **extra)
        if resp.status_code != 200:
            return resp, None
        events = [json.loads(l) for l in async_to_sync(_read)(resp).decode().splitlines()]
        return resp, events

    def test_page_only_frameable_by_allowed_sites(self):
        resp = self.client.get(self.base)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Security-Policy"], "frame-ancestors 'self' acme.com")
        self.assertNotIn("X-Frame-Options", resp)
        self.assertContains(resp, "Answers come from Acme")

    def test_disabled_unknown_or_inactive_is_404(self):
        self.assertEqual(self.client.get("/w/nope/").status_code, 404)
        Widget.objects.filter(pk=self.w.pk).update(enabled=False)
        self.assertEqual(self.client.get(self.base).status_code, 404)
        self.assertEqual(self.send()[0].status_code, 404)
        Widget.objects.filter(pk=self.w.pk).update(enabled=True)
        Tenant.objects.filter(pk=self.t.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.base).status_code, 404)

    def test_public_config(self):
        resp = self.client.get(self.base + "config.json")
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")
        self.assertEqual(resp.json(), {"enabled": True, "title": "Ask us", "color": "#2a78d6",
                                       "textColor": "#ffffff", "position": "right"})
        self.assertEqual(self.client.get("/w/nope/config.json").json(), {"enabled": False})

    def test_faq_answer_through_widget_creates_visitor_conversation(self):
        resp, events = self.send()
        self.assertEqual(events[2]["text"], "9 to 5, Monday to Friday.")
        self.assertTrue(events[-1]["faq"])
        conv = Conversation.objects.get()
        self.assertEqual((conv.source, conv.visitor_id, conv.tenant_id), ("widget", V1, self.t.pk))
        self.assertEqual(Message.objects.filter(role="assistant").get().outcome, Message.Outcome.FAQ)

    def test_visitors_are_isolated(self):
        _, events = self.send()
        cid = events[0]["conversation_id"]
        self.assertEqual(self.send(visitor=V1, conversation_id=cid)[0].status_code, 200)
        self.assertEqual(self.send(visitor=V2, conversation_id=cid)[0].status_code, 404)

        hist = self.client.post(self.base + "api/history/", json.dumps({"visitor_id": V1, "conversation_id": cid}),
                                content_type="application/json")
        self.assertEqual([m["role"] for m in hist.json()["messages"]], ["user", "assistant", "user", "assistant"])
        other = self.client.post(self.base + "api/history/", json.dumps({"visitor_id": V2, "conversation_id": cid}),
                                 content_type="application/json")
        self.assertEqual(other.status_code, 404)

        # Widget conversations stay out of the workspace chat.
        self.assertNotContains(self.client.get("/t/acme/chat/"), "What are your opening hours?")
        self.assertEqual(self.client.get(f"/t/acme/chat/c/{cid}/").status_code, 404)

    def test_request_checks(self):
        resp = self.client.post(self.base + "api/send/", "message=hi", content_type="text/plain")
        self.assertEqual(resp.status_code, 415)
        resp, _ = self.send(HTTP_ORIGIN="https://evil.example")
        self.assertEqual(resp.status_code, 403)
        resp, _ = self.send(visitor="short")
        self.assertEqual(resp.status_code, 400)
        resp, _ = self.send(HTTP_ORIGIN="http://testserver")  # our own iframe
        self.assertEqual(resp.status_code, 200)

    def test_rate_limit_per_visitor_ip(self):
        codes = [self.send()[0].status_code for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])
        self.assertEqual(self.send(REMOTE_ADDR="10.0.0.9")[0].status_code, 200)  # another IP

    def test_widget_cannot_choose_model(self):
        payload = json.dumps({"message": "What are your opening hours?", "visitor_id": V1,
                              "model": "other-provider/expensive"})
        resp = self.client.post(self.base + "api/send/", payload, content_type="application/json")
        self.assertEqual(resp.status_code, 200)

    def test_performance_counts_widget_questions(self):
        self.send()
        self.assertContains(self.client.get("/t/acme/admin/performance/"), "1 via website widget")
