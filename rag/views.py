"""Workspace admin page: knowledge-base report, uploads and document list."""
from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

import llm
from tenants.decorators import tenant_required

from . import services
from .models import Document, IndexingRun, embed_signature


@require_GET
@tenant_required(admin=True)
async def admin_home(request):
    tenant = request.tenant
    documents = [d async for d in Document.objects.filter(tenant=tenant)]
    llm_config = await llm.aget_config()
    policy = tenant.upload_policy
    signature = embed_signature(llm_config)
    latest = {}
    async for run in IndexingRun.objects.filter(tenant=tenant, document__isnull=False).order_by("-created_at").values(
        "document_id", "total_ms", "status"
    ):
        latest.setdefault(run["document_id"], run)
    for d in documents:
        run = latest.get(d.pk)
        d.indexed_in = _secs(run["total_ms"]) if run and run["status"] == "ready" else "–"
        # Failed, or embedded with settings that no longer apply: offer a rebuild.
        d.needs_rebuild = d.status == Document.Status.FAILED or (
            d.status == Document.Status.READY and d.embed_signature != signature
        )
    return render(
        request,
        "rag/admin.html",
        {
            "tenant": tenant,
            "documents": documents,
            "report": await services.report(tenant),
            "accept": ",".join(f".{e}" for e in policy.extensions),
            "allowed_types": ", ".join(e.upper() for e in policy.extensions),
            "max_upload_mb": policy.max_file_mb,
            "max_documents": policy.max_documents,
            "slots_left": max(0, policy.max_documents - len(documents)),
            "embed_model": llm_config.embed_model or "no embedding model configured",
            "no_embedding_model": not llm_config.embed_model,
        },
    )


@require_POST
@tenant_required(admin=True)
async def upload(request):
    tenant = request.tenant
    policy = tenant.upload_policy
    files = request.FILES.getlist("files")
    if not files:
        messages.error(request, "Choose at least one file to upload.")
        return redirect("rag:home", tenant_slug=tenant.slug)
    try:
        remaining = await services.check_capacity(tenant, policy)
    except services.UploadRejected as exc:
        messages.error(request, str(exc))
        return redirect("rag:home", tenant_slug=tenant.slug)
    if len(files) > remaining:
        messages.error(
            request,
            f"You selected {len(files)} files, but this workspace has room for {remaining} more "
            f"(limit {policy.max_documents}). Nothing was uploaded.",
        )
        return redirect("rag:home", tenant_slug=tenant.slug)
    for f in files:
        try:
            services.validate_upload(f.name, f.size, policy)  # reject before reading big files
            document = await services.ingest(tenant, f.name, f.read())
        except services.UploadRejected as exc:
            messages.error(request, str(exc))
            continue
        if document.status == Document.Status.READY:
            n = document.chunk_count
            messages.success(request, f"{document.name}: {n} chunk{'' if n == 1 else 's'} added.")
        else:
            messages.error(request, f"{document.name}: {document.error}")
    return redirect("rag:home", tenant_slug=tenant.slug)


@require_POST
@tenant_required(admin=True)
async def rebuild(request, pk):
    """Re-split and re-embed a document from its stored file."""
    try:
        document = await Document.objects.aget(pk=pk, tenant=request.tenant)
    except Document.DoesNotExist:
        raise Http404("Document not found")
    document = await services.process(document, trigger=IndexingRun.Trigger.REBUILD)
    if document.status == Document.Status.READY:
        n = document.chunk_count
        messages.success(request, f"{document.name}: rebuilt, {n} chunk{'' if n == 1 else 's'}.")
    else:
        messages.error(request, f"{document.name}: {document.error}")
    return redirect("rag:home", tenant_slug=request.tenant.slug)


@require_POST
@tenant_required(admin=True)
async def delete(request, pk):
    try:
        document = await Document.objects.aget(pk=pk, tenant=request.tenant)
    except Document.DoesNotExist:
        raise Http404("Document not found")
    name = document.name
    await services.delete_document(document)
    messages.success(request, f"Removed {name}.")
    return redirect("rag:home", tenant_slug=request.tenant.slug)


# --- Performance report ------------------------------------------------------------------


def _pct(x):
    return "–" if x is None else f"{round(x * 100)}%"


def _secs(ms):
    if ms is None:
        return "–"
    s = ms / 1000
    return f"{s:.1f} s" if s < 10 else f"{round(s)} s"


