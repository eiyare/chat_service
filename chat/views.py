"""Async chat views; model calls go through the provider-agnostic `llm` package. All views are scoped to request.tenant.

Answers are grounded exclusively in the workspace's documents (see api_send).
"""
import asyncio
import json
import logging
import re
import time

from django.conf import settings
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_GET, require_POST

import llm
from rag import faq, retrieval
from siteconfig import conf
from tenants.decorators import tenant_required

from . import persona
from . import rewrite as rewrite_lib
from . import usage as usage_lib
from .models import Conversation, Message

logger = logging.getLogger(__name__)


async def _get_conversation(tenant, pk, source=Conversation.Source.APP, visitor_id="", user=None):
    """A conversation of this workspace that the caller owns: the logged-in user's own
    (workspace chat) or the same widget visitor's."""
    filters = {"pk": pk, "tenant": tenant, "source": source, "visitor_id": visitor_id}
    if source == Conversation.Source.APP:
        filters["user"] = user
    try:
        return await Conversation.objects.aget(**filters)
    except (Conversation.DoesNotExist, ValueError, TypeError):
        raise Http404("Conversation not found")


async def _sidebar(tenant, user):
    qs = Conversation.objects.filter(
        tenant=tenant, source=Conversation.Source.APP, user=user
    ).only("id", "title", "updated_at")
    return [c async for c in qs[: settings.CHAT_SIDEBAR_LIMIT]]


async def _render_chat(request, conversation=None):
    tenant = request.tenant
    messages = []
    if conversation is not None:
        messages = [m.as_dict() async for m in conversation.messages.all()]
    llm_config = await llm.aget_config()
    config = {
        "conversationId": conversation.pk if conversation else None,
        "conversationModel": conversation.model if conversation else None,
        "models": [{"id": m, "label": llm_config.label(m)} for m in llm_config.chat_models],
        "defaultModel": llm_config.default_chat_model,
        "maxChars": (await conf.aget(tenant)).max_message_chars,
        "urls": {
            "home": reverse("chat:home", kwargs={"tenant_slug": tenant.slug}),
            "send": reverse("chat:api_send", kwargs={"tenant_slug": tenant.slug}),
            "deleteTemplate": reverse(
                "chat:api_delete", kwargs={"tenant_slug": tenant.slug, "pk": 0}
            ),
        },
    }
    return render(
        request,
        "chat/chat.html",
        {
            "tenant": tenant,
            "conversation": conversation,
            "conversations": await _sidebar(tenant, request.user),
            "messages_data": messages,
            "chat_config": config,
            "kb_status": await retrieval.kb_status(tenant),
        },
    )


@require_GET
@tenant_required
async def chat_home(request):
    return await _render_chat(request)


@require_GET
@tenant_required
async def conversation_detail(request, pk):
    conversation = await _get_conversation(request.tenant, pk, user=request.user)
    return await _render_chat(request, conversation)


@require_POST
@tenant_required
async def api_delete(request, pk):
    conversation = await _get_conversation(request.tenant, pk, user=request.user)
    await conversation.adelete()
    return JsonResponse({"ok": True})


def _json_error(message, status=400):
    return JsonResponse({"error": message}, status=status)


def _ndjson(event: dict) -> bytes:
    return (json.dumps(event) + "\n").encode()


_CITATION = re.compile(r"\s?\[\d+\](?:\[\d+\])*")


def strip_trailing_no_answer(content: str, sentences) -> str:
    """Small models sometimes append the "not in the documents" sentence after a real
    answer; drop it when the answer has other content."""
    text = content.rstrip()
    for sentence in sentences:
        sentence = sentence.strip()
        if sentence and text.endswith(sentence) and text[: -len(sentence)].strip():
            return text[: -len(sentence)].rstrip()
    return content


def grounding_prompt(cfg, no_answer: str | None = None) -> str:
    """The system prompt from the workspace's resolved settings (cfg), with the rules
    for its grounding mode (helpful or strict) at {grounding_rules}."""
    rules = cfg.strict_rules if cfg.grounding_mode == "strict" else cfg.helpful_rules
    prompt = cfg.system_prompt.replace("{grounding_rules}", rules)
    return conf.fill(prompt, cfg).replace("{no_answer}", no_answer or conf.fill(cfg.no_answer_message, cfg))


