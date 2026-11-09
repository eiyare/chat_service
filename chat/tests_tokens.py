"""Per-workspace token allowance and output cap."""
import json
from datetime import datetime, timedelta
from unittest import mock

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from saas_chat.testing import TestCase  # signed in by default
from django.utils import timezone

import llm
from chat import usage as usage_lib
from chat.models import Conversation, Message
from rag.models import FAQ, Chunk, Document, current_embed_signature
from tenants.models import Tenant

LEAVE = [1.0, 0.0, 0.0]
CALLS = []


def _vec(text):
    t = text.lower()
    if "leave" in t:
        return LEAVE
    if "hours" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


async def fake_embed(texts):
    return [_vec(t) for t in texts]


async def fake_stream(model, messages, temperature=None, max_tokens=None):
    CALLS.append({"max_tokens": max_tokens})
    yield {"type": "content", "text": "25 days."}
    yield {"type": "done", "prompt_tokens": 600, "completion_tokens": 50, "duration_ms": 5}


@override_settings(LLM_CHAT_MODELS=["p/chat"], LLM_DEFAULT_CHAT_MODEL="p/chat", LLM_EMBED_MODEL="p/embed",
                   RAG_QUERY_PREFIX="", RAG_DOCUMENT_PREFIX="", RAG_MIN_SCORE=0.5, LLM_CONFIG_CACHE_SECONDS=0,
                   CHAT_TOKEN_ALLOWANCE=0, CHAT_TOKEN_PERIOD="month", CHAT_MAX_OUTPUT_TOKENS=0,
                   CHAT_TOKEN_LIMIT_MESSAGE="LIMIT until {reset}", RAG_NO_ANSWER_MESSAGE="NOT IN DOCS")
@mock.patch("llm.embed", fake_embed)
@mock.patch("llm.stream_chat", fake_stream)
class TokenLimitTests(TestCase):
    def setUp(self):
        llm.invalidate()
        CALLS.clear()
        self.t = Tenant.objects.create(name="Acme", slug="acme")
        doc = Document.objects.create(tenant=self.t, name="h.txt", extension="txt", size=1, sha256="h",
                                      status=Document.Status.READY, chunk_count=1,
                                      embed_signature=current_embed_signature())
        Chunk.objects.create(tenant=self.t, document=doc, index=0, text="Employees get 25 days of annual leave.",
                             embedding=Chunk.pack(LEAVE), dimensions=3)

    async def ask(self, message="How much annual leave do I get?"):
        resp = await self.async_client.post("/t/acme/chat/api/send/", data=json.dumps(
            {"message": message, "model": "p/chat"}), content_type="application/json")
        events = [json.loads(l) for l in b"".join([c async for c in resp.streaming_content]).decode().splitlines()]
        return events, await Message.objects.filter(role="assistant").alast()

    def set(self, **fields):
        Tenant.objects.filter(pk=self.t.pk).update(**fields)

    async def test_unlimited_by_default(self):
        for _ in range(3):
            _, reply = await self.ask()
            self.assertEqual(reply.outcome, Message.Outcome.ANSWERED)
        self.assertEqual(CALLS[-1]["max_tokens"], None)

    async def test_allowance_blocks_model_calls_once_used_up(self):
        await sync_to_async(self.set)(token_allowance=1000)
        _, first = await self.ask()                      # 650 tokens used
        _, second = await self.ask()                     # allowed (650 < 1000); now 1300
        events, third = await self.ask()                 # over the allowance
        self.assertEqual([first.outcome, second.outcome, third.outcome],
                         [Message.Outcome.ANSWERED, Message.Outcome.ANSWERED, Message.Outcome.LIMITED])
        self.assertTrue(events[2]["text"].startswith("LIMIT until "))
        self.assertEqual(len(CALLS), 2)                  # no model call when limited
        self.assertIsNone(third.prompt_tokens)

    async def test_faq_and_not_found_still_work_when_limited(self):
        await sync_to_async(self.set)(token_allowance=1)
        await sync_to_async(FAQ.objects.create)(tenant=self.t, question="What are your hours?", answer="9 to 5.")
        await self.ask()  # uses tokens -> over the allowance
        _, faq_reply = await self.ask("What are your hours?")
        _, nf_reply = await self.ask("What is the capital of France?")
        self.assertEqual((faq_reply.outcome, nf_reply.outcome), (Message.Outcome.FAQ, Message.Outcome.NOT_FOUND))

    async def test_usage_resets_with_the_period(self):
        await sync_to_async(self.set)(token_allowance=1000, token_period="day")
        await self.ask()
        await self.ask()
        await Message.objects.filter(role="assistant").aupdate(created_at=timezone.now() - timedelta(days=2))
        _, reply = await self.ask()
        self.assertEqual(reply.outcome, Message.Outcome.ANSWERED)

    async def test_max_output_tokens_passed_to_model(self):
        await sync_to_async(self.set)(max_output_tokens=256)
        await self.ask()
        self.assertEqual(CALLS[-1]["max_tokens"], 256)

    @override_settings(CHAT_TOKEN_ALLOWANCE=500, CHAT_MAX_OUTPUT_TOKENS=128)
    async def test_platform_defaults_and_overrides(self):
        policy = (await Tenant.objects.aget(pk=self.t.pk)).token_policy
        self.assertEqual((policy.allowance, policy.period, policy.max_output_tokens), (500, "month", 128))
        await sync_to_async(self.set)(token_allowance=0, max_output_tokens=0, token_period="week")  # 0 = unlimited
        policy = (await Tenant.objects.aget(pk=self.t.pk)).token_policy
        self.assertEqual((policy.allowance, policy.period, policy.max_output_tokens), (None, "week", None))

    async def test_usage_counts_only_this_workspace_and_period(self):
        other = await Tenant.objects.acreate(name="O", slug="o")
        conv = await Conversation.objects.acreate(tenant=other, title="x", model="m")
        await Message.objects.acreate(conversation=conv, role="assistant", content="x", prompt_tokens=9999)
        await self.ask()
        usage = await usage_lib.current(await Tenant.objects.aget(pk=self.t.pk))
        self.assertEqual(usage.used, 650)


