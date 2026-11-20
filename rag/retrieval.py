"""Semantic search over a workspace's document chunks."""
from dataclasses import asdict, dataclass

import numpy as np
from django.conf import settings

import llm
from siteconfig import conf

from django.db.models import Q

from .lexical import coverage, terms
from .models import Chunk, Document, embed_signature


@dataclass
class Passage:
    chunk_id: int | None  # None for FAQ passages
    document_id: int
    document: str
    index: int
    text: str
    score: float

    def as_source(self, number: int) -> dict:
        data = asdict(self)
        del data["text"]
        data["n"] = number
        data["score"] = round(self.score, 3)
        return data


class Results(list):
    """Passages, plus the best raw similarity seen (even if nothing qualified)."""
    best_score: float | None = None


# The Moodle plugin names each course's overview "<course name>: course overview and schedule".
OVERVIEW_KEY, OVERVIEW_SUFFIX = "overview", ": course overview and schedule"
# Passages always taken from the page a Moodle student is viewing, so "this module" is answerable.
PAGE_PASSAGES = 2


@dataclass
class Place:
    """Where a Moodle student is: the course's name and the activity page they're viewing."""
    course: str = ""
    page: str = ""
    page_document_id: int | None = None


async def moodle_place(tenant, course, activity=None) -> Place:
    """The course and page names for a student on a Moodle course's pages (blank when unknown,
    e.g. the course hasn't been sent yet or the activity isn't shown to every student)."""
    place = Place()
    if course is None:
        return place
    docs = Document.objects.filter(tenant=tenant, moodle_course_id=course, status=Document.Status.READY)
    overview = await docs.filter(moodle_key=OVERVIEW_KEY).only("name").afirst()
    if overview:
        place.course = overview.name.removesuffix(OVERVIEW_SUFFIX)
    if activity:
        page = await docs.filter(moodle_key=f"cm:{activity}").only("id", "name").afirst()
        if page:
            place.page, place.page_document_id = page.name, page.id
    return place


def _searchable(tenant, config, course=None):
    """Chunks of ready documents embedded with the current embedding configuration: the
    workspace's own documents, or with ``course`` (a Moodle course ID) that course's content."""
    return Chunk.objects.filter(
        tenant=tenant,
        document__status=Document.Status.READY,
        document__embed_signature=embed_signature(config),
        **_scope(course, "document__"),
    )


def _scope(course, prefix=""):
    if course is None:
        return {f"{prefix}moodle_course_id__isnull": True}
    return {f"{prefix}moodle_course_id": course}


class KB:
    """Knowledge-base availability for a workspace."""
    READY = "ready"
    NO_DOCUMENTS = "no_documents"
    NO_EMBEDDING_MODEL = "no_embedding_model"  # search is switched off
    NEEDS_REINDEX = "needs_reindex"           # documents exist, embedded differently


async def kb_status(tenant, course=None) -> str:
    config = await llm.aget_config()
    if not config.embed_model:
        return KB.NO_EMBEDDING_MODEL
    if await _searchable(tenant, config, course).aexists():
        return KB.READY
    if await Document.objects.filter(tenant=tenant, status=Document.Status.READY, **_scope(course)).aexists():
        return KB.NEEDS_REINDEX
    return KB.NO_DOCUMENTS


async def has_documents(tenant, course=None) -> bool:
    return await kb_status(tenant, course) == KB.READY


async def embed_queries(queries: list[str]) -> list[list[float]]:
    """Embed questions the way search() does (with the query prefix)."""
    config = await llm.aget_config()
    return await llm.embed([config.query_prefix + q for q in queries], purpose="query")


