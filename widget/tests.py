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
from rag.models import FAQ, Chunk, Document
from tenants.models import Tenant

from . import moodle
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


async def fake_embed(texts, purpose="document"):
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


@override_settings(RAG_INLINE_PROCESSING=True, LLM_PROVIDERS=[], LLM_ENABLED_MODELS=[], LLM_EMBED_MODEL="test/embed",
                   RAG_QUERY_PREFIX="", LLM_CONFIG_CACHE_SECONDS=0, RAG_NO_ANSWER_MESSAGE="NOT IN DOCS",
                   CHAT_TOKEN_ALLOWANCE=0)
@mock.patch("llm.embed", fake_embed)
class MoodleTests(TestCase):
    """Moodle plugin: signed students, the shared code and the passage copy."""

    def setUp(self):
        llm.invalidate()
        cache.clear()
        self.t = Tenant.objects.create(name="Acme", slug="acme")
        self.w = Widget.objects.create(tenant=self.t, enabled=True, allowed_domains="moodle.acme.com",
                                       moodle_enabled=True)
        FAQ.objects.create(tenant=self.t, question="What are your opening hours?", answer="9 to 5, Monday to Friday.")
        self.code = self.w.moodle_shared_code
        self.base = f"/w/{self.w.public_key}/"

    def post(self, path, visitor=V1, **body):
        return self.client.post(self.base + path, json.dumps({"visitor_id": visitor, **body}),
                                content_type="application/json")

    def ask(self, **body):
        resp = self.post("api/send/", message="What are your opening hours?", **body)
        async_to_sync(_read)(resp)
        return Conversation.objects.latest("pk")

    def test_shared_code_generated_when_turned_on_and_encrypted(self):
        self.assertEqual(len(self.code), 43)
        self.assertNotIn(self.code, self.w.moodle_shared_code_encrypted)
        off = Widget.objects.create(tenant=Tenant.objects.create(name="B", slug="b"))
        self.assertEqual(off.moodle_shared_code, "")

    def test_token_verification(self):
        token = moodle.sign_user(self.code, 42, "Ada Lovelace")
        self.assertEqual(moodle.verify_user(self.code, token),
                         {"id": "42", "name": "Ada Lovelace", "course": None, "activity": None})
        on_page = moodle.verify_user(self.code, moodle.sign_user(self.code, 42, "Ada", course=7, activity=3))
        self.assertEqual((on_page["course"], on_page["activity"]), (7, 3))
        self.assertIsNone(moodle.verify_user(self.code, moodle.sign_user(self.code, 42, "Ada", activity=3))["activity"])
        self.assertIsNone(moodle.verify_user(self.code, moodle.sign_user(self.code, 42, "Ada", course=7, activity=-3)))
        with_course = moodle.verify_user(self.code, moodle.sign_user(self.code, 42, "Ada", course=7))
        self.assertEqual(with_course["course"], 7)
        self.assertIsNone(moodle.verify_user(self.code, moodle.sign_user(self.code, 42, "Ada", course="x")))
        self.assertIsNone(moodle.verify_user(self.code, moodle.sign_user(self.code, 42, "Ada", course=-1)))
        payload, sig = token.split(".")
        self.assertIsNone(moodle.verify_user("other-code", token))
        self.assertIsNone(moodle.verify_user(self.code, payload + "." + "0" * 64))
        forged = moodle.sign_user("other-code", 42, "Ada").split(".")[0] + "." + sig  # payload swapped
        self.assertIsNone(moodle.verify_user(self.code, forged))
        self.assertIsNone(moodle.verify_user(self.code, moodle.sign_user(self.code, 42, "Ada", ttl=-1)))  # expired
        for junk in (None, "", "abc", "a.b.c", 5):
            self.assertIsNone(moodle.verify_user(self.code, junk))

    def test_signed_student_keeps_conversations_across_devices(self):
        token = moodle.sign_user(self.code, 42, "Ada")
        conv = self.ask(user_token=token)
        self.assertEqual(conv.visitor_id, "moodle-user-00000042")
        # Another browser (different visitor ID), same student: latest conversation comes back.
        hist = self.post("api/history/", visitor=V2, user_token=token, conversation_id=None)
        self.assertEqual(hist.json()["conversation_id"], conv.pk)
        # Without the token that browser is just a guest.
        self.assertEqual(self.post("api/history/", visitor=V2, conversation_id=conv.pk).status_code, 404)

    def test_bad_token_or_integration_off_means_guest(self):
        self.assertEqual(self.ask(user_token="forged.token").visitor_id, V1)
        Widget.objects.filter(pk=self.w.pk).update(moodle_enabled=False)
        self.assertEqual(self.ask(user_token=moodle.sign_user(self.code, 42, "Ada")).visitor_id, V1)

    def test_guests_cannot_claim_a_student_visitor_id(self):
        self.assertEqual(self.post("api/history/", visitor="moodle-user-00000042").status_code, 400)

    def test_passage_copy_requires_shared_code_and_mirror_storage(self):
        doc = Document.objects.create(tenant=self.t, name="handbook.md", extension="md", size=1, sha256="abc",
                                      status=Document.Status.READY, chunk_count=2)
        for i, text in enumerate(["Late work loses 10% a day.", "Extensions: email your tutor."]):
            Chunk.objects.create(tenant=self.t, document=doc, index=i, section="Policies", text=text,
                                 embedding=Chunk.pack([1.0, 0.0, 0.0]), dimensions=3)
        Document.objects.create(tenant=self.t, name="pending.md", extension="md", size=1, sha256="p")  # not ready
        docs_url, auth = self.base + "moodle/documents/", {"HTTP_AUTHORIZATION": f"Bearer {self.code}"}

        self.assertEqual(self.client.get(docs_url).status_code, 403)
        self.assertEqual(self.client.get(docs_url, HTTP_AUTHORIZATION="Bearer wrong").status_code, 403)
        self.assertEqual(self.client.get(docs_url, **auth).status_code, 409)  # storage is "saaschat only"

        Widget.objects.filter(pk=self.w.pk).update(chunk_storage=Widget.ChunkStorage.MIRROR)
        listing = self.client.get(docs_url, **auth).json()["documents"]
        self.assertEqual([d["name"] for d in listing], ["handbook.md"])
        chunks = self.client.get(f"{docs_url}{doc.pk}/chunks/", **auth).json()["chunks"]
        self.assertEqual([c["text"] for c in chunks], ["Late work loses 10% a day.", "Extensions: email your tutor."])
        self.assertEqual(chunks[0]["section"], "Policies")

        other = Tenant.objects.create(name="Other", slug="other")
        foreign = Document.objects.create(tenant=other, name="x.md", extension="md", size=1, sha256="x",
                                          status=Document.Status.READY)
        self.assertEqual(self.client.get(f"{docs_url}{foreign.pk}/chunks/", **auth).status_code, 404)

        Widget.objects.filter(pk=self.w.pk).update(moodle_enabled=False)
        self.assertEqual(self.client.get(docs_url, **auth).status_code, 404)

    def test_greeting_default_has_name_placeholder(self):
        self.assertContains(self.client.get(self.base), "Hi {name}!")


