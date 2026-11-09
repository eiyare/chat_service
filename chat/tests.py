"""Chat view tests. Model calls are mocked at the `llm` adapter boundary; retrieval is real."""
import json
from unittest import mock

from django.test import TestCase, override_settings

from saas_chat.testing import TestCase  # signed in by default

import llm
from rag.models import Chunk, Document, current_embed_signature
from tenants.models import Tenant

from .models import Conversation, Message

# Tiny 3-D "embedding space": one axis per topic.
LEAVE, PRICING, OFF_TOPIC = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]
EMBED_INPUTS = []
STREAM_CALLS = []


def vector_for(text):
    text = text.lower()
    if "leave" in text or "holiday" in text:
        return LEAVE
    if "price" in text or "cost" in text or "plan" in text:
        return PRICING
    return OFF_TOPIC


async def fake_embed(texts):
    EMBED_INPUTS.append(list(texts))
    return [vector_for(t) for t in texts]


async def fake_stream(model, messages, temperature=None, max_tokens=None):
    STREAM_CALLS.append({"model": model, "messages": messages, "temperature": temperature})
    yield {"type": "content", "text": "You get 25 days [1]."}
    yield {"type": "done", "prompt_tokens": 120, "completion_tokens": 7, "duration_ms": 100}


async def failing_stream(model, messages, temperature=None, max_tokens=None):
    yield {"type": "content", "text": "partial"}
    raise llm.LLMError("Cannot reach the model provider")


async def failing_embed(texts):
    raise llm.LLMError("Cannot reach the model provider")


async def read_events(response):
    body = b"".join([chunk async for chunk in response.streaming_content])
    return [json.loads(line) for line in body.decode().splitlines() if line.strip()]


SETTINGS = dict(
    LLM_CHAT_MODELS=["prov/chat-a", "prov/chat-b"],
    LLM_DEFAULT_CHAT_MODEL="prov/chat-a",
    LLM_EMBED_MODEL="prov/embed",
    RAG_QUERY_PREFIX="q: ",
    RAG_DOCUMENT_PREFIX="d: ",
    RAG_MIN_SCORE=0.5,
    RAG_TOP_K=5,
    RAG_TEMPERATURE=0.1,
    RAG_NO_ANSWER_MESSAGE="NOT IN DOCS",
    RAG_EMPTY_KB_MESSAGE="NO DOCS YET",
    RAG_NO_EMBEDDING_MESSAGE="SEARCH OFF",
    RAG_NEEDS_REINDEX_MESSAGE="REINDEX NEEDED",
    RAG_SYSTEM_PROMPT="Assistant for {workspace}. If unsure say: {no_answer}",
)


