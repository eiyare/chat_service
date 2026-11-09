"""Nigerian Pidgin: small talk, search terms, replies and model instructions."""
import json
from unittest import mock

from django.test import SimpleTestCase, override_settings

import llm
from chat import persona
from chat.models import Message
from chat.views import grounded_question, grounding_prompt
from rag.faq import _wording_matches
from rag.lexical import terms
from rag.models import Chunk, Document, current_embed_signature
from saas_chat.testing import TestCase
from siteconfig import conf
from tenants.models import Tenant


class PidginUnitTests(SimpleTestCase):
    def test_small_talk(self):
        cases = {"How far Tosin": "greeting", "How you dey?": "greeting", "Good morning o": "greeting",
                 "Wetin be your name?": "identity", "Who you be?": "identity", "Na you be Tosin?": "identity",
                 "Tank you o": "thanks", "E do": "thanks", "Abeg wetin you fit do?": "help", "I dey go": "goodbye"}
        for text, expected in cases.items():
            self.assertEqual(persona.intent(text, "Tosin"), expected, text)

    def test_language_detection(self):
        for text in ("Abeg, wetin be quadratic equation?", "How I fit solve am?", "How far Tosin", "Tank you o",
                     "Wetin dey happen?"):
            self.assertEqual(persona.language(text), "pcm", text)
        for text in ("What is a quadratic equation?", "How far is the office from the station?",
                     "Do you have a demo?", "Hi Tosin, how much leave do I get?"):
            self.assertEqual(persona.language(text), "en", text)

    def test_pidgin_words_are_not_search_terms(self):
        self.assertEqual(terms("Abeg, wetin be quadratic equation?"), {"quadratic", "equation"})
        self.assertTrue(_wording_matches("Abeg how many leave days I dey get?", "How many days of annual leave do I get?"))

    def test_model_is_told_to_answer_in_pidgin(self):
        t = Tenant(name="Acme", chatbot_name="Tosin")
        self.assertIn("If it is written in Nigerian Pidgin English, answer in clear Nigerian Pidgin.", grounding_prompt(conf.static(t)))
        self.assertIn("answer in Nigerian Pidgin English", grounded_question("Wetin be X?", [], "pcm"))
        self.assertNotIn("Pidgin", grounded_question("What is X?", [], "en"))
        self.assertIn(persona.no_answer(conf.static(t), "pcm"), grounding_prompt(conf.static(t), persona.no_answer(conf.static(t), "pcm")))


async def fake_embed(texts):
    return [[1.0, 0.0] if "quadratic" in t.lower() else [0.0, 1.0] for t in texts]


STREAMS = []


async def fake_stream(model, messages, temperature=None, max_tokens=None):
    STREAMS.append(messages)
    yield {"type": "content", "text": "I no see dat one inside dis workspace documents."}
    yield {"type": "done", "prompt_tokens": 10, "completion_tokens": 9, "duration_ms": 5}


@override_settings(LLM_CHAT_MODELS=["p/chat"], LLM_DEFAULT_CHAT_MODEL="p/chat", LLM_EMBED_MODEL="p/embed",
                   RAG_QUERY_PREFIX="", RAG_DOCUMENT_PREFIX="", RAG_MIN_SCORE=0.5, LLM_CONFIG_CACHE_SECONDS=0,
                   CHAT_TOKEN_ALLOWANCE=0)
@mock.patch("llm.embed", fake_embed)
@mock.patch("llm.stream_chat", fake_stream)
class PidginChatTests(TestCase):
    def setUp(self):
        llm.invalidate()
        STREAMS.clear()
        self.t = Tenant.objects.create(name="Maths", slug="maths")
        doc = Document.objects.create(tenant=self.t, name="q.txt", extension="txt", size=1, sha256="q",
                                      status=Document.Status.READY, chunk_count=1,
                                      embed_signature=current_embed_signature())
        Chunk.objects.create(tenant=self.t, document=doc, index=0, text="A quadratic equation has the form ax2+bx+c=0.",
                             embedding=Chunk.pack([1.0, 0.0]), dimensions=2)

    async def ask(self, message):
        resp = await self.async_client.post("/t/maths/chat/api/send/", data=json.dumps(
            {"message": message, "model": "p/chat"}), content_type="application/json")
        events = [json.loads(l) for l in b"".join([c async for c in resp.streaming_content]).decode().splitlines()]
        return events, await Message.objects.filter(role="assistant").alast()

    async def test_pidgin_small_talk_and_not_found(self):
        events, _ = await self.ask("How far Tosin")
        self.assertTrue(events[2]["text"].startswith("How far! Na me be Tosin"))
        events, reply = await self.ask("Wetin be di capital of France?")
        self.assertEqual((events[2]["text"], reply.outcome),
                         ("I no see dat one inside dis workspace documents.", Message.Outcome.NOT_FOUND))

    async def test_pidgin_question_reaches_model_with_pidgin_instructions(self):
        events, reply = await self.ask("Abeg, wetin be quadratic equation?")
        self.assertEqual(len(STREAMS), 1)
        self.assertIn("answer in Nigerian Pidgin English", STREAMS[0][-1]["content"])
        self.assertIn("I no see dat one inside dis workspace documents.", STREAMS[0][0]["content"])
        self.assertEqual(reply.outcome, Message.Outcome.DECLINED)  # the Pidgin "not found" is recognised too


class TrailingNoAnswerTests(SimpleTestCase):
    def test_strip(self):
        from chat.views import strip_trailing_no_answer
        no = "I no see dat one inside dis workspace documents."
        self.assertEqual(strip_trailing_no_answer("Na ax2+bx+c=0 [1].\n\n" + no, [no]), "Na ax2+bx+c=0 [1].")
        self.assertEqual(strip_trailing_no_answer(no, [no]), no)  # a pure "not found" stays (declined)
        self.assertEqual(strip_trailing_no_answer("Answer.", [no]), "Answer.")
