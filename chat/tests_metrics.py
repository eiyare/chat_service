"""Performance metrics: outcome recording, calculations and the report page."""
import json
from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from saas_chat.testing import TestCase  # signed in by default
from django.utils import timezone

import llm
from chat import metrics
from chat.models import Conversation, Message
from rag.models import Chunk, Document, current_embed_signature
from tenants.models import Tenant

O = Message.Outcome


class PercentileTests(SimpleTestCase):
    def test_nearest_rank(self):
        self.assertIsNone(metrics.percentile([], 50))
        self.assertEqual(metrics.percentile([5], 95), 5)
        self.assertEqual(metrics.percentile([1, 2, 3, 4], 50), 2)
        self.assertEqual(metrics.percentile(list(range(1, 101)), 95), 95)
        self.assertEqual(metrics.percentile([None, 3, 1], 50), 1)


@override_settings(RAG_MIN_SCORE=0.5, RAG_NO_ANSWER_MESSAGE="NOT IN DOCS")
class ComputeTests(TestCase):
    def setUp(self):
        self.t = Tenant.objects.create(name="T", slug="t")
        self.other = Tenant.objects.create(name="O", slug="o")
        self.conv = Conversation.objects.create(tenant=self.t, title="c", model="m")
        src = [{"n": 1, "document": "handbook.md", "document_id": 1, "index": 0, "score": 0.7}]
        rows = [
            # question, outcome, model, response, retrieval, in, out, score, passages, sources, content
            ("Leave days?", O.ANSWERED, "p/a", 2000, 100, 300, 20, 0.70, 1, src, "25 days [1]."),
            ("Plan cost?", O.ANSWERED, "p/a", 4000, 200, 400, 40, 0.80, 2, src, "149 USD."),
            ("CEO?", O.DECLINED, "p/b", 3000, 150, 350, 5, 0.55, 1, src, "NOT IN DOCS"),
            ("Capital of France?", O.NOT_FOUND, "", 300, 120, None, None, 0.41, 0, [], "NOT IN DOCS"),
            ("Anything?", O.UNAVAILABLE, "", 50, None, None, None, None, 0, [], "No docs"),
        ]
        for q, outcome, model, resp, ret, pin, pout, score, n, sources, content in rows:
            Message.objects.create(conversation=self.conv, role="user", content=q)
            Message.objects.create(conversation=self.conv, role="assistant", content=content, model=model,
                                   outcome=outcome, response_ms=resp, retrieval_ms=ret, prompt_tokens=pin,
                                   completion_tokens=pout, top_score=score, passage_count=n, sources=sources)
        # Another workspace's data must never appear.
        oc = Conversation.objects.create(tenant=self.other, title="x", model="m")
        Message.objects.create(conversation=oc, role="assistant", content="x", outcome=O.ANSWERED, model="p/a")

    async def report(self, days=30):
        return await metrics.performance(self.t, days, "p/embed", {"documents": 2, "chunks": 9, "stale": 0})

    async def test_kpis(self):
        k = (await self.report())["kpis"]
        self.assertEqual(k["questions"], 5)
        self.assertEqual(k["answered"], 2)
        self.assertAlmostEqual(k["answer_rate"], 2 / 5)
        self.assertAlmostEqual(k["retrieval_hit_rate"], 3 / 4)    # found passages / searches (excl. unavailable)
        self.assertAlmostEqual(k["grounded_precision"], 2 / 3)    # answered / (answered + declined)
        self.assertAlmostEqual(k["citation_rate"], 1 / 2)
        self.assertEqual((k["response_p50_ms"], k["response_p95_ms"]), (2000, 4000))
        self.assertEqual((k["tokens_in"], k["tokens_out"]), (1050, 65))
        self.assertEqual((k["model_calls"], k["model_calls_avoided"]), (3, 2))

    async def test_outcomes_daily_and_histogram(self):
        r = await self.report(7)
        self.assertEqual([o["count"] for o in r["outcomes"]], [2, 0, 1, 1, 1])
        self.assertEqual(len(r["daily"]), 7)
        today = r["daily"][-1]
        self.assertEqual((today["answered"], today["declined"], today["not_found"], today["other"]), (2, 1, 1, 1))
        hist = {b["bin"]: b for b in r["score_hist"]}
        self.assertEqual(hist[0.4]["not_answered"], 1)
        self.assertEqual(hist[0.7]["answered"], 1)
        self.assertEqual(hist[0.8]["answered"], 1)
        self.assertEqual(hist[0.55]["not_answered"], 1)

    async def test_models_documents_and_unanswered(self):
        r = await self.report()
        models = {m["model"]: m for m in r["models"]}
        self.assertEqual(models["p/a"]["calls"], 2)
        self.assertEqual(models["p/a"]["answer_rate"], 1)
        self.assertAlmostEqual(models["p/a"]["tokens_per_second"], (20 / 1.9 + 40 / 3.8) / 2)
        self.assertEqual(models["p/b"]["answer_rate"], 0)
        self.assertEqual(r["documents"], [{"document": "handbook.md", "answers": 2}])
        self.assertEqual([u["question"] for u in r["unanswered"]], ["Capital of France?", "CEO?"])
        self.assertEqual(r["embedding"]["chunks"], 9)

    async def test_period_filter(self):
        await Message.objects.filter(content="149 USD.").aupdate(created_at=timezone.now() - timedelta(days=40))
        self.assertEqual((await self.report(30))["kpis"]["questions"], 4)
        self.assertEqual((await self.report(0))["kpis"]["questions"], 5)

    async def test_long_ranges_use_weeks(self):
        await Message.objects.filter(content="149 USD.").aupdate(created_at=timezone.now() - timedelta(days=200))
        r = await self.report(0)
        self.assertEqual(r["bucket"], "week")
        self.assertEqual(sum(d["answered"] for d in r["daily"]), 2)


