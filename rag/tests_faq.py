"""Workspace FAQs: direct answers without the model, safe matching, context, admin."""
import json
from unittest import mock

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from saas_chat.testing import TestCase  # signed in by default

import llm
from chat.models import Message
from rag import faq as faq_lib
from rag.models import FAQ, Chunk, Document, current_embed_signature
from tenants.models import Tenant

# Toy embedding space: questions about the same topic land on the same axis.
TOPICS = {"ceo": [1, 0, 0, 0], "cfo": [0.95, 0.31, 0, 0], "leave": [0, 0, 1, 0], "hours": [0, 0, 0, 1]}
EMBED_CALLS = []


def vec(text):
    t = text.lower()
    for word in ("cfo", "ceo", "leave", "vacation", "hours", "open"):
        if word in t:
            key = {"vacation": "leave", "open": "hours"}.get(word, word)
            return TOPICS[key]
    return [0.1, 0.1, 0.1, 0.1]


async def fake_embed(texts):
    EMBED_CALLS.append(list(texts))
    return [vec(t) for t in texts]


STREAM_CALLS = []


async def fake_stream(model, messages, temperature=None, max_tokens=None):
    STREAM_CALLS.append(messages)
    yield {"type": "content", "text": "From the model."}
    yield {"type": "done", "prompt_tokens": 50, "completion_tokens": 4, "duration_ms": 5}


class WordingTests(SimpleTestCase):
    def test_normalise_and_negation(self):
        self.assertEqual(faq_lib.normalise("  What are your HOURS?! "), "what are your hours")
        self.assertTrue(faq_lib.negated("Isn’t leave paid?"))
        self.assertFalse(faq_lib.negated("Is leave paid?"))

    def test_wording_rule(self):
        ok = faq_lib._wording_matches
        self.assertTrue(ok("how many leave days do i have", "How many days of annual leave do I get?"))
        self.assertFalse(ok("Who is the CFO of Ei Consulting?", "Who is the CEO of Ei Consulting?"))
        self.assertFalse(ok("How many days of sick leave do I get?", "How many days of annual leave do I get?"))
        self.assertFalse(ok("What are your weekend hours?", "What are your office hours?"))
        self.assertFalse(ok("Is leave not paid?", "Is leave paid?"))
        self.assertFalse(ok("leave", "How many days of annual leave do I get?"))  # covers < half


SETTINGS = dict(
    LLM_CHAT_MODELS=["p/chat"], LLM_DEFAULT_CHAT_MODEL="p/chat", LLM_EMBED_MODEL="p/embed",
    RAG_QUERY_PREFIX="", RAG_DOCUMENT_PREFIX="", RAG_MIN_SCORE=0.5, RAG_TOP_K=3,
    RAG_FAQ_MIN_SCORE=0.8, RAG_FAQ_CONTEXT_MIN_SCORE=0.75, RAG_FAQ_CONTEXT_MAX=2,
    RAG_NO_ANSWER_MESSAGE="NOT IN DOCS", RAG_EMPTY_KB_MESSAGE="NO DOCS", LLM_CONFIG_CACHE_SECONDS=0,
)


