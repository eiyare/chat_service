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
from widget import student_tools as student_tools_lib
from siteconfig import conf
from tenants.decorators import tenant_required

from . import persona
from . import rewrite as rewrite_lib
from . import usage as usage_lib
from .models import Conversation, Message
from .trace import Trace, excerpt

# Rounds in which the model may ask for tools before it must answer.
MAX_TOOL_ROUNDS = 4

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
    return [c async for c in qs[: (await conf.aget()).sidebar_limit]]


async def _render_chat(request, conversation=None):
    tenant = request.tenant
    messages = []
    if conversation is not None:
        messages = [m.as_dict() async for m in conversation.messages.visible()]
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
_CITED = re.compile(r"\[(\d+)\]")

_KB_LABELS = {
    retrieval.KB.READY: "Ready",
    retrieval.KB.NO_DOCUMENTS: "No documents yet",
    retrieval.KB.NO_EMBEDDING_MODEL: "No embedding model enabled",
    retrieval.KB.NEEDS_REINDEX: "Documents need reindexing",
}


def _search_summary(passages, t: dict) -> str:
    if not t.get("searched"):
        return "No searchable passages"
    if passages:
        return (f"{len(passages)} passage{'s' if len(passages) > 1 else ''} kept from {t['searched']} searched "
                f"(best similarity {t['best_score']:.2f})")
    best = t.get("best_score")
    return (f"Nothing relevant enough in {t['searched']} passages"
            + (f" (best similarity {best:.2f}, cut-off {t['min_score']:.2f})" if best is not None else ""))


def _search_detail(t: dict) -> dict:
    """The search trace, as brain-map step details."""
    if not t:
        return {}
    strong = t.get("strong_at")
    return {
        "thresholds": {
            "relevance cut-off (meaning)": round(t["min_score"], 3),
            "strong match (skips key-word check)": round(strong, 3) if strong not in (None, float("inf")) else "off",
            "key-word route coverage": t["keyword_min_coverage"],
            "key-word check coverage": t["min_term_overlap"],
            "passages per answer": t["top_k"],
        },
        "found": {
            "by meaning": t["by_meaning"], "by key words": t["by_keywords"],
            "from the page being viewed": t["page_passages"], "key-word coverage reached": t.get("key_word_coverage"),
        },
        "candidates": t.get("candidates"),
    }


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


def grounded_question(question: str, passages, lang: str = "en", place=None, today=None, tools=False) -> str:
    """The user's question wrapped with the retrieved passages it must be answered from, and,
    for a Moodle student (``place``), the course and page they are on and today's date."""
    blocks = [f"[{n}] (source: {p.document})\n{p.text}" for n, p in enumerate(passages, 1)]
    situation = []
    if place is not None and (place.course or place.page):
        if place.course:
            situation.append(f'The student is in the course "{place.course}".')
        if place.page:
            situation.append(f'They are viewing "{place.page}": "this module", "this page" or "this activity" '
                             "means that one.")
        if today is not None:
            situation.append(f"Today is {date_format(today, 'l j F Y')}.")
    instruction = ("Use the conversation so far to work out what the question refers to, "
                   "but take facts only from the context passages above.")
    if tools:
        instruction = ("Use the conversation so far to work out what the question refers to. Take facts only "
                       "from the context passages and from what the tools return; use the tools for anything "
                       "about the student's own grades, completion or deadlines, or to search the course.")
        if not blocks:
            blocks = ["(No course passages matched the question; use the tools.)"]
    if lang == "pcm":
        instruction += " The question is in Nigerian Pidgin English: answer in Nigerian Pidgin English."
    context = ("\n".join(situation) + "\n\n") if situation else ""
    return ("Context passages:\n\n" + "\n\n".join(blocks)
            + f"\n\n{context}Question: {question}\n\n{instruction}")


@require_POST
@tenant_required
async def api_send(request):
    return await respond(request, request.tenant, user=request.user)