LEAVE, OFF = [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]


async def fake_embed(texts):
    return [LEAVE if "leave" in t.lower() else OFF for t in texts]


async def answering_stream(model, messages, temperature=None, max_tokens=None):
    yield {"type": "content", "text": "25 days [1]."}
    yield {"type": "done", "prompt_tokens": 100, "completion_tokens": 6, "duration_ms": 10}


async def declining_stream(model, messages, temperature=None, max_tokens=None):
    yield {"type": "content", "text": "NOT IN DOCS"}
    yield {"type": "done", "prompt_tokens": 100, "completion_tokens": 6, "duration_ms": 10}


@override_settings(LLM_CHAT_MODELS=["p/chat"], LLM_DEFAULT_CHAT_MODEL="p/chat", LLM_EMBED_MODEL="p/embed",
                   RAG_QUERY_PREFIX="", RAG_DOCUMENT_PREFIX="", RAG_MIN_SCORE=0.5,
                   RAG_NO_ANSWER_MESSAGE="NOT IN DOCS", RAG_EMPTY_KB_MESSAGE="NO DOCS")
@mock.patch("llm.embed", fake_embed)
class OutcomeRecordingTests(TestCase):
    def setUp(self):
        llm.invalidate()
        self.t = Tenant.objects.create(name="Acme", slug="acme")

    def add_doc(self):
        doc = Document.objects.create(tenant=self.t, name="h.md", extension="md", size=1, sha256="h",
                                      status=Document.Status.READY, chunk_count=1,
                                      embed_signature=current_embed_signature())
        Chunk.objects.create(tenant=self.t, document=doc, index=0, text="Employees get 25 days of annual leave.",
                             embedding=Chunk.pack(LEAVE), dimensions=3)

    async def ask(self, message):
        resp = await self.async_client.post("/t/acme/chat/api/send/", data=json.dumps(
            {"message": message, "model": "p/chat"}), content_type="application/json")
        b"".join([c async for c in resp.streaming_content])
        return await Message.objects.filter(role="assistant").alast()

    async def test_outcomes_and_retrieval_metrics_recorded(self):
        msg = await self.ask("How much leave?")
        self.assertEqual(msg.outcome, O.UNAVAILABLE)
        self.assertIsNone(msg.retrieval_ms)

        from asgiref.sync import sync_to_async
        await sync_to_async(self.add_doc)()
        with mock.patch("llm.stream_chat", answering_stream):
            msg = await self.ask("How much leave do I get?")
        self.assertEqual(msg.outcome, O.ANSWERED)
        self.assertEqual(msg.passage_count, 1)
        self.assertAlmostEqual(msg.top_score, 1.0, places=5)
        self.assertIsNotNone(msg.retrieval_ms)

        with mock.patch("llm.stream_chat", declining_stream):
            msg = await self.ask("Is annual leave paid in cash?")
        self.assertEqual(msg.outcome, O.DECLINED)

        msg = await self.ask("What is the capital of France?")
        self.assertEqual(msg.outcome, O.NOT_FOUND)
        self.assertEqual(msg.passage_count, 0)
        self.assertAlmostEqual(msg.top_score, 0.0, places=5)  # best raw similarity is still recorded


class ReportPageTests(TestCase):
    def setUp(self):
        self.t = Tenant.objects.create(name="Acme", slug="acme")

    def test_empty_period(self):
        resp = self.client.get("/t/acme/admin/performance/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No questions in this period yet")
        self.assertContains(resp, 'aria-current="page">Performance')

    def test_report_renders_with_data_and_period(self):
        conv = Conversation.objects.create(tenant=self.t, title="c", model="p/a")
        Message.objects.create(conversation=conv, role="user", content="<script>alert(1)</script>?")
        Message.objects.create(conversation=conv, role="assistant", content="NOT IN DOCS", outcome=O.NOT_FOUND,
                               response_ms=100, retrieval_ms=50, top_score=0.3, passage_count=0)
        resp = self.client.get("/t/acme/admin/performance/?days=7")
        self.assertContains(resp, "Questions the documents couldn't answer")
        self.assertContains(resp, "&lt;script&gt;alert(1)&lt;/script&gt;?")  # escaped
        self.assertNotContains(resp, "<script>alert(1)")
        self.assertContains(resp, 'class="on" aria-current="true">Last 7 days')
        self.assertContains(resp, 'id="perf-data"')
        self.assertContains(resp, "chart-daily")
        self.assertEqual(self.client.get("/t/acme/admin/performance/?days=abc").status_code, 200)

    def test_tenant_scoped(self):
        self.assertEqual(self.client.get("/t/nope/admin/performance/").status_code, 404)
