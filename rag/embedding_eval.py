"""Embedding test: how well an embedding model finds the right passages.

Runs a fixed set of questions (rag/eval_cases/embeddings.json) against each
workspace's documents and reports, per question, what the live search would do
with this model and its cut-offs: find the right passage, return a wrong one,
or refuse. Off-topic questions should be refused, and FAQ rewordings should
match while different questions should not.

Nothing is saved. The model under test can be any embedding model row (enabled
or not), with optional unsaved overrides for its cut-offs and options. When a
workspace's documents are already embedded with exactly this model and options,
their stored vectors are reused (no embedding cost); otherwise the documents are
chunked and embedded in memory.

Not covered: query rewriting (chat model) and FAQ context passages, which the
live chat adds on top.
"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from asgiref.sync import sync_to_async

import llm
from llm.config import build
from siteconfig import conf

from .extract import extract_text
from .faq import _wording_matches
from .lexical import terms
from .models import Chunk, Document, embed_signature
from .retrieval import Passage, finalise, keyword_coverage, strong_threshold
from .services import _read_file, split_document

DEFAULT_CASES = Path(__file__).parent / "eval_cases" / "embeddings.json"
OVERRIDABLE = ("min_score", "strong_score", "faq_min_score", "faq_context_min_score",
               "query_params", "document_params", "query_prefix", "document_prefix")


class EvalError(Exception):
    pass


def load_cases(path=None) -> dict:
    path = Path(path or DEFAULT_CASES)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise EvalError(f"Cannot read test cases {path}: {exc}") from exc


def candidate_config(model: str | None = None, overrides: dict | None = None):
    """The live configuration with ``model`` as the embedding model (in memory only).

    ``model`` is an embedding model's identifier ("bedrock/cohere.embed-multilingual-v3");
    None means the enabled one. ``overrides`` replace that row's fields for this run.
    """
    from llm.models import LLMModel, Provider

    providers = list(Provider.objects.all())
    models = list(LLMModel.objects.select_related("provider"))
    embedding = [m for m in models if m.kind == LLMModel.Kind.EMBEDDING]
    if model:
        target = next((m for m in embedding if m.identifier == model), None)
        if target is None:
            known = ", ".join(m.identifier for m in embedding) or "none"
            raise EvalError(f"No embedding model {model!r} in the admin (known: {known})")
    else:
        target = next((m for m in embedding if m.enabled), None)
        if target is None:
            raise EvalError("No embedding model is enabled; pass --model")
    for m in embedding:  # never saved
        m.enabled = m is target
    for name, value in (overrides or {}).items():
        if name not in OVERRIDABLE:
            raise EvalError(f"Cannot override {name!r} (allowed: {', '.join(OVERRIDABLE)})")
        setattr(target, name, value)
    config = build(providers, models)
    if config.embed_model != target.identifier:
        raise EvalError(f"{target.identifier} cannot be used: its provider is disabled or incomplete")
    return config


@dataclass
class Index:
    ids: list
    texts: list
    documents: list
    matrix: np.ndarray
    reused: bool
    embedded: int = 0  # texts sent to the embedding model


def _unit(m):
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.where(norms == 0, 1, norms)


async def _embed(texts, purpose, config, batch):
    out = []
    for i in range(0, len(texts), batch):
        out += await llm.embed(texts[i : i + batch], purpose=purpose, config=config)
    return out


async def build_index(tenant, config, cfg) -> Index:
    docs = [d async for d in Document.objects.filter(tenant=tenant, status=Document.Status.READY).order_by("pk")]
    if not docs:
        return Index([], [], [], np.zeros((0, 0), dtype=np.float32), reused=True)
    signature = embed_signature(config)
    if all(d.embed_signature == signature for d in docs):
        rows = [c async for c in Chunk.objects.filter(document__in=docs).select_related("document")
                .order_by("document_id", "index")]
        return Index([c.id for c in rows], [c.text for c in rows], [c.document.name for c in rows],
                     _unit([list(c.vector()) for c in rows]), reused=True)
    ids, texts, names, inputs = [], [], [], []
    for d in docs:
        data = await sync_to_async(_read_file)(d)
        text = await sync_to_async(extract_text)(data, d.extension)
        _, pieces, embed_inputs = split_document(text, d.name, cfg)
        for i, (piece, inp) in enumerate(zip(pieces, embed_inputs)):
            ids.append(f"{d.pk}:{i}")
            texts.append(piece)
            names.append(d.name)
            inputs.append(config.document_prefix + inp)
    vectors = await _embed(inputs, "document", config, cfg.embed_batch_size)
    return Index(ids, texts, names, _unit(vectors), reused=False, embedded=len(inputs))


def _is_right(case, document, text) -> bool:
    if case.get("document") and case["document"].lower() not in document.lower():
        return False
    expect = case.get("expect") or []
    return not expect or any(e.lower() in text.lower() for e in expect)


def decide(question, qv, index: Index, config, cfg):
    """What the live search() would return for one question (same steps, in memory)."""
    if not index.ids:
        return [], np.zeros(0)
    scores = index.matrix @ qv
    top_k, min_score = cfg.top_k, config.doc_cutoff
    order = np.argsort(-scores)[: top_k * 3]
    best = {int(i): float(scores[i]) for i in order if scores[i] >= min_score}
    if cfg.keyword_min_coverage > 0:
        query_terms = [terms(question)] if terms(question) else []
        if query_terms:
            ranked = sorted(((keyword_coverage(query_terms, t), i) for i, t in enumerate(index.texts)), reverse=True)
            for cov, i in ranked[: top_k * 3]:
                if cov >= cfg.keyword_min_coverage:
                    best.setdefault(i, float(scores[i]))
    passages = [
        Passage(chunk_id=i, document_id=0, document=index.documents[i], index=i, text=index.texts[i], score=s)
        for i, s in sorted(best.items(), key=lambda kv: -kv[1])
    ]
    chosen = finalise([question], passages, strong_threshold(config, cfg, min_score), top_k, cfg.min_term_overlap)
    return chosen, scores


async def run(cases: dict, config, tenants: dict, log=lambda msg: None) -> dict:
    """Run the test. ``tenants`` maps workspace slug -> Tenant (missing ones are skipped)."""
    platform = await conf.aget()
    report = {
        "model": config.embed_model,
        "settings": {
            "query_params": config.embed_query_params, "document_params": config.embed_document_params,
            "query_prefix": config.query_prefix, "document_prefix": config.document_prefix,
            "doc_cutoff": config.doc_cutoff, "strong_cutoff": config.strong_cutoff,
            "faq_cutoff": config.faq_cutoff, "faq_context_cutoff": config.faq_context_cutoff,
        },
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workspaces": {}, "questions": [], "faq_pairs": [], "skipped": [],
    }
    for slug, spec in cases.get("workspaces", {}).items():
        tenant = tenants.get(slug)
        if tenant is None:
            report["skipped"].append(slug)
            continue
        cfg = await conf.aget(tenant)
        index = await build_index(tenant, config, platform)
        report["workspaces"][slug] = {"passages": len(index.ids), "reused_stored_vectors": index.reused,
                                      "embedded_texts": index.embedded}
        log(f"{slug}: {len(index.ids)} passages ({'stored vectors' if index.reused else 'embedded now'})")
        answerable = [dict(c, kind="answerable") for c in spec.get("answerable", [])]
        off_topic = [{"q": q, "kind": "off_topic"} for q in spec.get("off_topic", [])]
        cases_ = answerable + off_topic
        if not cases_:
            continue
        qvs = _unit(await _embed([config.query_prefix + c["q"] for c in cases_], "query", config, 64))
        for case, qv in zip(cases_, qvs):
            chosen, scores = decide(case["q"], qv, index, config, cfg)
            row = {"workspace": slug, "question": case["q"], "kind": case["kind"],
                   "best_score": round(float(scores.max()), 4) if len(scores) else None,
                   "returned": len(chosen)}
            if case["kind"] == "answerable":
                right = [i for i in range(len(index.ids)) if _is_right(case, index.documents[i], index.texts[i])]
                ranking = list(np.argsort(-scores)) if len(scores) else []
                rank = min((ranking.index(i) + 1 for i in right), default=None)
                row["right_score"] = round(max(float(scores[i]) for i in right), 4) if right else None
                row["right_rank"] = rank
                if not right:
                    row["outcome"] = "no_right_passage"  # test case doesn't match the documents
                elif any(_is_right(case, p.document, p.text) for p in chosen):
                    row["outcome"] = "found"
                else:
                    row["outcome"] = "wrong" if chosen else "refused"
            else:
                row["outcome"] = "leaked" if chosen else "refused"
            report["questions"].append(row)

    pairs = cases.get("faq_pairs", [])
    if pairs:
        a = _unit(await _embed([config.query_prefix + p["faq"] for p in pairs], "query", config, 64))
        b = _unit(await _embed([config.query_prefix + p["asked"] for p in pairs], "query", config, 64))
        for p, va, vb in zip(pairs, a, b):
            score = float(va @ vb)
            similar = score >= config.faq_cutoff
            wording = _wording_matches(p["asked"], p["faq"])
            # "same": the model must find them similar enough (the wording rule is separate
            # and model-independent; it's reported, not scored). Otherwise: no FAQ answer.
            ok = similar if p["expect"] == "same" else not (similar and wording)
            report["faq_pairs"].append({
                "faq": p["faq"], "asked": p["asked"], "expect": p["expect"], "score": round(score, 4),
                "similar_enough": similar, "wording_ok": wording, "answered_by_faq": similar and wording, "ok": ok,
            })
    report["summary"] = summarise(report)
    return report


def summarise(report) -> dict:
    qs = report["questions"]
    ans = [q for q in qs if q["kind"] == "answerable" and q["outcome"] != "no_right_passage"]
    off = [q for q in qs if q["kind"] == "off_topic"]
    faq = report["faq_pairs"]
    right_scores = [q["right_score"] for q in ans if q["right_score"] is not None]
    off_scores = [q["best_score"] for q in off if q["best_score"] is not None]
    lowest_right = min(right_scores) if right_scores else None
    highest_off = max(off_scores) if off_scores else None
    ranks = [q["right_rank"] for q in ans if q["right_rank"] is not None]
    passed = (sum(q["outcome"] == "found" for q in ans) + sum(q["outcome"] == "refused" for q in off)
              + sum(p["ok"] for p in faq))
    total = len(ans) + len(off) + len(faq)
    return {
        "score": round(passed / total, 4) if total else None,
        "passed": passed, "total": total,
        "answerable": len(ans),
        "found": sum(q["outcome"] == "found" for q in ans),
        "wrong": sum(q["outcome"] == "wrong" for q in ans),
        "refused": sum(q["outcome"] == "refused" for q in ans),
        "off_topic": len(off),
        "off_topic_refused": sum(q["outcome"] == "refused" for q in off),
        "right_in_top3": sum(r <= 3 for r in ranks),
        "median_right_rank": float(np.median(ranks)) if ranks else None,
        "lowest_right_score": lowest_right,
        "highest_off_topic_score": highest_off,
        "margin": round(lowest_right - highest_off, 4) if right_scores and off_scores else None,
        "faq_pairs": len(faq), "faq_ok": sum(p["ok"] for p in faq),
        "bad_cases": sum(q["outcome"] == "no_right_passage" for q in qs),
    }
