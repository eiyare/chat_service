"""The assistant's persona: addressed by name, small talk, and name-aware search."""
import json
from unittest import mock

from asgiref.sync import sync_to_async
from django.test import SimpleTestCase, override_settings

import llm
from chat import persona
from chat.models import Message
from chat.views import grounding_prompt
from rag.models import FAQ, Chunk, Document, current_embed_signature
from saas_chat.testing import TestCase
from siteconfig import conf
from tenants.models import Tenant


class IntentTests(SimpleTestCase):
    def test_small_talk_is_recognised(self):
        cases = {
            "Hi Tosin": "greeting", "tosin?": "greeting", "Good morning!": "greeting", "Hey there": "greeting",
            "Who are you?": "identity", "What’s your name?": "identity", "Are you Tosin?": "identity",
            "Who is Tosin?": "identity", "Are you a bot?": "identity",
            "Thanks Tosin!": "thanks", "ok thank you so much": "thanks",
            "What can you do?": "help", "How can you help me, Tosin?": "help",
            "Bye Tosin": "goodbye",
        }
        for text, expected in cases.items():
            self.assertEqual(persona.intent(text, "Tosin"), expected, text)

    def test_real_questions_are_not_small_talk(self):
        for text in ("Hi Tosin, how many vacation days do I get?", "Who is the CEO?", "Are you open on Sundays?",
                     "What is the leave policy?", "Thanks for the list, but what about the Growth plan price?",
                     "Who is Tosin's manager in the finance team and where do they sit?"):
            self.assertIsNone(persona.intent(text, "Tosin"), text)

    def test_other_names_are_not_mine(self):
        self.assertIsNone(persona.intent("Hi Tosin, what is the leave policy", "Ada"))
        self.assertEqual(persona.intent("Hello Ada", "Ada"), "greeting")
        self.assertEqual(persona.intent("hello ada lovelace", "Ada Lovelace"), "greeting")

    def test_search_text_drops_name_and_greeting(self):
        self.assertEqual(persona.search_text("Hi Tosin, how much leave do I get?", "Tosin"), "how much leave do I get?")
        self.assertEqual(persona.search_text("hey tosin – whats the plan price", "Tosin"), "whats the plan price")
        self.assertEqual(persona.search_text("Tosin, who is the CEO?", "Tosin"), "who is the CEO?")
        self.assertEqual(persona.search_text("What does Tosin's team do?", "Tosin"), "What does team do?")
        self.assertEqual(persona.search_text("What is the leave policy?", "Tosin"), "What is the leave policy?")

    def test_can_be_switched_off(self):
        self.assertIsNone(persona.intent("Hi Tosin", "Tosin", enabled=False))


LEAVE = [1.0, 0.0, 0.0]
EMBEDS, STREAMS = [], []


async def fake_embed(texts):
    EMBEDS.append(list(texts))
    return [LEAVE if ("leave" in t.lower() or "vacation" in t.lower()) else [0.0, 0.0, 1.0] for t in texts]


async def fake_stream(model, messages, temperature=None, max_tokens=None):
    STREAMS.append(messages)
    yield {"type": "content", "text": "25 days [1]."}
    yield {"type": "done", "prompt_tokens": 10, "completion_tokens": 3, "duration_ms": 5}


@override_settings(LLM_CHAT_MODELS=["p/chat"], LLM_DEFAULT_CHAT_MODEL="p/chat", LLM_EMBED_MODEL="p/embed",
                   RAG_QUERY_PREFIX="", RAG_DOCUMENT_PREFIX="", RAG_MIN_SCORE=0.5, LLM_CONFIG_CACHE_SECONDS=0,
                   RAG_NO_ANSWER_MESSAGE="NOT IN DOCS", CHAT_TOKEN_ALLOWANCE=0)
@mock.patch("llm.embed", fake_embed)
@mock.patch("llm.stream_chat", fake_stream)
class PersonaChatTests(TestCase):
    def setUp(self):
        llm.invalidate()
        EMBEDS.clear()
        STREAMS.clear()
        self.t = Tenant.objects.create(name="Acme", slug="acme")
        doc = Document.objects.create(tenant=self.t, name="h.txt", extension="txt", size=1, sha256="h",
                                      status=Document.Status.READY, chunk_count=1,
                                      embed_signature=current_embed_signature())
        Chunk.objects.create(tenant=self.t, document=doc, index=0, text="Employees get 25 days of annual leave.",
                             embedding=Chunk.pack(LEAVE), dimensions=3)

    async def ask(self, message):
        resp = await self.async_client.post("/t/acme/chat/api/send/", data=json.dumps(
            {"message": message, "model": "p/chat"}), content_type="application/json")
        events = [json.loads(l) for l in b"".join([c async for c in resp.streaming_content]).decode().splitlines()]
        return events, await Message.objects.filter(role="assistant").alast()

    async def test_greeting_and_identity_answered_in_persona_without_search_or_model(self):
        events, reply = await self.ask("Hi Tosin!")
        self.assertEqual(events[2]["text"], "Hi! I'm Tosin, the assistant for Acme. Ask me anything about Acme's documents.")
        self.assertEqual((reply.outcome, reply.prompt_tokens), (Message.Outcome.SMALL_TALK, None))
        events, reply = await self.ask("Who are you?")
        self.assertTrue(events[2]["text"].startswith("I'm Tosin, Acme's document assistant."))
        self.assertEqual((EMBEDS, STREAMS), ([], []))

    async def test_addressing_by_name_does_not_break_search(self):
        events, reply = await self.ask("Hi Tosin, how much annual leave do I get?")
        self.assertEqual(reply.outcome, Message.Outcome.ANSWERED)
        self.assertEqual(EMBEDS[0], ["how much annual leave do I get?"])  # name and greeting removed
        self.assertIn("Question: Hi Tosin, how much annual leave do I get?", STREAMS[0][-1]["content"])  # model sees it all

    async def test_named_question_still_matches_faq(self):
        await sync_to_async(FAQ.objects.create)(tenant=self.t, question="How many vacation days do I get?",
                                                answer="25 days.")
        events, reply = await self.ask("Tosin, how many vacation days do I get?")
        self.assertEqual((reply.outcome, events[2]["text"]), (Message.Outcome.FAQ, "25 days."))

    async def test_renamed_assistant_answers_to_its_new_name(self):
        await Tenant.objects.filter(pk=self.t.pk).aupdate(chatbot_name="Ada")
        events, reply = await self.ask("Hello Ada")
        self.assertIn("I'm Ada", events[2]["text"])
        events, reply = await self.ask("Hi Tosin")  # not its name: treated as an ordinary message
        self.assertNotEqual(reply.outcome, Message.Outcome.SMALL_TALK)

    def test_persona_rule_in_system_prompt(self):
        prompt = grounding_prompt(conf.static(self.t))
        self.assertIn("Your name is Tosin. Messages that address Tosin are speaking to you", prompt)

    async def test_small_talk_counts_with_faq_in_report(self):
        await self.ask("Thanks Tosin")
        page = await self.async_client.get("/t/acme/admin/performance/")
        self.assertContains(page, "FAQ or small talk")