async def respond(request, tenant, source=Conversation.Source.APP, visitor_id="", user=None, course=None,
                  activity=None, student_tools=None):
    """Answer a question strictly from the workspace's documents, streamed as NDJSON.

    Shared by the workspace chat and the website widget (source/visitor_id scope
    which conversations the caller may continue; the widget always uses the
    default model). ``course``: a student on a Moodle course's pages, answered
    from that course's content only (no workspace documents or FAQs); ``activity``: the
    course module they are viewing, whose passages are always included; ``student_tools``:
    their Moodle lookups (widget.student_tools.StudentTools), which make the model an agent
    that can look up their own grades, completion and deadlines and search the course.

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
    trace = Trace()  # the brain map of this reply
    small_talk = persona.intent(text, name, cfg.small_talk)
    lang = persona.language(text)  # reply in Nigerian Pidgin when asked in Pidgin
    no_answer = persona.no_answer(cfg, lang)
    search = persona.search_text(text, name)
    trace.add("question", "Question received", "info", text,
              language="Nigerian Pidgin" if lang == "pcm" else "English",
              searched_as=search if search != text else None, model=model,
              channel=Conversation.Source(source).label, history_limit=cfg.history_limit)
    trace.add("small_talk", "Small talk check", "hit" if small_talk else "pass",
              f"Recognised as {small_talk.replace('_', ' ')}: answered with the persona's reply" if small_talk
              else ("Not small talk" if cfg.small_talk else "Small-talk replies are switched off"))
    queries = [search]
    previous_text = previous_answer = None
    if conversation is not None and not small_talk:
        previous = await conversation.messages.filter(role=Message.Role.USER).alast()
        if previous:
            # Follow-ups ("and the second one?") need the earlier question for context,
            # and replies to an offer ("yes") the assistant's answer too.
            previous_text = persona.search_text(previous.content, name)
            queries.append(f"{previous_text}\n{search}")
            answer = await (conversation.messages.filter(role=Message.Role.ASSISTANT, id__gt=previous.id)
                            .exclude(content="").alast())
            previous_answer = _CITATION.sub("", answer.content) if answer else None
    place = await retrieval.moodle_place(tenant, course, activity)
    kb_status = await retrieval.kb_status(tenant, course)
    faqs = [] if course is not None else await faq.enabled_for(tenant)
    trace.add("context", "Context", "info",
              "Follow-up to an earlier question" if previous_text else
              ("First question in this conversation" if conversation is None else "No earlier question to build on"),
              earlier_question=previous_text, earlier_answer=excerpt(previous_answer) if previous_answer else None,
              moodle_course=(place.course or f"#{course}") if course is not None else None,
              moodle_page=place.page or (f"activity #{activity}" if activity else None),
              searching="this course's content only" if course is not None else "the workspace's documents and FAQs",
              knowledge_base=_KB_LABELS.get(kb_status, kb_status), faqs=len(faqs) if course is None else None)
    passages = retrieval.Results()
    faq_hit = None
    retrieval_ms = None
    search_started = time.monotonic()
    search_trace = {}

    # 1. Same question as an FAQ (ignoring case/punctuation): answer it, no embedding needed.
    faq_hit = None if small_talk else faq.direct_match(search, None, faqs)
    if small_talk:
        trace.add("faq_exact", "FAQ: same question", "skip", "Small talk")
    elif not faqs:
        trace.add("faq_exact", "FAQ: same question", "skip",
                  "FAQs aren't used on Moodle course pages" if course is not None else "The workspace has no FAQs")
    else:
        trace.add("faq_exact", "FAQ: same question", "hit" if faq_hit else "miss",
                  f'Same wording as the FAQ "{faq_hit.faq.question}"' if faq_hit
                  else f"No FAQ asked in the same words ({len(faqs)} checked)")
    search_query, rewrite_tokens = search, (0, 0)
    exhausted = (not small_talk and faq_hit is None and kb_status == retrieval.KB.READY and cfg.rewrite_queries
                 and (await usage_lib.current(tenant)).exhausted)
    skip_rewrite = ("Small talk" if small_talk else "Answered by an FAQ" if faq_hit else
                    f"Nothing to search: {_KB_LABELS.get(kb_status, kb_status).lower()}" if kb_status != retrieval.KB.READY
                    else "Switched off for this workspace" if not cfg.rewrite_queries else
                    "The workspace's token allowance is used up" if exhausted else None)
    if skip_rewrite:
        trace.add("rewrite", "Question rewriting", "skip", skip_rewrite)
    else:
        # Turn the question into a plain-English search query (Pidgin, paraphrases, follow-ups).
        t = trace.clock()
        rewritten, *rewrite_tokens = await rewrite_lib.rewrite(model, search, previous_text, cfg,
                                                               answer=previous_answer, page=place.page)
        given = rewrite_lib.rewrite_input(search, previous_text, previous_answer, place.page)
        if rewritten:
            search_query = rewritten
            queries = [rewritten] + queries
        trace.add("rewrite", "Question rewriting", "pass" if rewritten else "fail",
                  f'Searching for "{rewritten}"' if rewritten else "No usable rewrite: searching with the question as asked",
                  trace.since(t), given_to_model=given, rewritten=rewritten,
                  tokens=f"{rewrite_tokens[0]} in / {rewrite_tokens[1]} out" if any(rewrite_tokens) else None)
    original = queries.index(search)  # the user's own wording, for FAQ matching
    if (not small_talk and faq_hit is None and (faqs or kb_status == retrieval.KB.READY)
            and llm_config.embed_model):
        try:
            # The questions are embedded once, for FAQ matching and document search alike.
            t = trace.clock()
            query_vectors = await retrieval.embed_queries(queries)
            trace.add("embed", "Embedding", "pass", f"{len(queries)} search text{'s' if len(queries) > 1 else ''} "
                      f"turned into vectors with {llm_config.embed_model}", trace.since(t), searched_with=queries)
            indexed = await faq.index(faqs, llm_config) if faqs else []
            # 2. Close FAQ match (wording and meaning): answer it without the model.
            faq_hit = faq.direct_match(search, query_vectors[original], indexed, llm_config.faq_cutoff)
            if indexed:
                trace.add("faq_close", "FAQ: close match", "hit" if faq_hit else "miss",
                          f'Matches the FAQ "{faq_hit.faq.question}" (similarity {faq_hit.score:.2f})' if faq_hit
                          else "No FAQ close enough in wording and meaning",
                          cutoff=llm_config.faq_cutoff, phrasing=faq_hit.phrasing if faq_hit else None)
            if faq_hit is None:
                # 3. Document search, plus close FAQs as extra context for the model.
                if kb_status == retrieval.KB.READY:
                    t = trace.clock()
                    passages = await retrieval.search(tenant, queries, query_vectors=query_vectors, course=course,
                                                      page_document=place.page_document_id, trace=search_trace)
                    trace.add("search", "Document search", "pass" if passages else "miss",
                              _search_summary(passages, search_trace), trace.since(t), **_search_detail(search_trace))
                else:
                    trace.add("search", "Document search", "skip", _KB_LABELS.get(kb_status, kb_status))
                faq_context = faq.context_passages(query_vectors[original], indexed, llm_config.faq_context_cutoff)
                if faq_context:
                    best = passages.best_score
                    passages = retrieval.Results(faq_context + list(passages))
                    passages.best_score = best
                    trace.add("faq_context", "FAQs as context", "pass",
                              f"{len(faq_context)} related FAQ{'s' if len(faq_context) > 1 else ''} added for the model",
                              faqs=[p.document for p in faq_context], cutoff=llm_config.faq_context_cutoff)
        except llm.LLMError as exc:
            return _json_error(f"Document search failed: {exc.public()}", status=503)
        retrieval_ms = int((time.monotonic() - search_started) * 1000)
    elif not small_talk and faq_hit is None and not llm_config.embed_model:
        trace.add("search", "Document search", "skip", "No embedding model is enabled")

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

    # Failed replies are recorded with no content; they aren't part of the conversation.
    recent = conversation.messages.exclude(content="").order_by("-created_at", "-id")[: max(cfg.history_limit - 1, 0)]
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

    async def save_reply(content, thinking, reply_model, outcome, prompt_tokens=None, completion_tokens=None,
                         error=None):
        if not content and not thinking and error is None:
            return None
        if outcome == Message.Outcome.ANSWERED and content.strip() in (
            conf.fill(cfg.no_answer_message, cfg).strip(), no_answer.strip()
        ):
            outcome = Message.Outcome.DECLINED  # passages were given, but the model found no answer in them
            trace.add("declined", "Answer check", "miss",
                      "The model replied that the passages don't answer the question")
        response_ms = int((time.monotonic() - started) * 1000)
        trace.add("reply", "Reply", "fail" if outcome == Message.Outcome.ERROR else "info",
                  Message.Outcome(outcome).label, response_ms)
        if any(rewrite_tokens):  # the rewriting call counts toward usage too
            prompt_tokens = (prompt_tokens or 0) + rewrite_tokens[0]
            completion_tokens = (completion_tokens or 0) + rewrite_tokens[1]
        msg = await Message.objects.acreate(
            conversation=conversation, role=Message.Role.ASSISTANT, content=content,
            thinking=thinking, model=reply_model, sources=sources if content else [],
            response_ms=response_ms, search_query=search_query[:500], trace=trace.steps,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            outcome=outcome, retrieval_ms=retrieval_ms, passage_count=passage_count, top_score=top_score,
            error=error.summary() if error else "",
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

    def debug_event(outcome, attempts=None, error=None):
        """With DEBUG on: how the reply was produced, for the browser console and the log."""
        info = {
            "outcome": outcome, "model": model, "small_talk": small_talk, "language": lang,
            "search_query": search_query, "queries": queries, "knowledge_base": kb_status,
            "best_score": passages.best_score if not faq_hit else None, "passages": passage_count,
            "faq": {"question": faq_hit.faq.question, "score": round(faq_hit.score, 3)} if faq_hit else None,
            "retrieval_ms": retrieval_ms, "history_messages": len(history), "attempts": attempts,
            "error": error.summary() if error else None,
        }
        if course is not None:
            info["moodle"] = {"course": course, "activity": activity, "course_name": place.course,
                              "page": place.page}
        logger.debug("chat reply details tenant=%s conversation=%s %s", tenant.slug, conversation.pk,
                     json.dumps(info, default=str))
        return _ndjson({"type": "debug", "debug": info})

    async def fixed_reply(message, outcome):
        # An FAQ answer, or nothing relevant found: reply without consulting the model.
        yield _ndjson(meta)
        yield _ndjson({"type": "sources", "sources": sources if outcome == Message.Outcome.FAQ else []})
        yield _ndjson({"type": "content", "text": message})
        msg = await save_reply(message, "", "", outcome)
        if outcome == Message.Outcome.FAQ:
            await faq.record_hit(faq_hit.faq)
        if settings.DEBUG:
            yield debug_event(outcome)
        yield _ndjson({"type": "done", "message_id": msg.pk, "grounded": False,
                       "faq": outcome == Message.Outcome.FAQ, "stats": msg.stats()})

    token_policy = tenant.token_policy

    use_tools = student_tools is not None and course is not None

    async def run_tool(call) -> str:
        """Run one tool call for the model; returns what the model is given back. The brain map
        records what was looked up and how it went, never the student's data."""
        name = call["name"]
        try:
            args = json.loads(call["arguments"] or "{}")
            args = args if isinstance(args, dict) else {}
        except ValueError:
            args = {}
        t = trace.clock()
        if name == "search_course":
            query = str(args.get("query") or "").strip()[:300]
            if not query:
                trace.add("tool", "Tool: search_course", "fail", "No search words given", trace.since(t))
                return json.dumps({"error": "Give the words to search for."})
            try:
                found = await retrieval.search(tenant, [query], course=course)
            except llm.LLMError as exc:
                trace.add("tool", "Tool: search_course", "fail", str(exc), trace.since(t), query=query)
                return json.dumps({"error": "The course search failed."})
            known = {p.chunk_id for p in passages}
            new = [p for p in found if p.chunk_id not in known]
            sources.extend(p.as_source(n) for n, p in enumerate(new, len(passages) + 1))
            passages.extend(new)
            number = {p.chunk_id: n for n, p in enumerate(passages, 1)}
            trace.add("tool", "Tool: search_course", "pass" if found else "miss",
                      f'"{query}": {len(found)} passage{"s" if len(found) != 1 else ""}', trace.since(t), query=query,
                      passages=[f"[{number[p.chunk_id]}] {p.document} (similarity {p.score:.2f})" for p in found])
            return "\n\n".join(f"[{number[p.chunk_id]}] (source: {p.document})\n{p.text}" for p in found) \
                or "No passages found."
        if name in student_tools_lib.MOODLE_TOOLS:
            data, ms = await student_tools.lookup(name)
            inner = data.get("data") if isinstance(data, dict) else None
            failed = not isinstance(data, dict) or data.get("error") or (isinstance(inner, dict) and inner.get("error"))
            trace.add("tool", f"Tool: {name}", "fail" if failed else "pass", student_tools_lib.outline(name, data), ms,
                      note="The student's data went to the model only; it isn't stored here.")
            return student_tools_lib.result_text(data)
        trace.add("tool", f"Tool: {name}", "fail", "The model asked for a tool that doesn't exist", 0)
        return json.dumps({"error": f"There is no tool called {name}."})

    async def model_reply():
        system = grounding_prompt(cfg, no_answer)
        if use_tools:
            system += "\n\n" + conf.fill(cfg.student_tools_prompt, cfg)
        question = grounded_question(text, passages, lang, place, timezone.localdate(), tools=use_tools)
        messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": question}]
        tools = student_tools_lib.DEFINITIONS if use_tools else None
        trace.add("prompt", "Prompt", "pass",
                  f"{len(passages)} passage{'s' if len(passages) != 1 else ''} and {len(history)} earlier "
                  f"message{'s' if len(history) != 1 else ''} sent to {llm_config.label(model)}"
                  + (", with student data tools" if tools else ""),
                  grounding_mode=cfg.grounding_mode, temperature=cfg.temperature,
                  max_output_tokens=token_policy.max_output_tokens, retries_allowed=cfg.provider_retries,
                  tools=[d["function"]["name"] for d in tools] if tools else None,
                  system_prompt=system, question_with_passages=question)
        model_started = trace.clock()
        content, thinking = [], []
        tokens = [None, None]  # input, output: summed over the rounds
        done = {}
        yield _ndjson(meta)
        yield _ndjson({"type": "sources", "sources": sources})
        try:
            for round_number in range(1, MAX_TOOL_ROUNDS + 2):
                round_started, round_from = trace.clock(), len(content)
                calls, done = None, {}
                try:
                    async for event in llm.stream_chat(model, messages, temperature=cfg.temperature,
                                                       max_tokens=token_policy.max_output_tokens,
                                                       retries=cfg.provider_retries, tools=tools):
                        if event["type"] == "content":
                            content.append(event["text"])
                            yield _ndjson(event)
                        elif event["type"] == "thinking":
                            thinking.append(event["text"])
                            yield _ndjson(event)
                        elif event["type"] == "tool_calls":
                            calls = event["calls"]
                        elif event["type"] == "done":
                            done = event
                except llm.LLMError as exc:
                    if tools and round_number == 1 and exc.kind == "bad_request" and not exc.partial:
                        # The model (or its provider) doesn't take tools: answer as before.
                        trace.add("model", "Model: tools", "fail",
                                  "The model rejected the tools, so it answers without student data",
                                  trace.since(round_started), provider_error=exc.detail)
                        tools = None
                        messages[0]["content"] = grounding_prompt(cfg, no_answer)
                        continue
                    raise
                for i, key in enumerate(("prompt_tokens", "completion_tokens")):
                    if done.get(key) is not None:
                        tokens[i] = (tokens[i] or 0) + done[key]
                if not (calls and tools):
                    break
                if round_number > MAX_TOOL_ROUNDS:  # still asking for tools: stop with what it has
                    trace.add("model", "Model: tools", "fail", f"Still asking for tools after {MAX_TOOL_ROUNDS} rounds")
                    break
                said = "".join(content[round_from:])
                trace.add("model", f"Model round {round_number}", "pass",
                          "Asked for " + ", ".join(c["name"] for c in calls), trace.since(round_started),
                          tokens=f"{done.get('prompt_tokens')} in / {done.get('completion_tokens')} out",
                          said_first=said or None)
                messages.append({"role": "assistant", "content": said or None, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                    for c in calls]})
                for c in calls:
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": await run_tool(c)})
                yield _ndjson({"type": "sources", "sources": sources})
                if said:
                    content.append("\n\n")
                    yield _ndjson({"type": "content", "text": "\n\n"})
            done = {**done, "type": "done", "prompt_tokens": tokens[0], "completion_tokens": tokens[1]}
            trace.add("model", "Model answer", "pass",
                      f"{llm_config.label(model)} answered"
                      + (f" after {done['attempts']} attempts" if done.get("attempts", 1) > 1 else ""),
                      trace.since(model_started),
                      tokens=f"{tokens[0]} in / {tokens[1]} out",
                      generation_time=f"{done['duration_ms']} ms" if done.get("duration_ms") is not None else None,
                      attempts=done.get("attempts") if done.get("attempts", 1) > 1 else None,
                      reasoning=excerpt("".join(thinking), 4000) if thinking else None)
            full = "".join(content)
            cleaned = strip_trailing_no_answer(full, (no_answer, conf.fill(cfg.no_answer_message, cfg)))
            cited = sorted({int(n) for n in _CITED.findall(cleaned) if 0 < int(n) <= len(passages)})
            trace.add("checks", "Answer checks", "pass",
                      "Cites " + ", ".join(f"[{n}] {passages[n - 1].document}" for n in cited) if cited
                      else "No passages cited",
                      trailing_no_answer_removed="yes" if cleaned != full else None,
                      uncited_passages=[f"[{n}] {p.document}" for n, p in enumerate(passages, 1)
                                        if n not in cited] or None)
            if cleaned != full:
                content[:] = [cleaned]
                yield _ndjson({"type": "replace", "text": cleaned})  # fix what the user sees
            msg = await save_reply("".join(content), "".join(thinking), model, Message.Outcome.ANSWERED,
                                   tokens[0], tokens[1])
            done["message_id"] = msg.pk if msg else None
            done["grounded"] = True
            done["stats"] = msg.stats() if msg else None
            if settings.DEBUG:
                yield debug_event(msg.outcome if msg else Message.Outcome.ANSWERED, done.get("attempts"))
            yield _ndjson(done)
        except asyncio.CancelledError:
            # Client disconnected (e.g. pressed Stop): keep what was generated so far.
            trace.add("model", "Model answer", "fail", "Stopped by the user before the answer was finished",
                      trace.since(model_started))
            await save_reply("".join(content), "".join(thinking), model, Message.Outcome.STOPPED)
            raise
        except llm.LLMError as exc:
            trace.add("model", "Model answer", "fail", str(exc), trace.since(model_started), kind=exc.kind,
                      attempts=exc.attempts, provider_error=exc.detail,
                      cut_off="yes: the partial answer was kept" if exc.partial else None)
            await save_reply("".join(content), "".join(thinking), model, Message.Outcome.ERROR, error=exc)
            if settings.DEBUG:
                yield debug_event(Message.Outcome.ERROR, exc.attempts, exc)
            yield _ndjson({"type": "error", "error": exc.public()})

    unavailable = {
        retrieval.KB.NO_DOCUMENTS: conf.fill(cfg.empty_kb_message, cfg),
        retrieval.KB.NO_EMBEDDING_MODEL: conf.fill(cfg.no_embedding_message, cfg),
        retrieval.KB.NEEDS_REINDEX: conf.fill(cfg.needs_reindex_message, cfg),
    }
    usage = await usage_lib.current(tenant) if (passages or use_tools) and not faq_hit else None

    def route(summary, status="pass"):
        trace.add("route", "Route", status, summary)

    if small_talk:
        route("Small talk: the persona's reply, no model call", "hit")
        events = fixed_reply(persona.reply(small_talk, cfg, lang), Message.Outcome.SMALL_TALK)
    elif faq_hit:
        route("FAQ answer, no model call", "hit")
        events = fixed_reply(faq_hit.faq.answer, Message.Outcome.FAQ)
    elif (passages or use_tools) and usage.exhausted:
        route("The workspace's token allowance is used up", "fail")
        reset = date_format(usage.resets_at, "DATETIME_FORMAT" if token_policy.period == "day" else "DATE_FORMAT")
        events = fixed_reply(conf.fill(cfg.token_limit_message, cfg).replace("{reset}", reset), Message.Outcome.LIMITED)
    elif passages or use_tools:
        route("Ask the model to answer, with the student data tools" + (" and the passages found" if passages else "")
              if use_tools else "Passages found: ask the model to answer from them")
        events = model_reply()
    elif kb_status in unavailable and not faqs:
        route(f"{_KB_LABELS[kb_status]}: fixed reply, no model call", "fail")
        events = fixed_reply(unavailable[kb_status], Message.Outcome.UNAVAILABLE)
    else:
        route("Nothing relevant found: the not-found reply, no model call", "miss")
        events = fixed_reply(no_answer, Message.Outcome.NOT_FOUND)

    response = StreamingHttpResponse(events, content_type="application/x-ndjson")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # disable proxy buffering (nginx)
    return response
