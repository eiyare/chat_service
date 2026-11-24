"""Connected apps: the API, signed users, content scopes, tools and the workspace admin page."""
import hashlib
import hmac
import json
import time
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import override_settings

import llm
from chat.models import Conversation, Message
from rag.models import Document
from saas_chat.testing import TestCase
from tenants.models import Tenant
from widget.models import Widget
from widget.tests import V1, _read, fake_embed

from . import identity
from .models import Integration
from .tools import ManifestError, validate_manifest

MANIFEST = {"callback": "/saaschat/tools", "tools": [
    {"name": "get_my_orders", "description": "The signed-in customer's recent orders.", "kind": "read"},
    {"name": "cancel_order", "description": "Cancel one of the customer's orders.", "kind": "action",
     "parameters": {"type": "object", "properties": {"order": {"type": "string"}}, "required": ["order"]}},
    {"name": "opening_hours", "description": "Today's opening hours.", "needs_user": False},
]}


@override_settings(RAG_INLINE_PROCESSING=True, LLM_PROVIDERS=[], LLM_ENABLED_MODELS=[], LLM_EMBED_MODEL="test/embed",
                   LLM_CHAT_MODELS=["p/chat"], LLM_DEFAULT_CHAT_MODEL="p/chat", RAG_QUERY_PREFIX="",
                   LLM_CONFIG_CACHE_SECONDS=0, RAG_NO_ANSWER_MESSAGE="NOT IN DOCS", CHAT_TOKEN_ALLOWANCE=0)