def _compact(n):
    if n is None:
        return "–"
    n = float(n)
    for div, suffix in ((1e6, "M"), (1e3, "K")):
        if abs(n) >= div * 10 if suffix == "K" else abs(n) >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{round(n):,}"


@require_GET
@tenant_required(admin=True)
async def performance(request):
    from chat import metrics

    tenant = request.tenant
    try:
        days = int(request.GET.get("days", 30))
    except ValueError:
        days = 30
    if days not in metrics.PERIODS:
        days = 30
    llm_config = await llm.aget_config()
    kb = await services.report(tenant)
    data = await metrics.performance(
        tenant, days, llm_config.embed_model,
        {"documents": kb.documents, "chunks": kb.chunks, "stale": kb.stale},
        llm_config.doc_cutoff,
    )
    k = data["kpis"]
    tiles = [
        {"label": "Questions", "value": _compact(k["questions"]),
         "sub": f"{k['model_calls_avoided']} without a model call"
                + (f", {k['widget_questions']} via website widget" if k["widget_questions"] else "")},
        {"label": "Answer rate", "value": _pct(k["answer_rate"]),
         "sub": f"{k['answered']} answered" + (f", {k['faq_answers']} from FAQs" if k["faq_answers"] else "")},
        {"label": "Retrieval hit rate", "value": _pct(k["retrieval_hit_rate"]),
         "sub": "searches that found relevant passages"},
        {"label": "Median response time", "value": _secs(k["response_p50_ms"]),
         "sub": f"95th percentile {_secs(k['response_p95_ms'])}"},
        {"label": "Tokens used", "value": _compact(k["tokens_in"] + k["tokens_out"]),
         "sub": f"{_compact(k['tokens_in'])} in / {_compact(k['tokens_out'])} out"},
    ]
    models = [
        {**m, "answer_rate": _pct(m["answer_rate"]), "response": _secs(m["response_p50_ms"]),
         "tokens_in": _compact(m["tokens_in_avg"]), "tokens_out": _compact(m["tokens_out_avg"]),
         "speed": "–" if m["tokens_per_second"] is None else f"{m['tokens_per_second']:.1f}",
         "label": llm_config.label(m["model"])}
        for m in data["models"]
    ]
    emb = data["embedding"]
    from chat import usage as usage_lib

    usage = await usage_lib.current(tenant)
    token_policy = tenant.token_policy
    allowance = {
        "used": _compact(usage.used),
        "limit": _compact(usage.allowance) if usage.allowance else None,
        "share": usage.share,
        "percent": None if usage.share is None else round(usage.share * 100),
        "state": "critical" if usage.exhausted else ("warning" if (usage.share or 0) >= 0.9 else "ok"),
        "period": usage.period,
        "resets_at": usage.resets_at,
        "max_output": _compact(token_policy.max_output_tokens) if token_policy.max_output_tokens else None,
    }
    proc = data["processing"]
    processing = {
        **proc,
        "total_p50": _secs(proc["total_p50_ms"]),
        "total_max": _secs(proc["total_max_ms"]),
        "extract_p50": _secs(proc["extract_p50_ms"]),
        "speed": "–" if proc["chunks_per_second"] is None else f"{proc['chunks_per_second']:.1f}",
        "chunks_display": _compact(proc["chunks"]),
        "recent": [
            {**r, "extract": _secs(r["extract_ms"]), "embed": _secs(r["embed_ms"]), "total": _secs(r["total_ms"])}
            for r in proc["recent"]
        ],
    }
    return render(request, "rag/performance.html", {
        "processing": processing,
        "allowance": allowance,
        "tenant": tenant,
        "data": data,
        "tiles": tiles,
        "models": models,
        "embedding": {**emb, "p50": _secs(emb["retrieval_p50_ms"]), "p95": _secs(emb["retrieval_p95_ms"])},
        "grounding": {
            "precision": _pct(k["grounded_precision"]),
            "citations": _pct(k["citation_rate"]),
            "top_score": "–" if k["top_score_p50"] is None else f"{k['top_score_p50']:.2f}",
            "tokens_in": _compact(k["tokens_per_answer_in"]),
            "tokens_out": _compact(k["tokens_per_answer_out"]),
        },
        "periods": [{"days": d, "label": label, "active": d == days} for d, label in metrics.PERIODS.items()],
        "period_label": metrics.PERIODS[days],
    })
