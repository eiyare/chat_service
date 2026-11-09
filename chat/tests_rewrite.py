"""Question rewriting before search, and strong matches skipping the key-word check."""
import json
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import override_settings

import llm
from chat.models import Message
from rag.models import Chunk, Document, current_embed_signature
from saas_chat.testing import TestCase
from siteconfig.models import WorkspaceSettings
from tenants.models import Tenant

HOURS = [1.0, 0.0, 0.0]
EMBEDS, CALLS = [], []


async def fake_embed(texts, purpose="document"):
    EMBEDS.append(list(texts))
    return [HOURS if "opening hours" in t.lower() else [0.0, 0.0, 1.0] for t in texts]


async def fake_stream(model, messages, temperature=None, max_tokens=None):
    CALLS.append(messages)
    if "search query" in messages[0]["content"]:  # the rewriting call
        yield {"type": "content", "text": "Friday closing time opening hours"}
        yield {"type": "done", "prompt_tokens": 40, "completion_tokens": 6, "duration_ms": 5}
        return
    yield {"type": "content", "text": "On Friday we close at 23:15 [1]."}
    yield {"type": "done", "prompt_tokens": 300, "completion_tokens": 12, "duration_ms": 5}


@override_settings(LLM_CHAT_MODELS=["p/chat"], LLM_DEFAULT_CHAT_MODEL="p/chat", LLM_EMBED_MODEL="p/embed",
                   RAG_QUERY_PREFIX="", RAG_DOCUMENT_PREFIX="", RAG_MIN_SCORE=0.2, LLM_CONFIG_CACHE_SECONDS=0,
                   RAG_REWRITE_QUERIES=True, RAG_CONFIDENT_FACTOR=2.0, CHAT_TOKEN_ALLOWANCE=0)
@mock.patch("llm.embed", fake_embed)
@mock.patch("llm.stream_chat", fake_stream)
class RewriteTests(TestCase):
    def setUp(self):
        llm.invalidate()
        EMBEDS.clear()
        CALLS.clear()
        self.t = Tenant.objects.create(name="Pizza Co", slug="pizza")
        doc = Document.objects.create(tenant=self.t, name="d.pdf", extension="pdf", size=1, sha256="d",
                                      status=Document.Status.READY, chunk_count=1,
                                      embed_signature=current_embed_signature())
        Chunk.objects.create(tenant=self.t, document=doc, index=0,
                             text="Operating Hours: Thursday - Saturday: 09:00 - 23:15",
                             embedding=Chunk.pack(HOURS), dimensions=3)

    def ask(self, message):
        from widget.tests import _read
        resp = self.client.post("/t/pizza/chat/api/send/", json.dumps({"message": message, "model": "p/chat"}),
                                content_type="application/json")
        return [json.loads(l) for l in async_to_sync(_read)(resp).decode().splitlines()], Message.objects.filter(role="assistant").last()

    def test_paraphrased_question_is_found_through_rewriting(self):
        # Neither "close" nor "Friday" is in the document; the rewritten query finds the hours,
        # and its strong similarity skips the key-word check.
        events, reply = self.ask("What time do you close on Friday?")
        self.assertEqual(reply.outcome, Message.Outcome.ANSWERED)
        self.assertEqual(reply.search_query, "Friday closing time opening hours")
        self.assertEqual(EMBEDS[0][0], "Friday closing time opening hours")      # rewritten first
        self.assertEqual(EMBEDS[0][1], "What time do you close on Friday?")      # original kept
        self.assertEqual(len(CALLS), 2)                                          # rewrite + answer
        self.assertEqual((reply.prompt_tokens, reply.completion_tokens), (340, 18))  # both counted

    def test_rewriting_can_be_switched_off_per_workspace(self):
        WorkspaceSettings.objects.create(tenant=self.t, rewrite_queries=False)
        events, reply = self.ask("What time do you close on Friday?")
        self.assertEqual(reply.outcome, Message.Outcome.NOT_FOUND)  # no rewrite: the paraphrase isn't found
        self.assertEqual(len(CALLS), 0)

    def test_rewrite_sees_the_earlier_question_for_follow_ups(self):
        events, _ = self.ask("What time do you close on Friday?")
        cid = events[0]["conversation_id"]
        self.client.post("/t/pizza/chat/api/send/", json.dumps(
            {"message": "And on Saturday?", "model": "p/chat", "conversation_id": cid}), content_type="application/json")
        rewrite_call = [c for c in CALLS if "search query" in c[0]["content"]][-1]
        self.assertEqual(rewrite_call[1]["content"],
                         "Earlier question: What time do you close on Friday?\nLatest question: And on Saturday?")
