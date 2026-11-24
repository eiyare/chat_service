"""Brain map page helpers: which conversations a workspace admin may inspect, and each
reply's trace (chat/trace.py) turned into display rows."""
import re

from django.db.models import Count, Q

from chat.models import Conversation, Message

STATUS_LABELS = {"pass": "Passed on", "hit": "Answered here", "miss": "Nothing found", "skip": "Skipped",
                 "fail": "Problem", "info": "Context"}
_MOODLE = re.compile(r"^moodle-user-0*(\d+)(?:-c(\d+))?$")
_APP = re.compile(r"^app\.(\d+)\.(.+?)(?:\.s[0-9a-f]{12})?$")
LONG_TEXT = 160


def conversations(tenant, user):
    """Conversations the admin may open: every website-widget conversation (visitors and
    Moodle students) and their own workspace chats. Other members' workspace chats stay
    private, as in the chat itself."""
    return Conversation.objects.filter(tenant=tenant).filter(
        Q(source=Conversation.Source.WIDGET) | Q(source=Conversation.Source.APP, user=user)
    )


def with_counts(qs):
    assistant = Q(messages__role=Message.Role.ASSISTANT)
    return qs.annotate(
        questions=Count("messages", filter=Q(messages__role=Message.Role.USER)),
        answered=Count("messages", filter=assistant & Q(messages__outcome__in=["answered", "faq", "small_talk"])),
        missed=Count("messages", filter=assistant & Q(messages__outcome__in=["not_found", "declined"])),
        failed=Count("messages", filter=assistant & Q(messages__outcome__in=["error", "limited", "unavailable"])),
    )


def who(conversation) -> str:
    if conversation.source == Conversation.Source.APP:
        return "Workspace chat (yours)"
    m = _MOODLE.match(conversation.visitor_id or "")
    if m:
        return f"Moodle user {m.group(1)}" + (f" · course {m.group(2)}" if m.group(2) else "")
    m = _APP.match(conversation.visitor_id or "")
    if m:
        from integrations.models import Integration

        app = Integration.objects.filter(pk=m.group(1)).values_list("name", flat=True).first()
        return f"{app or 'App'} user {m.group(2)}"
    return "Website visitor"


def fmt_ms(ms) -> str:
    if ms is None:
        return ""
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


LABELS = {"faqs": "FAQs", "faq": "FAQ", "moodle_course": "Moodle course", "moodle_page": "Moodle page"}


def _label(key: str) -> str:
    if key in LABELS:
        return LABELS[key]
    key = key.replace("_", " ")
    return key[:1].upper() + key[1:]


def _rows(detail: dict) -> list[dict]:
    """Step details as display rows: kind is "pair", "text" (long), "list", "pairs" or "candidates"."""
    rows = []
    for key, value in detail.items():
        label = _label(key)
        if key == "candidates" and isinstance(value, list):
            rows.append({"kind": "candidates", "label": label, "items": value})
        elif isinstance(value, dict):
            pairs = [(_label(k), v) for k, v in value.items() if v not in (None, "")]
            if pairs:
                rows.append({"kind": "pairs", "label": label, "pairs": pairs})
        elif isinstance(value, list):
            rows.append({"kind": "list", "label": label, "items": [str(v) for v in value]})
        elif isinstance(value, str) and (len(value) > LONG_TEXT or "\n" in value):
            rows.append({"kind": "text", "label": label, "text": value})
        else:
            rows.append({"kind": "pair", "label": label, "value": value})
    return rows


def steps(message: Message) -> tuple[list[dict], bool]:
    """(display steps, recorded) for an assistant reply. Replies saved before brain maps
    were recorded get an outline rebuilt from their saved metrics (recorded=False)."""
    raw = message.trace or _rebuild(message)
    total = message.response_ms or sum(s.get("ms") or 0 for s in raw) or 0
    shown = []
    for n, s in enumerate(raw, 1):
        ms = s.get("ms")
        timed = ms is not None and s.get("key") != "reply"
        shown.append({
            **s, "n": n, "status_label": STATUS_LABELS.get(s.get("status"), ""), "time": fmt_ms(ms),
            "rows": _rows(s.get("detail") or {}),
            "share": round(100 * ms / total, 1) if timed and total else None,
        })
    return shown, bool(message.trace)


def _rebuild(m: Message) -> list[dict]:
    out = []

    def add(key, label, status, summary="", ms=None, **detail):
        out.append({"key": key, "label": label, "status": status, "summary": summary, "ms": ms,
                    "detail": {k: v for k, v in detail.items() if v not in (None, "", [], {})}})

    if m.search_query:
        add("rewrite", "Searched for", "info", f'"{m.search_query}"')
    if m.outcome in ("small_talk", "faq"):
        add("route", "Route", "hit", Message.Outcome(m.outcome).label + ", no model call")
    elif m.passage_count is not None or m.top_score is not None:
        found = m.passage_count or 0
        add("search", "Document search", "pass" if found else "miss",
            f"{found} passage{'s' if found != 1 else ''} found"
            + (f" (best similarity {m.top_score:.2f})" if m.top_score is not None else ""),
            m.retrieval_ms,
            passages=[f"[{s.get('n')}] {s.get('document')} (similarity {s.get('score')})" for s in m.sources or []])
    if m.model:
        add("model", "Model answer", "fail" if m.outcome == "error" else "pass", m.model,
            tokens=f"{m.prompt_tokens} in / {m.completion_tokens} out" if m.prompt_tokens is not None else None,
            error=m.error or None)
    add("reply", "Reply", "fail" if m.outcome == "error" else "info",
        Message.Outcome(m.outcome).label if m.outcome else "", m.response_ms)
    return out