def grounded_question(question: str, passages, lang: str = "en") -> str:
    """The user's question wrapped with the retrieved passages it must be answered from."""
    blocks = [f"[{n}] (source: {p.document})\n{p.text}" for n, p in enumerate(passages, 1)]
    instruction = "Answer using only the context passages above."
    if lang == "pcm":
        instruction += " The question is in Nigerian Pidgin English: answer in Nigerian Pidgin English."
    return "Context passages:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question}\n\n{instruction}"


@require_POST
@tenant_required
async def api_send(request):
    return await respond(request, request.tenant, user=request.user)


async def respond(request, tenant, source=Conversation.Source.APP, visitor_id="", user=None):
    """Answer a question strictly from the workspace's documents, streamed as NDJSON.

    Shared by the workspace chat and the website widget (source/visitor_id scope
    which conversations the caller may continue; the widget always uses the
    default model).

    1. Retrieve the most relevant document chunks for the question.
    2. If none are relevant enough, reply with a fixed "not in the documents"
       message without calling the model at all.
    3. Otherwise ask the model to answer only from those passages.

    Request JSON: {"message": str, "model": str, "conversation_id": int|null}
    Response events (one JSON object per line):
      {"type": "meta", "conversation_id", "title", "url"}
      {"type": "sources", "sources": [{"n", "document", "document_id", "index", "score", ...}]}
      {"type": "thinking"|"content", "text"}
      {"type": "done", "message_id", "grounded",
       "stats": {"response_ms", "prompt_tokens", "completion_tokens", "total_tokens"}}
      {"type": "error", "error"}
    """
    started = time.monotonic()  # response time covers search + generation
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return _json_error("Invalid JSON body")
    if not isinstance(body, dict):
        return _json_error("Invalid JSON body")

    text = str(body.get("message") or "").strip()
    if not text:
        return _json_error("Message is empty")
    cfg = await conf.aget(tenant)  # this workspace's settings (overrides -> platform -> .env)
    if len(text) > cfg.max_message_chars:
        return _json_error(f"Message exceeds {cfg.max_message_chars} characters")

    llm_config = await llm.aget_config()
    requested = body.get("model") if source == Conversation.Source.APP else None
    model = requested or llm_config.default_chat_model
    if not model:
        return _json_error("No chat models are configured", status=503)
    # Only allowlisted models: the model string selects the provider, so an
    # arbitrary value could route requests (and costs) to any provider.
    if model not in llm_config.chat_models:
        return _json_error(f"Model '{model}' is not available")

    conversation = None
    conversation_id = body.get("conversation_id")
    if conversation_id:
        conversation = await _get_conversation(tenant, conversation_id, source, visitor_id, user)

    # --- Retrieval (before anything is saved, so a failure leaves no trace) ---
    # The assistant's persona: small talk is answered directly; being addressed by
    # name ("Hi Tosin, ...") must not affect FAQ matching or document search.
    name = tenant.chatbot_name
    small_talk = persona.intent(text, name, cfg.small_talk)
    lang = persona.language(text)  # reply in Nigerian Pidgin when asked in Pidgin
    no_answer = persona.no_answer(cfg, lang)
    search = persona.search_text(text, name)
    queries = [search]
    previous_text = None
    if conversation is not None and not small_talk:
        previous = await conversation.messages.filter(role=Message.Role.USER).alast()
        if previous:
            # Follow-ups ("and the second one?") need the earlier question for context.
            previous_text = persona.search_text(previous.content, name)
            queries.append(f"{previous_text}\n{search}")
    kb_status = await retrieval.kb_status(tenant)
    faqs = await faq.enabled_for(tenant)
    passages = retrieval.Results()
    faq_hit = None
    retrieval_ms = None
    search_started = time.monotonic()

    # 1. Same question as an FAQ (ignoring case/punctuation): answer it, no embedding needed.
    faq_hit = None if small_talk else faq.direct_match(search, None, faqs)
    search_query, rewrite_tokens = search, (0, 0)
    if (not small_talk and faq_hit is None and kb_status == retrieval.KB.READY and cfg.rewrite_queries
            and not (await usage_lib.current(tenant)).exhausted):
        # Turn the question into a plain-English search query (Pidgin, paraphrases, follow-ups).
        rewritten, *rewrite_tokens = await rewrite_lib.rewrite(model, search, previous_text, cfg)
        if rewritten:
            search_query = rewritten
            queries = [rewritten] + queries
    original = queries.index(search)  # the user's own wording, for FAQ matching
    if (not small_talk and faq_hit is None and (faqs or kb_status == retrieval.KB.READY)
            and llm_config.embed_model):
        try:
            # The questions are embedded once, for FAQ matching and document search alike.
            query_vectors = await retrieval.embed_queries(queries)
            indexed = await faq.index(faqs, llm_config) if faqs else []
            # 2. Close FAQ match (wording and meaning): answer it without the model.
            faq_hit = faq.direct_match(search, query_vectors[original], indexed, llm_config.faq_cutoff)
            if faq_hit is None:
                # 3. Document search, plus close FAQs as extra context for the model.
                if kb_status == retrieval.KB.READY:
                    passages = await retrieval.search(tenant, queries, query_vectors=query_vectors)
                faq_context = faq.context_passages(query_vectors[original], indexed, llm_config.faq_context_cutoff)
                if faq_context:
                    best = passages.best_score
                    passages = retrieval.Results(faq_context + list(passages))
                    passages.best_score = best
        except llm.LLMError as exc:
            return _json_error(f"Document search failed: {exc}", status=503)
        retrieval_ms = int((time.monotonic() - search_started) * 1000)

    # --- Persist the question ---
    if conversation is not None:
        conversation.model = model
        await conversation.asave(update_fields=["model", "updated_at"])
    else:
        title = text.splitlines()[0][: settings.CHAT_TITLE_MAX_CHARS].strip() or "New chat"
        conversation = await Conversation.objects.acreate(
            tenant=tenant, title=title, model=model, source=source, visitor_id=visitor_id,
            user=user if source == Conversation.Source.APP else None,
        )

    recent = conversation.messages.order_by("-created_at", "-id")[: max(cfg.history_limit - 1, 0)]
    history = [
        {
            "role": m.role,
            # Old citation numbers refer to old passages; drop them to avoid confusion.
            "content": _CITATION.sub("", m.content) if m.role == Message.Role.ASSISTANT else m.content,
        }
        async for m in recent
    ]
    history.reverse()
    await Message.objects.acreate(conversation=conversation, role=Message.Role.USER, content=text)

    if faq_hit:
        sources = [{"n": 1, "kind": "faq", "document": "FAQ", "document_id": f"faq-{faq_hit.faq.pk}",
                    "chunk_id": None, "index": 0, "question": faq_hit.faq.question,
                    "score": round(faq_hit.score, 3)}]
        top_score, passage_count = faq_hit.score, 1
    else:
        sources = [p.as_source(n) for n, p in enumerate(passages, 1)]
        top_score = max((p.score for p in passages), default=passages.best_score)
        passage_count = len(passages)
    url = reverse("chat:conversation", kwargs={"tenant_slug": tenant.slug, "pk": conversation.pk})

    async def save_reply(content, thinking, reply_model, outcome, prompt_tokens=None, completion_tokens=None):
        if not content and not thinking:
            return None
        if outcome == Message.Outcome.ANSWERED and content.strip() in (
            conf.fill(cfg.no_answer_message, cfg).strip(), no_answer.strip()
        ):
            outcome = Message.Outcome.DECLINED  # passages were given, but the model found no answer in them
        if any(rewrite_tokens):  # the rewriting call counts toward usage too
            prompt_tokens = (prompt_tokens or 0) + rewrite_tokens[0]
            completion_tokens = (completion_tokens or 0) + rewrite_tokens[1]
        msg = await Message.objects.acreate(
            conversation=conversation, role=Message.Role.ASSISTANT, content=content,
            thinking=thinking, model=reply_model, sources=sources if content else [],
            response_ms=int((time.monotonic() - started) * 1000), search_query=search_query[:500],
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            outcome=outcome, retrieval_ms=retrieval_ms, passage_count=passage_count, top_score=top_score,
        )
        await Conversation.objects.filter(pk=conversation.pk).aupdate(updated_at=timezone.now())
        logger.info(
            "chat reply tenant=%s conversation=%s model=%s response_time=%.2fs "
            "input_tokens=%s output_tokens=%s total_tokens=%s",
            tenant.slug, conversation.pk, reply_model or "-", msg.response_ms / 1000,
            msg.prompt_tokens, msg.completion_tokens, msg.total_tokens,
        )
        return msg

    meta = {"type": "meta", "conversation_id": conversation.pk, "title": conversation.title, "url": url}

    async def fixed_reply(message, outcome):
        # An FAQ answer, or nothing relevant found: reply without consulting the model.
        yield _ndjson(meta)
        yield _ndjson({"type": "sources", "sources": sources if outcome == Message.Outcome.FAQ else []})
        yield _ndjson({"type": "content", "text": message})
        msg = await save_reply(message, "", "", outcome)
        if outcome == Message.Outcome.FAQ:
            await faq.record_hit(faq_hit.faq)
        yield _ndjson({"type": "done", "message_id": msg.pk, "grounded": False,
                       "faq": outcome == Message.Outcome.FAQ, "stats": msg.stats()})

    token_policy = tenant.token_policy

    async def model_reply():
        messages = (
            [{"role": "system", "content": grounding_prompt(cfg, no_answer)}]
            + history
            + [{"role": "user", "content": grounded_question(text, passages, lang)}]
        )
        content, thinking = [], []
        yield _ndjson(meta)
        yield _ndjson({"type": "sources", "sources": sources})
        try:
            async for event in llm.stream_chat(model, messages, temperature=cfg.temperature,
                                               max_tokens=token_policy.max_output_tokens):
                if event["type"] == "content":
                    content.append(event["text"])
                elif event["type"] == "thinking":
                    thinking.append(event["text"])
                elif event["type"] == "done":
                    full = "".join(content)
                    cleaned = strip_trailing_no_answer(full, (no_answer, conf.fill(cfg.no_answer_message, cfg)))
                    if cleaned != full:
                        content[:] = [cleaned]
                        yield _ndjson({"type": "replace", "text": cleaned})  # fix what the user sees
                    msg = await save_reply(
                        "".join(content), "".join(thinking), model, Message.Outcome.ANSWERED,
                        event.get("prompt_tokens"), event.get("completion_tokens"),
                    )
                    event["message_id"] = msg.pk if msg else None
                    event["grounded"] = True
                    event["stats"] = msg.stats() if msg else None
                yield _ndjson(event)
        except asyncio.CancelledError:
            # Client disconnected (e.g. pressed Stop): keep what was generated so far.
            await save_reply("".join(content), "".join(thinking), model, Message.Outcome.STOPPED)
            raise
        except llm.LLMError as exc:
            await save_reply("".join(content), "".join(thinking), model, Message.Outcome.ERROR)
            yield _ndjson({"type": "error", "error": str(exc)})

    unavailable = {
        retrieval.KB.NO_DOCUMENTS: conf.fill(cfg.empty_kb_message, cfg),
        retrieval.KB.NO_EMBEDDING_MODEL: conf.fill(cfg.no_embedding_message, cfg),
        retrieval.KB.NEEDS_REINDEX: conf.fill(cfg.needs_reindex_message, cfg),
    }
    usage = await usage_lib.current(tenant) if passages and not faq_hit else None
    if small_talk:
        events = fixed_reply(persona.reply(small_talk, cfg, lang), Message.Outcome.SMALL_TALK)
    elif faq_hit:
        events = fixed_reply(faq_hit.faq.answer, Message.Outcome.FAQ)
    elif passages and usage.exhausted:
        reset = date_format(usage.resets_at, "DATETIME_FORMAT" if token_policy.period == "day" else "DATE_FORMAT")
        events = fixed_reply(conf.fill(cfg.token_limit_message, cfg).replace("{reset}", reset), Message.Outcome.LIMITED)
    elif passages:
        events = model_reply()
    elif kb_status in unavailable and not faqs:
        events = fixed_reply(unavailable[kb_status], Message.Outcome.UNAVAILABLE)
    else:
        events = fixed_reply(no_answer, Message.Outcome.NOT_FOUND)

    response = StreamingHttpResponse(events, content_type="application/x-ndjson")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # disable proxy buffering (nginx)
    return response
