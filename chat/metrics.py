"""Retrieval and grounding performance metrics for a workspace's chat."""
import math
import re
from collections import Counter, defaultdict
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import Message

O = Message.Outcome
PERIODS = {7: "Last 7 days", 30: "Last 30 days", 90: "Last 90 days", 0: "All time"}
_CITATION = re.compile(r"\[\d+\]")
_SCORE_BIN = 0.05


def percentile(values, pct):
    """Nearest-rank percentile; None for no data."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = max(0, min(len(values) - 1, math.ceil(pct / 100 * len(values)) - 1))
    return values[k]


def _rate(part, whole):
    return part / whole if whole else None


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


async def performance(tenant, days: int, embed_model: str = "", kb: dict | None = None,
                      min_score: float | None = None) -> dict:
    """All metrics for the period (days=0 means all time), JSON-serialisable."""
    now = timezone.now()
    qs = Message.objects.filter(conversation__tenant=tenant, role=Message.Role.ASSISTANT).exclude(outcome="")
    if days:
        qs = qs.filter(created_at__gte=now - timedelta(days=days))
    rows = [m async for m in qs.order_by("created_at", "id").values(
        "id", "conversation_id", "conversation__source", "created_at", "outcome", "model", "response_ms", "retrieval_ms",
        "prompt_tokens", "completion_tokens", "top_score", "passage_count", "sources", "content",
    )]
    questions = await _questions_for(rows)
    result = compute(rows, questions, days, now, embed_model, kb or {},
                     min_score)
    result["processing"] = await processing(tenant, days, now)
    return result


async def processing(tenant, days: int, now) -> dict:
    """Document processing (upload / rebuild / reindex) statistics for the period."""
    from rag.models import IndexingRun

    qs = IndexingRun.objects.filter(tenant=tenant)
    if days:
        qs = qs.filter(created_at__gte=now - timedelta(days=days))
    runs = [r async for r in qs.order_by("-created_at").values(
        "document_name", "trigger", "status", "error", "size", "characters", "chunks",
        "extract_ms", "embed_ms", "total_ms", "embed_model", "created_at",
    )]
    return compute_processing(runs)


def compute_processing(runs) -> dict:
    ready = [r for r in runs if r["status"] == "ready"]
    timed = [r for r in ready if r["embed_ms"]]
    embed_seconds = sum(r["embed_ms"] for r in timed) / 1000
    return {
        "runs": len(runs),
        "uploads": sum(1 for r in runs if r["trigger"] == "upload"),
        "failed": sum(1 for r in runs if r["status"] == "failed"),
        "cancelled": sum(1 for r in runs if r["status"] == "cancelled"),
        "chunks": sum(r["chunks"] for r in ready),
        "bytes": sum(r["size"] for r in ready),
        "total_p50_ms": percentile([r["total_ms"] for r in ready], 50),
        "total_max_ms": max((r["total_ms"] for r in ready if r["total_ms"] is not None), default=None),
        "extract_p50_ms": percentile([r["extract_ms"] for r in ready], 50),
        "chunks_per_second": (sum(r["chunks"] for r in timed) / embed_seconds) if embed_seconds else None,
        "recent": [
            {**r, "date": timezone.localtime(r["created_at"]).strftime("%Y-%m-%d %H:%M")}
            for r in runs[:10]
        ],
    }


async def _questions_for(rows) -> dict:
    """assistant message id -> the user question it answered."""
    if not rows:
        return {}
    conv_ids = {r["conversation_id"] for r in rows}
    users = defaultdict(list)
    async for m in Message.objects.filter(conversation_id__in=conv_ids, role=Message.Role.USER).order_by("id").values(
        "id", "conversation_id", "content"
    ):
        users[m["conversation_id"]].append((m["id"], m["content"]))
    result = {}
    for r in rows:
        earlier = [c for i, c in users[r["conversation_id"]] if i < r["id"]]
        result[r["id"]] = earlier[-1] if earlier else ""
    return result


def compute(rows, questions, days, now, embed_model, kb, min_score=None) -> dict:
    if min_score is None:
        from siteconfig import conf
        min_score = conf.get().min_score
    total = len(rows)
    by = Counter(r["outcome"] for r in rows)
    answered = [r for r in rows if r["outcome"] == O.ANSWERED]
    # Questions that went through document search (FAQ answers and unavailable ones didn't).
    searched = [r for r in rows if r["outcome"] not in (O.UNAVAILABLE, O.FAQ, O.SMALL_TALK)]
    found = [r for r in searched if (r["passage_count"] or 0) > 0]
    model_calls = [r for r in rows if r["model"]]

    def gen_ms(r):
        if r["response_ms"] is None:
            return None
        return r["response_ms"] - (r["retrieval_ms"] or 0)

    prompt_total = sum(r["prompt_tokens"] or 0 for r in model_calls)
    completion_total = sum(r["completion_tokens"] or 0 for r in model_calls)

    kpis = {
        "questions": total,
        "answered": by[O.ANSWERED] + by[O.FAQ] + by[O.SMALL_TALK],
        "faq_answers": by[O.FAQ],
        "small_talk": by[O.SMALL_TALK],
        "answer_rate": _rate(by[O.ANSWERED] + by[O.FAQ] + by[O.SMALL_TALK], total),
        "retrieval_hit_rate": _rate(len(found), len(searched)),
        "grounded_precision": _rate(by[O.ANSWERED], by[O.ANSWERED] + by[O.DECLINED]),
        "citation_rate": _rate(sum(1 for r in answered if _CITATION.search(r["content"] or "")), len(answered)),
        "response_p50_ms": percentile([r["response_ms"] for r in answered], 50),
        "response_p95_ms": percentile([r["response_ms"] for r in answered], 95),
        "retrieval_p50_ms": percentile([r["retrieval_ms"] for r in searched], 50),
        "top_score_p50": percentile([r["top_score"] for r in found], 50),
        "tokens_in": prompt_total,
        "tokens_out": completion_total,
        "tokens_per_answer_in": _mean([r["prompt_tokens"] for r in answered]),
        "tokens_per_answer_out": _mean([r["completion_tokens"] for r in answered]),
        "model_calls": len(model_calls),
        "model_calls_avoided": total - len(model_calls),
        "widget_questions": sum(1 for r in rows if r.get("conversation__source") == "widget"),
    }

    outcomes = [
        {"key": key, "label": label, "count": by[key], "share": _rate(by[key], total)}
        for key, label in ((O.ANSWERED, "Answered by model"), (O.FAQ, "FAQ or small talk"),
                           (O.DECLINED, "Declined by model"),
                           (O.NOT_FOUND, "Not found in documents"), ("other", "Unavailable, limit or error"))
    ]
    outcomes[1]["count"] = by[O.FAQ] + by[O.SMALL_TALK]
    outcomes[1]["share"] = _rate(outcomes[1]["count"], total)
    outcomes[-1]["count"] = by[O.UNAVAILABLE] + by[O.LIMITED] + by[O.ERROR] + by[O.STOPPED]
    outcomes[-1]["share"] = _rate(outcomes[-1]["count"], total)

    # --- daily series ---------------------------------------------------------------
    if rows:
        start = timezone.localtime(now - timedelta(days=days - 1)).date() if days else \
            timezone.localtime(rows[0]["created_at"]).date()
    else:
        start = timezone.localtime(now - timedelta(days=(days or 7) - 1)).date()
    end = timezone.localtime(now).date()
    day_rows = defaultdict(list)
    for r in rows:
        day_rows[timezone.localtime(r["created_at"]).date()].append(r)
    # One bucket per day; long ranges use weeks so columns stay readable.
    step = 7 if (end - start).days + 1 > 92 else 1
    daily = []
    d = start
    while d <= end:
        rs = [r for i in range(step) for r in day_rows.get(d + timedelta(days=i), [])]
        ans = [r for r in rs if r["outcome"] == O.ANSWERED]
        daily.append({
            "date": d.isoformat(),
            "answered": len(ans),
            "faq": sum(1 for r in rs if r["outcome"] in (O.FAQ, O.SMALL_TALK)),
            "declined": sum(1 for r in rs if r["outcome"] == O.DECLINED),
            "not_found": sum(1 for r in rs if r["outcome"] == O.NOT_FOUND),
            "other": sum(1 for r in rs if r["outcome"] in (O.UNAVAILABLE, O.LIMITED, O.ERROR, O.STOPPED)),
            "p50_ms": percentile([r["response_ms"] for r in ans], 50),
            "p95_ms": percentile([r["response_ms"] for r in ans], 95),
        })
        d += timedelta(days=step)

    # --- best-passage score distribution (calibration) -----------------------------------
    bins = [round(i * _SCORE_BIN, 2) for i in range(int(1 / _SCORE_BIN))]
    hist = {b: {"answered": 0, "not_answered": 0} for b in bins}
    for r in searched:
        if r["top_score"] is None:
            continue
        # Small tolerance: 0.7 / 0.05 is 13.999... in floating point.
        b = bins[min(len(bins) - 1, max(0, int(r["top_score"] / _SCORE_BIN + 1e-9)))]
        hist[b]["answered" if r["outcome"] == O.ANSWERED else "not_answered"] += 1
    scored = [b for b in bins if hist[b]["answered"] or hist[b]["not_answered"]]
    lo = min(scored + [max(0.0, round(min_score - 0.1, 2))], default=0.3)
    hi = max(scored + [round(min_score + 0.1, 2)], default=0.9)
    score_hist = [{"bin": b, **hist[b]} for b in bins if lo - 1e-9 <= b <= hi + 1e-9]

    # --- documents used -------------------------------------------------------------------
    doc_uses = Counter()
    for r in answered:
        for name in {s.get("document") for s in (r["sources"] or []) if isinstance(s, dict)}:
            if name:
                doc_uses[name] += 1
    documents = [{"document": n, "answers": c} for n, c in doc_uses.most_common(6)]

    # --- per model -------------------------------------------------------------------------
    per_model = defaultdict(list)
    for r in model_calls:
        per_model[r["model"]].append(r)
    models = []
    for name, rs in sorted(per_model.items(), key=lambda kv: -len(kv[1])):
        ans = [r for r in rs if r["outcome"] == O.ANSWERED]
        speeds = [
            (r["completion_tokens"] or 0) / (gen_ms(r) / 1000)
            for r in ans if r["completion_tokens"] and gen_ms(r) and gen_ms(r) > 0
        ]
        models.append({
            "model": name,
            "calls": len(rs),
            "answer_rate": _rate(len(ans), len(rs)),
            "response_p50_ms": percentile([r["response_ms"] for r in ans], 50),
            "tokens_in_avg": _mean([r["prompt_tokens"] for r in rs]),
            "tokens_out_avg": _mean([r["completion_tokens"] for r in rs]),
            "tokens_per_second": _mean(speeds),
        })

    unanswered = [
        {
            "question": questions.get(r["id"], ""),
            "outcome": Message.Outcome(r["outcome"]).label,
            "top_score": r["top_score"],
            "date": timezone.localtime(r["created_at"]).date().isoformat(),
        }
        for r in reversed(rows) if r["outcome"] in (O.NOT_FOUND, O.DECLINED)
    ][:10]

    return {
        "period_days": days,
        "kpis": kpis,
        "outcomes": outcomes,
        "daily": daily,
        "bucket": "week" if step == 7 else "day",
        "score_hist": score_hist,
        "min_score": min_score,
        "documents": documents,
        "models": models,
        "embedding": {
            "model": embed_model,
            "retrieval_p50_ms": kpis["retrieval_p50_ms"],
            "retrieval_p95_ms": percentile([r["retrieval_ms"] for r in searched], 95),
            "searches": sum(1 for r in searched if r["retrieval_ms"] is not None),
            **kb,
        },
        "unanswered": unanswered,
    }