@override_settings(**SETTINGS)
@mock.patch("llm.embed", fake_embed)
@mock.patch("llm.stream_chat", fake_stream)
class GroundedChatTests(TestCase):
    def setUp(self):
        EMBED_INPUTS.clear()
        STREAM_CALLS.clear()
        self.acme = Tenant.objects.create(name="Acme", slug="acme")
        self.other = Tenant.objects.create(name="Other", slug="other")
        self.other_convo = Conversation.objects.create(tenant=self.other, title="Secret", model="prov/chat-a")

    def add_doc(self, tenant, name, chunks, signature=None):
        with self.settings(**SETTINGS):
            sig = current_embed_signature() if signature is None else signature
        doc = Document.objects.create(
            tenant=tenant, name=name, extension="md", size=1, sha256=name,
            status=Document.Status.READY, chunk_count=len(chunks), embed_signature=sig,
        )
        for i, (text, vec) in enumerate(chunks):
            Chunk.objects.create(tenant=tenant, document=doc, index=i, text=text,
                                 embedding=Chunk.pack(vec), dimensions=len(vec))
        return doc

    def add_handbook(self, tenant=None):
        return self.add_doc(tenant or self.acme, "handbook.md", [
            ("Employees get 25 days of annual leave.", LEAVE),
            ("The Growth plan costs 149 USD.", PRICING),
        ])

    async def send(self, slug="acme", **payload):
        payload.setdefault("message", "How much leave do I get?")
        payload.setdefault("model", "prov/chat-a")
        return await self.async_client.post(
            f"/t/{slug}/chat/api/send/", data=json.dumps(payload), content_type="application/json"
        )

    # --- page ---------------------------------------------------------------

    async def test_page_lists_configured_models_and_no_docs_note(self):
        resp = await self.async_client.get("/t/acme/chat/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Hi, I'm Tosin")
        self.assertContains(resp, "Upload some in Admin")
        config = json.loads(resp.content.decode().split('id="chat-config" type="application/json">')[1].split("</script>")[0])
        self.assertEqual(config["models"], [{"id": "prov/chat-a", "label": "prov/chat-a"},
                                            {"id": "prov/chat-b", "label": "prov/chat-b"}])
        self.assertEqual(config["defaultModel"], "prov/chat-a")
        self.assertNotContains(resp, "Secret")

    async def test_page_hides_note_when_documents_exist(self):
        await self._async(self.add_handbook)
        resp = await self.async_client.get("/t/acme/chat/")
        self.assertNotContains(resp, "Upload some in Admin")

    # --- grounding ----------------------------------------------------------

    async def test_no_documents_gives_fixed_reply_without_model(self):
        events = await read_events(await self.send())
        self.assertEqual([e["type"] for e in events], ["meta", "sources", "content", "done"])
        self.assertEqual(events[2]["text"], "NO DOCS YET")
        self.assertFalse(events[-1]["grounded"])
        self.assertEqual(STREAM_CALLS, [])
        self.assertEqual(EMBED_INPUTS, [])  # nothing to search, so no embedding call

    async def test_similar_looking_question_without_matching_terms_skips_model(self):
        # "holiday" maps to the leave vector (high embedding score) but the word
        # isn't in any passage, so the term check refuses it before the model.
        await self._async(self.add_handbook)
        events = await read_events(await self.send(message="When is the next public holiday?"))
        self.assertEqual(events[2]["text"], "NOT IN DOCS")
        self.assertEqual(STREAM_CALLS, [])
        self.assertIsNone(events[-1]["stats"]["total_tokens"])

    async def test_off_topic_question_is_refused_without_model(self):
        await self._async(self.add_handbook)
        events = await read_events(await self.send(message="What is the capital of France?"))
        self.assertEqual(events[1], {"type": "sources", "sources": []})
        self.assertEqual(events[2]["text"], "NOT IN DOCS")
        self.assertEqual(STREAM_CALLS, [])
        reply = await Message.objects.filter(role="assistant").alast()
        self.assertEqual((reply.content, reply.model, reply.sources), ("NOT IN DOCS", "", []))

    async def test_on_topic_question_is_answered_from_passages(self):
        await self._async(self.add_handbook)
        events = await read_events(await self.send(message="How much leave do I get?"))

        sources = events[1]["sources"]
        self.assertEqual(len(sources), 1)  # pricing chunk scores 0 -> excluded
        self.assertEqual((sources[0]["n"], sources[0]["document"], sources[0]["index"]), (1, "handbook.md", 0))
        self.assertTrue(events[-1]["grounded"])

        call = STREAM_CALLS[0]
        self.assertEqual(call["model"], "prov/chat-a")
        self.assertEqual(call["temperature"], 0.1)
        system, user = call["messages"][0], call["messages"][-1]
        self.assertEqual(system, {"role": "system", "content": "Assistant for Acme. If unsure say: NOT IN DOCS"})
        self.assertIn("[1] (source: handbook.md)\nEmployees get 25 days of annual leave.", user["content"])
        self.assertIn("Question: How much leave do I get?", user["content"])
        self.assertNotIn("149 USD", user["content"])

        self.assertEqual(EMBED_INPUTS[0], ["q: How much leave do I get?"])  # query prefix applied

        reply = await Message.objects.filter(role="assistant").alast()
        self.assertEqual(reply.content, "You get 25 days [1].")
        self.assertEqual(reply.sources[0]["document"], "handbook.md")
        user_msg = await Message.objects.filter(role="user").alast()
        self.assertEqual(user_msg.content, "How much leave do I get?")  # stored without the context

    async def test_answer_reports_response_time_and_tokens(self):
        await self._async(self.add_handbook)
        with self.assertLogs("chat.views", "INFO") as logs:
            events = await read_events(await self.send())
        stats = events[-1]["stats"]
        self.assertEqual(
            (stats["prompt_tokens"], stats["completion_tokens"], stats["total_tokens"]), (120, 7, 127)
        )
        self.assertIsInstance(stats["response_ms"], int)

        reply = await Message.objects.filter(role="assistant").alast()
        self.assertEqual(reply.stats(), stats)
        self.assertIn("input_tokens=120 output_tokens=7 total_tokens=127", logs.output[0])

        # Reopening the conversation shows the same figures.
        resp = await self.async_client.get(events[0]["url"])
        saved = json.loads(resp.content.decode().split('id="chat-messages" type="application/json">')[1].split("</script>")[0])
        self.assertEqual(saved[-1]["stats"], stats)
        self.assertIsNone(saved[0]["stats"])  # user messages carry no stats

    async def test_fixed_reply_reports_time_but_no_tokens(self):
        await self._async(self.add_handbook)
        events = await read_events(await self.send(message="What is the capital of France?"))
        stats = events[-1]["stats"]
        self.assertIsInstance(stats["response_ms"], int)
        self.assertEqual(
            (stats["prompt_tokens"], stats["completion_tokens"], stats["total_tokens"]), (None, None, None)
        )

    async def test_follow_up_searches_with_previous_question_and_strips_old_citations(self):
        await self._async(self.add_handbook)
        first = await read_events(await self.send(message="How much leave do I get?"))
        cid = first[0]["conversation_id"]
        await read_events(await self.send(message="And can I carry it over?", conversation_id=cid))

        self.assertEqual(EMBED_INPUTS[-1], [
            "q: And can I carry it over?",
            "q: How much leave do I get?\nAnd can I carry it over?",
        ])
        messages = STREAM_CALLS[-1]["messages"]
        self.assertEqual(messages[1], {"role": "user", "content": "How much leave do I get?"})
        self.assertEqual(messages[2], {"role": "assistant", "content": "You get 25 days."})
        self.assertIn("Question: And can I carry it over?", messages[-1]["content"])

    async def test_other_tenants_documents_are_never_used(self):
        await self._async(self.add_handbook, self.other)
        events = await read_events(await self.send())
        self.assertEqual(events[2]["text"], "NO DOCS YET")
        self.assertEqual(STREAM_CALLS, [])

    async def test_documents_with_outdated_embeddings_are_ignored(self):
        await self._async(self.add_doc, self.acme, "old.md", [("Leave is 25 days.", LEAVE)], "old-model|")
        events = await read_events(await self.send())
        self.assertEqual(events[2]["text"], "REINDEX NEEDED")
        self.assertEqual(STREAM_CALLS, [])
        resp = await self.async_client.get("/t/acme/chat/")
        self.assertContains(resp, "need reindexing")
        self.assertNotContains(resp, "No documents yet")

    async def test_no_embedding_model_is_reported_not_called_empty(self):
        await self._async(self.add_handbook)
        with self.settings(LLM_EMBED_MODEL=""):
            events = await read_events(await self.send())
            page = await self.async_client.get("/t/acme/chat/")
        self.assertEqual(events[2]["text"], "SEARCH OFF")
        self.assertEqual((STREAM_CALLS, EMBED_INPUTS), ([], []))
        self.assertContains(page, "Document search is unavailable")
        self.assertNotContains(page, "No documents yet")

    # --- validation and failures -------------------------------------------

    async def test_model_outside_allowlist_rejected(self):
        resp = await self.send(model="other-provider/expensive-model")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(await Message.objects.acount(), 0)

    @override_settings(LLM_CHAT_MODELS=[], LLM_DEFAULT_CHAT_MODEL="")
    async def test_no_models_configured(self):
        resp = await self.send(model="")
        self.assertEqual(resp.status_code, 503)

    async def test_embedding_failure_returns_503_and_saves_nothing(self):
        await self._async(self.add_handbook)
        with mock.patch("llm.embed", failing_embed):
            resp = await self.send()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(await Conversation.objects.filter(tenant=self.acme).acount(), 0)
        self.assertEqual(await Message.objects.acount(), 0)

    async def test_model_failure_mid_stream_keeps_partial_and_reports_error(self):
        await self._async(self.add_handbook)
        with mock.patch("llm.stream_chat", failing_stream):
            events = await read_events(await self.send())
        self.assertEqual(events[-1], {"type": "error", "error": "Cannot reach the model provider"})
        reply = await Message.objects.filter(role="assistant").alast()
        self.assertEqual(reply.content, "partial")

    async def test_empty_and_too_long_messages_rejected(self):
        self.assertEqual((await self.send(message="  ")).status_code, 400)
        with self.settings(CHAT_MAX_MESSAGE_CHARS=5):
            self.assertEqual((await self.send(message="toolong")).status_code, 400)

    # --- tenant scoping -----------------------------------------------------

    async def test_unknown_tenant_404(self):
        self.assertEqual((await self.async_client.get("/t/nope/chat/")).status_code, 404)

    async def test_other_tenants_conversation_is_404(self):
        resp = await self.async_client.get(f"/t/acme/chat/c/{self.other_convo.pk}/")
        self.assertEqual(resp.status_code, 404)
        resp = await self.send(conversation_id=self.other_convo.pk)
        self.assertEqual(resp.status_code, 404)

    async def test_delete_scoped_to_tenant(self):
        resp = await self.async_client.post(f"/t/acme/chat/api/c/{self.other_convo.pk}/delete/")
        self.assertEqual(resp.status_code, 404)
        mine = await Conversation.objects.acreate(tenant=self.acme, title="Mine", model="x", user=self.signed_in_user)
        resp = await self.async_client.post(f"/t/acme/chat/api/c/{mine.pk}/delete/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(await Conversation.objects.filter(pk=mine.pk).aexists())

    @staticmethod
    async def _async(fn, *args):
        from asgiref.sync import sync_to_async
        return await sync_to_async(fn)(*args)