@mock.patch("llm.embed", fake_embed)
class AppIntegrationTests(TestCase):
    def setUp(self):
        llm.invalidate()
        cache.clear()
        self.t = Tenant.objects.create(name="Ei Consulting", slug="ei")
        self.w = Widget.objects.create(tenant=self.t, enabled=True, allowed_domains="www.ei.consulting")
        self.app = Integration.objects.create(tenant=self.t, name="Website (Odoo)", site_url="https://www.ei.consulting",
                                              content=True, tools=True)
        self.secret = self.app.secret
        self.api = f"/i/{self.app.key}/"
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.secret}"}

    def token(self, **extra):
        return identity.sign(self.secret, {"iss": self.app.key, "sub": "cust-17", "name": "Ada Obi",
                                           "exp": int(time.time()) + 3600, **extra})

    def push(self, scope, key, text, name="Item"):
        return self.client.post(f"{self.api}scopes/{scope}/items/", json.dumps(
            {"key": key, "name": name, "text": text, "version": "v1"}), content_type="application/json", **self.auth)

    # --- API --------------------------------------------------------------------------------------

    def test_api_needs_the_secret_and_reports_what_is_allowed(self):
        self.client.logout()  # server-to-server: no session
        self.assertEqual(self.client.get(self.api).status_code, 403)
        self.assertEqual(self.client.get(self.api, HTTP_AUTHORIZATION="Bearer wrong").status_code, 403)
        info = self.client.get(self.api, **self.auth).json()
        self.assertEqual((info["workspace"], info["allowed"]["content"], info["allowed"]["share_documents"]),
                         ("Ei Consulting", True, False))
        self.assertEqual(self.client.get(f"{self.api}documents/", **self.auth).status_code, 409)  # not allowed
        Integration.objects.filter(pk=self.app.pk).update(enabled=False)
        self.assertEqual(self.client.get(self.api, **self.auth).status_code, 404)

    def test_manifest_is_validated_and_stored(self):
        resp = self.client.put(f"{self.api}manifest/", json.dumps(MANIFEST), content_type="application/json",
                               **self.auth)
        self.assertEqual(resp.json()["tools"], ["get_my_orders", "cancel_order", "opening_hours"])
        self.app.refresh_from_db()
        self.assertEqual(self.app.callback_url, "https://www.ei.consulting/saaschat/tools")
        for bad in ({"tools": [{"name": "Bad Name", "description": "x"}], "callback": "/t"},
                    {"tools": [{"name": "search_content", "description": "x"}], "callback": "/t"},
                    {"tools": [{"name": "ok_tool"}], "callback": "/t"},
                    {"tools": [{"name": "ok_tool", "description": "x"}]},
                    {"tools": [{"name": "ok_tool", "description": "x"}], "callback": "https://evil.example/x"}):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                validate_manifest(bad)
        resp = self.client.put(f"{self.api}manifest/", "{}x", content_type="application/json", **self.auth)
        self.assertEqual(resp.status_code, 400)

    def test_content_in_scopes_and_shared(self):
        self.assertEqual(self.push("shared", "page:services", "We offer LMS planning and VR in education.",
                                   "Services").status_code, 202)
        self.assertEqual(self.push("client:acme", "contract", "Acme's support plan renews on 1 March.",
                                   "Acme contract").status_code, 202)
        self.assertEqual(self.push("bad scope!", "x", "x").status_code, 400)
        self.assertEqual(self.push("shared", "bad key with spaces", "x").status_code, 400)
        scopes = self.client.get(f"{self.api}scopes/", **self.auth).json()["scopes"]
        self.assertEqual(scopes, [{"scope": "shared", "items": 1}, {"scope": "client:acme", "items": 1}])
        items = self.client.get(f"{self.api}scopes/client:acme/items/", **self.auth).json()["items"]
        self.assertEqual(items, {"contract": "v1"})

        from rag import retrieval

        def search(q, scope=None):
            return [p.document for p in async_to_sync(retrieval.search)(self.t, [q], scope=scope, min_score=0.5)]

        # Shared content adds to the workspace's knowledge; scoped content only within its scope.
        self.assertEqual(search("LMS planning VR education"), ["Services"])
        self.assertEqual(search("Acme support plan renews"), [])
        acme = retrieval.Scope(self.app.pk, "client:acme")
        self.assertEqual(search("Acme support plan renews", acme), ["Acme contract"])
        # Not in the workspace's own document list or limit.
        self.assertNotContains(self.client.get("/t/ei/admin/"), "Acme contract")

        resp = self.client.post(f"{self.api}scopes/client:acme/items/delete/", json.dumps({"keys": ["contract"]}),
                                content_type="application/json", **self.auth)
        self.assertEqual(resp.json(), {"deleted": 1})
        self.assertEqual(self.client.post(f"{self.api}scopes/shared/delete/", **self.auth).json(), {"deleted": 1})
        self.assertFalse(Document.objects.filter(integration=self.app).exists())
        Integration.objects.filter(pk=self.app.pk).update(content=False)
        self.assertEqual(self.push("shared", "x", "x").status_code, 409)

    # --- Signed users -------------------------------------------------------------------------------

    def test_signed_users_from_any_app(self):
        visitor = identity.verify(self.t, self.token(scope="client:acme", page="contract"))
        self.assertEqual((visitor.user_id, visitor.name, visitor.scope, visitor.page),
                         ("cust-17", "Ada Obi", "client:acme", "contract"))
        self.assertTrue(visitor.visitor_id.startswith(f"app.{self.app.pk}.cust-17.s"))
        self.assertEqual(identity.verify(self.t, self.token()).visitor_id, f"app.{self.app.pk}.cust-17")
        for bad in (self.token(exp=1), self.token(sub="a b"), self.token(scope="bad scope!"),
                    identity.sign("wrong", {"iss": self.app.key, "sub": "1", "exp": int(time.time()) + 60}),
                    identity.sign(self.secret, {"iss": "other-key", "sub": "1", "exp": int(time.time()) + 60}),
                    "junk", None):
            with self.subTest(bad=bad):
                self.assertIsNone(identity.verify(self.t, bad))
        other = Tenant.objects.create(name="Other", slug="other")
        self.assertIsNone(identity.verify(other, self.token()))  # another workspace's widget
        Integration.objects.filter(pk=self.app.pk).update(content=False)
        self.assertIsNone(identity.verify(self.t, self.token(scope="client:acme")).scope)  # no content, no scope

    # --- Tools --------------------------------------------------------------------------------------

    def ask(self, question, token=None, stream=None):
        body = {"message": question, "visitor_id": V1}
        if token:
            body["user_token"] = token
        with mock.patch("llm.stream_chat", stream):
            resp = self.client.post(f"/w/{self.w.public_key}/api/send/", json.dumps(body),
                                    content_type="application/json")
            events = [json.loads(line) for line in async_to_sync(_read)(resp).decode().splitlines()]
        return events, Message.objects.filter(role="assistant").last()

    def test_tools_are_called_on_the_app_with_a_signature(self):
        self.client.put(f"{self.api}manifest/", json.dumps(MANIFEST), content_type="application/json", **self.auth)
        calls, offered, posted, definitions = [], [], [], []

        async def stream(model, messages, tools=None, **kwargs):
            calls.append(messages)
            definitions.append(tools or [])
            offered.append(sorted(t["function"]["name"] for t in tools or []))
            if tools and not any(m["role"] == "tool" for m in messages):
                yield {"type": "tool_calls", "calls": [{"id": "c1", "name": "get_my_orders", "arguments": "{}"}]}
                yield {"type": "done", "prompt_tokens": 10, "completion_tokens": 2}
                return
            yield {"type": "content", "text": "Your order SO-1042 shipped on Monday."}
            yield {"type": "done", "prompt_tokens": 20, "completion_tokens": 8}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"orders": [{"number": "SO-1042", "status": "shipped"}]}

        async def post(client, url, content=None, headers=None):
            posted.append({"url": url, "body": content, "headers": headers})
            return FakeResponse()

        token = self.token()
        with mock.patch("httpx.AsyncClient.post", post):
            _, reply = self.ask("Where is my order?", token, stream)
        self.assertEqual(reply.content, "Your order SO-1042 shipped on Monday.")
        self.assertEqual(offered[0], ["cancel_order", "get_my_orders", "opening_hours"])
        call = posted[0]
        self.assertEqual(call["url"], "https://www.ei.consulting/saaschat/tools")
        body = json.loads(call["body"])
        self.assertEqual((body["tool"], body["user_token"]), ("get_my_orders", token))
        stamp = call["headers"]["X-Saaschat-Timestamp"]
        expected = hmac.new(self.secret.encode(), stamp.encode() + b"." + call["body"], hashlib.sha256).hexdigest()
        self.assertEqual(call["headers"]["X-Saaschat-Signature"], f"sha256={expected}")
        self.assertIn("SO-1042", calls[-1][-1]["content"])  # the result went to the model
        cancel = next(t for t in definitions[0] if t["function"]["name"] == "cancel_order")
        self.assertIn("first tell the user exactly what you will do", cancel["function"]["description"])
        tool_step = next(s for s in reply.trace if s["key"] == "tool")
        self.assertEqual((tool_step["label"], tool_step["summary"]), ("Tool: get_my_orders", "1 orders"))
        self.assertNotIn("shipped", json.dumps(tool_step))  # the brain map keeps no customer data
        self.assertEqual(Conversation.objects.latest("pk").visitor_id, f"app.{self.app.pk}.cust-17")

    def test_guests_only_get_tools_meant_for_everyone(self):
        self.client.put(f"{self.api}manifest/", json.dumps(MANIFEST), content_type="application/json", **self.auth)
        offered = []

        async def stream(model, messages, tools=None, **kwargs):
            offered.append(sorted(t["function"]["name"] for t in tools or []))
            yield {"type": "content", "text": "We're open 9 to 5."}
            yield {"type": "done", "prompt_tokens": 1, "completion_tokens": 1}

        self.ask("Are you open today?", None, stream)
        self.assertEqual(offered, [["opening_hours"]])
        Integration.objects.filter(pk=self.app.pk).update(tools=False)
        offered.clear()
        _, reply = self.ask("Are you open today?", None, stream)
        self.assertEqual(offered, [])  # no tools, no passages: the model isn't called
        self.assertEqual(reply.outcome, Message.Outcome.UNAVAILABLE)