@override_settings(**SETTINGS)
@mock.patch("llm.embed", fake_embed)
@mock.patch("llm.stream_chat", fake_stream)
class ChatFAQTests(TestCase):
    def setUp(self):
        llm.invalidate()
        EMBED_CALLS.clear()
        STREAM_CALLS.clear()
        self.acme = Tenant.objects.create(name="Acme", slug="acme")
        self.other = Tenant.objects.create(name="Other", slug="other")
        self.ceo = FAQ.objects.create(tenant=self.acme, question="Who is the CEO of Ei Consulting?",
                                      answer="**Etinosa Iyare** is the CEO.")
        self.leave = FAQ.objects.create(tenant=self.acme, question="How many days of annual leave do I get?",
                                        alternate_questions="How many vacation days do I get?\n",
                                        answer="25 days a year.")

    async def ask(self, message, slug="acme"):
        resp = await self.async_client.post(f"/t/{slug}/chat/api/send/", data=json.dumps(
            {"message": message, "model": "p/chat"}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        events = [json.loads(l) for l in b"".join([c async for c in resp.streaming_content]).decode().splitlines()]
        reply = await Message.objects.filter(role="assistant").alast()
        return events, reply

    async def test_exact_question_answers_without_embedding_or_model(self):
        events, reply = await self.ask("who is the CEO of ei consulting")
        self.assertEqual(events[2]["text"], "**Etinosa Iyare** is the CEO.")
        self.assertTrue(events[-1]["faq"])
        self.assertEqual(events[1]["sources"][0]["document"], "FAQ")
        self.assertEqual((EMBED_CALLS, STREAM_CALLS), ([], []))
        self.assertEqual((reply.outcome, reply.model, reply.prompt_tokens), (Message.Outcome.FAQ, "", None))
        faq = await FAQ.objects.aget(pk=self.ceo.pk)
        self.assertEqual(faq.hit_count, 1)
        self.assertIsNotNone(faq.last_hit_at)

    async def test_reworded_question_matches_a_phrasing(self):
        events, reply = await self.ask("how many vacation days do i get")  # an "other phrasing", reworded
        self.assertEqual(reply.outcome, Message.Outcome.FAQ)
        self.assertEqual(events[2]["text"], "25 days a year.")
        self.assertEqual(STREAM_CALLS, [])

    async def test_similar_but_different_question_is_not_answered_from_faq(self):
        # CFO scores ~0.95 against the CEO FAQ, but "cfo" isn't in its wording.
        events, reply = await self.ask("Who is the CFO of Ei Consulting?")
        self.assertNotEqual(reply.outcome, Message.Outcome.FAQ)
        # ...instead the close FAQ is given to the model as context, under grounding rules.
        self.assertEqual(len(STREAM_CALLS), 1)
        self.assertIn("Q: Who is the CEO of Ei Consulting?", STREAM_CALLS[0][-1]["content"])
        self.assertEqual(events[1]["sources"][0]["document"], "FAQ")
        self.assertEqual(reply.outcome, Message.Outcome.ANSWERED)

    async def test_unrelated_question_without_documents_is_not_found(self):
        events, reply = await self.ask("What is the capital of France?")
        self.assertEqual((events[2]["text"], reply.outcome), ("NOT IN DOCS", Message.Outcome.NOT_FOUND))
        self.assertEqual(STREAM_CALLS, [])

    async def test_other_workspaces_and_disabled_faqs_are_ignored(self):
        events, reply = await self.ask("Who is the CEO of Ei Consulting?", slug="other")
        self.assertEqual(reply.outcome, Message.Outcome.UNAVAILABLE)
        await FAQ.objects.filter(pk=self.ceo.pk).aupdate(enabled=False)
        events, reply = await self.ask("Who is the CEO of Ei Consulting?")
        self.assertNotEqual(reply.outcome, Message.Outcome.FAQ)

    async def test_faq_wins_over_documents_and_indexes_once(self):
        def add_doc():
            doc = Document.objects.create(tenant=self.acme, name="h.md", extension="md", size=1, sha256="h",
                                          status=Document.Status.READY, chunk_count=1,
                                          embed_signature=current_embed_signature())
            Chunk.objects.create(tenant=self.acme, document=doc, index=0, text="Annual leave is 25 days.",
                                 embedding=Chunk.pack(TOPICS["leave"]), dimensions=4)
        await sync_to_async(add_doc)()
        await self.ask("How many leave days do I get?")
        await self.ask("How many leave days do I get?")
        self.assertEqual(STREAM_CALLS, [])
        # FAQ phrasings embedded once (first request), then reused.
        phrasing_batches = [c for c in EMBED_CALLS if len(c) == 3]
        self.assertEqual(len(phrasing_batches), 1)


@override_settings(**SETTINGS)
@mock.patch("llm.embed", fake_embed)
class FAQAdminTests(TestCase):
    def setUp(self):
        llm.invalidate()
        self.t = Tenant.objects.create(name="Acme", slug="acme")
        self.client.force_login(get_user_model().objects.create_superuser("root", "r@example.com", "pw"))

    def test_create_indexes_and_lists(self):
        resp = self.client.post("/admin/rag/faq/add/", {
            "tenant": self.t.pk, "question": "What are your office hours?",
            "alternate_questions": "When are you open?", "answer": "9 to 5.", "enabled": "on",
        })
        self.assertEqual(resp.status_code, 302)
        faq = FAQ.objects.get()
        self.assertEqual(faq.matrix().shape, (2, 4))
        listing = self.client.get("/admin/rag/faq/")
        self.assertContains(listing, "What are your office hours?")
        self.assertContains(listing, "Current")
        self.assertContains(listing, "answered with its answer directly")

    def test_editing_phrasings_reindexes_and_failure_is_reported(self):
        faq = FAQ.objects.create(tenant=self.t, question="Q one?", answer="A")
        with mock.patch("llm.embed", side_effect=llm.LLMError("Cannot reach the model provider")):
            resp = self.client.post("/admin/rag/faq/", {"action": "reindex", "index": 0,
                                                         "_selected_action": [faq.pk]}, follow=True)
        self.assertContains(resp, "indexed automatically on first use")
        resp = self.client.post("/admin/rag/faq/", {"action": "reindex", "index": 0,
                                                     "_selected_action": [faq.pk]}, follow=True)
        self.assertContains(resp, "Indexed 1 FAQ.")