@override_settings(RAG_INLINE_PROCESSING=True, LLM_PROVIDERS=[], LLM_ENABLED_MODELS=[], LLM_EMBED_MODEL="test/embed",
                   RAG_QUERY_PREFIX="", LLM_CONFIG_CACHE_SECONDS=0, RAG_NO_ANSWER_MESSAGE="NOT IN DOCS",
                   CHAT_TOKEN_ALLOWANCE=0)
@mock.patch("llm.embed", fake_embed)
class MoodleCourseTests(TestCase):
    """Course content sent by the plugin, and answers scoped to the student's course."""

    def setUp(self):
        llm.invalidate()
        cache.clear()
        self.t = Tenant.objects.create(name="Acme", slug="acme")
        self.w = Widget.objects.create(tenant=self.t, enabled=True, allowed_domains="moodle.acme.com",
                                       moodle_enabled=True, moodle_courses=True)
        FAQ.objects.create(tenant=self.t, question="What are your opening hours?", answer="9 to 5, Monday to Friday.")
        self.code = self.w.moodle_shared_code
        self.base = f"/w/{self.w.public_key}/moodle/courses/"
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.code}"}

    def put_item(self, course, key, text, version="v1", name="Item", filename="item.md", **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile

        data = {"key": key, "version": version, "name": name,
                "file": SimpleUploadedFile(filename, text.encode(), content_type="text/markdown")}
        return self.client.post(f"{self.base}{course}/items/", data, **{**self.auth, **extra})

    def search(self, question, course=None):
        from rag import retrieval

        return [p.document for p in async_to_sync(retrieval.search)(self.t, [question], course=course, min_score=0.5)]

    def test_requires_shared_code_and_course_content_on(self):
        self.assertEqual(self.client.get(self.base).status_code, 403)
        self.assertEqual(self.put_item(7, "cm:1", "x", HTTP_AUTHORIZATION="Bearer wrong").status_code, 403)
        Widget.objects.filter(pk=self.w.pk).update(moodle_courses=False)
        self.assertEqual(self.client.get(self.base, **self.auth).status_code, 409)
        self.assertEqual(self.put_item(7, "cm:1", "x").status_code, 409)
        Widget.objects.filter(pk=self.w.pk).update(moodle_enabled=False)
        self.assertEqual(self.client.get(self.base, **self.auth).status_code, 404)

    def test_items_added_listed_replaced_and_removed(self):
        resp = self.put_item(7, "cm:1", "# Week 1\nLate work loses 10% a day.", name="Week 1 (Page)")
        self.assertEqual(resp.status_code, 202)
        self.put_item(7, "cm:2", "Quiz 1 closes on Friday.", name="Quiz 1")
        self.put_item(8, "cm:1", "Another course.", name="Other")
        self.assertEqual(self.client.get(self.base, **self.auth).json()["courses"], [7, 8])
        listing = self.client.get(f"{self.base}7/items/", **self.auth).json()
        self.assertEqual(listing["items"], {"cm:1": "v1", "cm:2": "v1"})
        self.assertIn("md", listing["extensions"])

        doc = Document.objects.get(moodle_course_id=7, moodle_key="cm:1")
        self.assertEqual((doc.status, doc.name), (Document.Status.READY, "Week 1 (Page)"))

        self.put_item(7, "cm:1", "Late work loses 5% a day.", version="v2", name="Week 1 (Page)")
        self.assertEqual(Document.objects.filter(moodle_course_id=7, moodle_key="cm:1").count(), 1)
        self.assertIn("5%", Chunk.objects.get(document__moodle_key="cm:1", document__moodle_course_id=7).text)

        resp = self.client.post(f"{self.base}7/items/delete/", json.dumps({"keys": ["cm:2", "cm:99"]}),
                                content_type="application/json", **self.auth)
        self.assertEqual(resp.json(), {"deleted": 1})
        self.assertEqual(self.client.post(f"{self.base}8/delete/", **self.auth).json(), {"deleted": 1})
        self.assertEqual(self.client.get(self.base, **self.auth).json()["courses"], [7])

    def test_bad_items_rejected(self):
        self.assertEqual(self.put_item(7, "Bad Key!", "x").status_code, 400)
        self.assertEqual(self.put_item(7, "cm:1", "x", filename="slides.exe").status_code, 400)
        self.assertEqual(self.put_item(7, "cm:1", "").status_code, 400)
        self.assertFalse(Document.objects.exists())

    def test_course_content_kept_apart_from_workspace_documents(self):
        self.put_item(7, "cm:1", "Late work loses 10% a day.", name="Course 7 rules")
        self.put_item(8, "cm:1", "Late work loses 50% a day.", name="Course 8 rules")
        self.assertEqual(self.search("late work", course=7), ["Course 7 rules"])
        self.assertEqual(self.search("late work", course=8), ["Course 8 rules"])
        self.assertEqual(self.search("late work"), [])  # the workspace chat never sees course content
        # Not in the workspace's document list, limit or passage copy.
        self.assertNotContains(self.client.get("/t/acme/admin/"), "Course 7 rules")
        Widget.objects.filter(pk=self.w.pk).update(chunk_storage=Widget.ChunkStorage.MIRROR)
        copy = self.client.get(f"/w/{self.w.public_key}/moodle/documents/", **self.auth).json()
        self.assertEqual(copy["documents"], [])

    def test_same_file_in_two_courses_and_in_the_workspace(self):
        from rag import services

        async_to_sync(services.ingest)(self.t, "rules.txt", b"Late work loses 10% a day.")
        self.assertEqual(self.put_item(7, "file:1", "Late work loses 10% a day.").status_code, 202)
        self.assertEqual(self.put_item(8, "file:1", "Late work loses 10% a day.").status_code, 202)
        self.assertEqual(Document.objects.count(), 3)

    def ask(self, token, question="What are your opening hours?"):
        resp = self.client.post(f"/w/{self.w.public_key}/api/send/", json.dumps(
            {"message": question, "visitor_id": V1, "user_token": token}), content_type="application/json")
        events = [json.loads(l) for l in async_to_sync(_read)(resp).decode().splitlines()]
        return Conversation.objects.latest("pk"), events

    def test_students_on_a_course_page_get_that_course_only(self):
        # Outside a course: the workspace's FAQ answers.
        conv, events = self.ask(moodle.sign_user(self.code, 42, "Ada"))
        self.assertEqual(conv.visitor_id, "moodle-user-00000042")
        self.assertTrue(events[-1]["faq"])
        # On course 7's pages: its own conversations, and no workspace FAQs or documents.
        conv, events = self.ask(moodle.sign_user(self.code, 42, "Ada", course=7))
        self.assertEqual(conv.visitor_id, "moodle-user-00000042-c7")
        self.assertFalse(events[-1]["faq"])
        self.assertEqual(conv.messages.get(role="assistant").outcome, Message.Outcome.UNAVAILABLE)

    @override_settings(RAG_REWRITE_QUERIES=True)
    def test_this_module_means_the_page_the_student_is_viewing(self):
        calls = []

        async def fake_stream(model, messages, temperature=None, max_tokens=None, **kwargs):
            calls.append(messages)
            yield {"type": "content", "text": "search words" if "search query" in messages[0]["content"] else "OK"}
            yield {"type": "done", "prompt_tokens": 1, "completion_tokens": 1}

        self.put_item(7, "overview", "Modules 1 to 8.", name="Prompting: course overview and schedule")
        self.put_item(7, "cm:3", "The RTCF framework: role, task, context, format.", name="Module 3 (Page)")
        self.put_item(7, "cm:8", "Fix a prompt instead of restarting it.", name="Module 8 (Page)")
        self.put_item(7, "cm:9", "More about module 8.", name="Module 9 (Page)")
        with mock.patch("llm.stream_chat", fake_stream):
            _, events = self.ask(moodle.sign_user(self.code, 42, "Ada", course=7, activity=3),
                                 "What is this module about?")
        sources = next(e for e in events if e["type"] == "sources")["sources"]
        self.assertEqual(sources[0]["document"], "Module 3 (Page)")  # the page comes first
        self.assertEqual(sum(s["document"] == "Module 3 (Page)" for s in sources), 1)  # and only once
        question = calls[-1][-1]["content"]
        self.assertIn('The student is in the course "Prompting".', question)
        self.assertIn('They are viewing "Module 3 (Page)"', question)
        self.assertIn("Today is ", question)
        rewrite = next(c for c in calls if "search query" in c[0]["content"])
        self.assertIn("Page the user is viewing: Module 3 (Page)", rewrite[1]["content"])

    def test_course_ignored_when_course_content_off(self):
        Widget.objects.filter(pk=self.w.pk).update(moodle_courses=False)
        conv, events = self.ask(moodle.sign_user(self.code, 42, "Ada", course=7))
        self.assertEqual(conv.visitor_id, "moodle-user-00000042")
        self.assertTrue(events[-1]["faq"])