async def search(tenant, queries: list[str], top_k: int | None = None,
                 min_score: float | None = None, query_vectors=None, course=None,
                 page_document=None) -> list[Passage]:
    """Return the passages that answer ``queries``, best first (hybrid search).

    Candidates come from two routes:
    - meaning: chunks whose cosine similarity (best across the queries) is at
      least ``min_score``;
    - keywords: chunks containing at least RAG_KEYWORD_MIN_COVERAGE of a
      query's key terms. Embeddings score names and rare terms poorly
      ("Who is Chief Iyare?"), so exact matches must not depend on them.
    All candidates must then pass the term-overlap check (_lexical_filter).
    Passages matching more of the question's terms rank first, then by
    similarity. Raises llm.LLMError if the query cannot be embedded.

    ``course`` (a Moodle course ID) searches that course's content instead of the
    workspace's documents. ``page_document``: the course item the student is viewing; its
    PAGE_PASSAGES best passages come first whatever their score, so questions like "what is
    this module about?" are answered from it.
    """
    cfg = await conf.aget(tenant)
    top_k = cfg.top_k if top_k is None else top_k
    queries = [q for q in queries if q.strip()]
    if not queries:
        return []

    config = await llm.aget_config()
    min_score = config.doc_cutoff if min_score is None else min_score
    rows = [r async for r in _searchable(tenant, config, course).values_list("id", "embedding", "dimensions")]
    if not rows:
        return []

    if query_vectors is None:
        query_vectors = await llm.embed([config.query_prefix + q for q in queries], purpose="query")
    q = np.asarray(query_vectors, dtype=np.float32)
    dims = q.shape[1]
    rows = [r for r in rows if r[2] == dims]  # skip vectors from a different model
    if not rows:
        return []

    ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    matrix = np.frombuffer(b"".join(bytes(r[1]) for r in rows), dtype=np.float32).reshape(len(rows), dims)

    def unit(m):
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(norms == 0, 1, norms)

    scores = (unit(matrix) @ unit(q).T).max(axis=1)
    score_of = dict(zip(ids.tolist(), scores.tolist()))
    results = Results()
    results.best_score = float(scores.max()) if len(scores) else None
    # Over-fetch so the lexical filter can drop candidates without starving the result.
    order = np.argsort(-scores)[: top_k * 3]
    best = {int(ids[i]): float(scores[i]) for i in order if scores[i] >= min_score}
    for cid in await _keyword_candidates(tenant, config, queries, limit=top_k * 3, threshold=cfg.keyword_min_coverage,
                                         course=course):
        if cid in score_of:
            best.setdefault(cid, score_of[cid])
    page_ids = []
    if page_document is not None:
        in_page = [cid async for cid in _searchable(tenant, config, course).filter(document_id=page_document)
                   .values_list("id", flat=True)]
        page_ids = sorted((c for c in in_page if c in score_of), key=lambda c: -score_of[c])[:PAGE_PASSAGES]
    if not best and not page_ids:
        return results

    chunks = {
        c.id: c
        async for c in Chunk.objects.filter(id__in=[*best, *page_ids]).select_related("document").only(
            "id", "index", "text", "document__id", "document__name"
        )
    }

    def passage(cid, score):
        c = chunks[cid]
        return Passage(chunk_id=cid, document_id=c.document.id, document=c.document.name, index=c.index,
                       text=c.text, score=score)

    passages = [passage(cid, score) for cid, score in sorted(best.items(), key=lambda kv: -kv[1])
                if cid in chunks and cid not in page_ids]
    results.extend(passage(cid, score_of[cid]) for cid in page_ids if cid in chunks)
    results.extend(finalise(queries, passages, strong_threshold(config, cfg, min_score), top_k, cfg.min_term_overlap))
    return results


def strong_threshold(config, cfg, min_score: float) -> float:
    """Similarity at which passages skip the key-word check."""
    if min_score == config.doc_cutoff:
        return config.strong_cutoff  # per embedding model, or cut-off x platform factor
    # an explicit min_score (diagnostics/tests)
    return min_score * cfg.confident_factor if min_score > 0 else float("inf")


def finalise(queries, passages, strong_at: float, top_k: int, min_term_overlap: float):
    """Apply the key-word check to candidate passages (best first) and keep top_k.

    Strong matches are trusted as they are; the key-word check guards the rest
    (it catches questions that only resemble a passage in shape).
    """
    if min_term_overlap > 0:
        strong = [p for p in passages if p.score >= strong_at]
        others = [p for p in passages if p.score < strong_at]
        passages = strong + (_lexical_filter(queries, others, min_term_overlap) if others else [])
    return passages[:top_k]


def keyword_coverage(query_terms: list[set], text: str) -> float:
    """Best share of any query's key terms found in ``text``."""
    chunk_terms = terms(text)
    return max(len(t & chunk_terms) / len(t) for t in query_terms)


async def _keyword_candidates(tenant, config, queries, limit, threshold, course=None) -> list[int]:
    """Chunk IDs containing at least ``threshold`` (keyword search coverage) of a query's terms."""
    if threshold <= 0:
        return []
    query_terms = [t for t in (terms(q) for q in queries) if t]
    if not query_terms:
        return []
    # Cheap database prefilter on term prefixes (tolerates word endings), then exact check.
    prefilter = Q()
    for term in set().union(*query_terms):
        prefilter |= Q(text__icontains=term[:4] if len(term) > 4 else term)
    rows = [r async for r in _searchable(tenant, config, course).filter(prefilter).values_list("id", "text")[:500]]
    ranked = []
    for cid, text in rows:
        best = keyword_coverage(query_terms, text)
        if best >= threshold:
            ranked.append((best, cid))
    ranked.sort(reverse=True)
    return [cid for _, cid in ranked[:limit]]


def _lexical_filter(queries, passages, min_overlap):
    """Keep passages only when the question's key terms actually appear in them.

    For the query whose terms are best covered: each kept passage must share at
    least one term with it, and together the kept passages must cover at least
    ``min_overlap`` of its terms; otherwise nothing is returned (so the model is
    never called). Queries without content terms skip the check.
    """
    best = None  # (coverage, kept passages)
    texts = [p.text for p in passages]
    for query in queries:
        cov, matched = coverage(query, texts)
        if cov is None:
            continue
        kept = [p for p, m in zip(passages, matched) if m]
        if best is None or cov > best[0]:
            best = (cov, kept)
    if best is None:
        return passages
    cov, kept = best
    if cov < min_overlap:
        return []
    # More of the question's terms first, then similarity (stable for ties).
    query_terms = max((terms(q) for q in queries), key=lambda t: len(t))
    return sorted(kept, key=lambda p: (-len(query_terms & terms(p.text)), -p.score))