class WorkspaceAdminTests(TestCase):
    def setUp(self):
        self.t = Tenant.objects.create(name="Ei Consulting", slug="ei")
        Widget.objects.create(tenant=self.t)

    def test_connect_configure_regenerate_and_disconnect(self):
        page = self.client.get("/t/ei/admin/integrations/")
        self.assertContains(page, "Connect an app")
        resp = self.client.post("/t/ei/admin/integrations/", {"name": "Website (Odoo)", "kind": "app",
                                                              "site_url": "https://www.ei.consulting",
                                                              "enabled": "on", "signed_users": "on", "content": "on"})
        app = Integration.objects.get(tenant=self.t)
        self.assertRedirects(resp, f"/t/ei/admin/integrations/{app.pk}/", fetch_redirect_response=False)
        detail = self.client.get(f"/t/ei/admin/integrations/{app.pk}/")
        for text in (app.key, app.secret, f"/i/{app.key}/", "Tools the app offers"):
            self.assertContains(detail, text)
        old = app.secret
        self.client.post(f"/t/ei/admin/integrations/{app.pk}/", {"action": "regenerate"})
        app.refresh_from_db()
        self.assertNotEqual(app.secret, old)
        resp = self.client.post(f"/t/ei/admin/integrations/{app.pk}/", {"action": "save", "name": "Website",
                                                                        "tools": "on", "enabled": "on"})
        self.assertContains(resp, "enter it")  # tools need the app's address
        Document.objects.create(tenant=self.t, name="p", extension="md", size=1, sha256="x", integration=app)
        self.client.post(f"/t/ei/admin/integrations/{app.pk}/", {"action": "delete", "confirm": "nope"})
        self.assertTrue(Integration.objects.filter(pk=app.pk).exists())
        self.client.post(f"/t/ei/admin/integrations/{app.pk}/", {"action": "delete", "confirm": "Website (Odoo)"})
        self.assertFalse(Integration.objects.filter(pk=app.pk).exists())
        self.assertFalse(Document.objects.filter(sha256="x").exists())  # its content went too

    def test_one_moodle_per_workspace_and_other_workspaces_hidden(self):
        Integration.objects.create(tenant=self.t, kind="moodle", name="Moodle")
        resp = self.client.post("/t/ei/admin/integrations/", {"name": "Moodle 2", "kind": "moodle", "enabled": "on"})
        self.assertContains(resp, "already has a Moodle connection")
        other = Tenant.objects.create(name="Other", slug="other")
        theirs = Integration.objects.create(tenant=other, name="Theirs")
        self.assertEqual(self.client.get(f"/t/ei/admin/integrations/{theirs.pk}/").status_code, 404)