class PeriodTests(TestCase):
    def test_period_bounds(self):
        t = Tenant(name="x", slug="x", token_period="month")
        now = timezone.make_aware(datetime(2026, 11, 18, 15, 30))
        start, reset = t.token_policy.period_bounds(now)
        self.assertEqual((start.day, start.hour, reset.month, reset.day), (1, 0, 12, 1))
        t.token_period = "week"
        start, reset = t.token_policy.period_bounds(now)
        self.assertEqual((start.weekday(), (reset - start).days), (0, 7))
        t.token_period = "day"
        start, reset = t.token_policy.period_bounds(timezone.make_aware(datetime(2026, 12, 31, 23, 0)))
        self.assertEqual((reset.year, reset.month, reset.day), (2027, 1, 1))


@override_settings(CHAT_TOKEN_ALLOWANCE=0, CHAT_TOKEN_PERIOD="month", CHAT_MAX_OUTPUT_TOKENS=0)
class TokenAdminAndReportTests(TestCase):
    def setUp(self):
        self.t = Tenant.objects.create(name="Acme", slug="acme")
        self.client.force_login(get_user_model().objects.create_superuser("root", "r@example.com", "pw"))
        conv = Conversation.objects.create(tenant=self.t, title="c", model="m")
        Message.objects.create(conversation=conv, role="assistant", content="a", outcome="answered",
                               prompt_tokens=900, completion_tokens=100)

    def test_tenant_admin_fields_and_usage_column(self):
        page = self.client.get(f"/admin/tenants/tenant/{self.t.pk}/change/")
        self.assertContains(page, "Chat token limits")
        self.assertContains(page, "Platform default: unlimited")
        resp = self.client.post(f"/admin/tenants/tenant/{self.t.pk}/change/", {
            "memberships-TOTAL_FORMS": "0", "memberships-INITIAL_FORMS": "0",
            "memberships-MIN_NUM_FORMS": "0", "settings_override-TOTAL_FORMS": "0", "settings_override-INITIAL_FORMS": "0", "settings_override-MIN_NUM_FORMS": "0", "settings_override-MAX_NUM_FORMS": "1", "memberships-MAX_NUM_FORMS": "1000", "chatbot_name": "Tosin",
            "name": "Acme", "slug": "acme", "is_active": "on", "max_file_mb": "", "max_documents": "",
            "allowed_file_types": "", "token_allowance": "2000", "token_period": "week", "max_output_tokens": "300",
        })
        self.assertEqual(resp.status_code, 302)
        policy = Tenant.objects.get(pk=self.t.pk).token_policy
        self.assertEqual((policy.allowance, policy.period, policy.max_output_tokens), (2000, "week", 300))
        self.assertContains(self.client.get("/admin/tenants/tenant/"), "1,000 / 2,000 per week")

    def test_performance_meter(self):
        page = self.client.get("/t/acme/admin/performance/")
        self.assertContains(page, "no allowance set")
        Tenant.objects.filter(pk=self.t.pk).update(token_allowance=1050)
        page = self.client.get("/t/acme/admin/performance/")
        self.assertContains(page, "of 1,050 tokens used this month (95%)")
        self.assertContains(page, "Almost used")
        Tenant.objects.filter(pk=self.t.pk).update(token_allowance=1000)
        self.assertContains(self.client.get("/t/acme/admin/performance/"), "Limit reached")
