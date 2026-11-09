"""Workspace FAQs: direct answers without the model, and close FAQs as context."""
import hashlib
import re
from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.db.models import F
from django.utils import timezone

import llm
from siteconfig import conf

from .lexical import terms
from .models import FAQ
from .retrieval import Passage

_NEGATION = re.compile(r"\b(not|no|never|none|nobody|nothing|cannot|can't|won't|don't|doesn't|didn't|isn't|"
                       r"aren't|wasn't|weren't|shouldn't|wouldn't|couldn't|without)\b", re.I)


def normalise(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", text.lower()))


def negated(text: str) -> bool:
    return bool(_NEGATION.search(text.replace("’", "'")))


def signature(faq: FAQ, config) -> str:
    digest = hashlib.sha1("\n".join(faq.phrasings()).encode()).hexdigest()[:12]
    return f"{config.embed_model}|{config.query_prefix}|{digest}"


@dataclass
class Match:
    faq: FAQ
    phrasing: str
    score: float  # 1.0 for an exact match


async def enabled_for(tenant) -> list[FAQ]:
    return [f async for f in FAQ.objects.filter(tenant=tenant, enabled=True)]


async def index(faqs, config) -> list[FAQ]:
    """Embed FAQs whose phrasings or embedding settings changed. Returns the indexed ones."""
    if not config.embed_model:
        return []
    stale = [f for f in faqs if f.embed_signature != signature(f, config) or f.matrix() is None]
    if stale:
        texts = [config.query_prefix + p for f in stale for p in f.phrasings()]
        vectors = await llm.embed(texts)  # raises llm.LLMError
        i = 0
        for f in stale:
            n = len(f.phrasings())
            block = np.asarray(vectors[i : i + n], dtype=np.float32)
            i += n
            f.vectors, f.dimensions, f.embed_signature = block.tobytes(), block.shape[1], signature(f, config)
            await FAQ.objects.filter(pk=f.pk).aupdate(
                vectors=f.vectors, dimensions=f.dimensions, embed_signature=f.embed_signature
            )
    return [f for f in faqs if f.matrix() is not None and f.embed_signature == signature(f, config)]


def _similarities(query_vector, faq):
    m = faq.matrix()
    q = np.asarray(query_vector, dtype=np.float32)
    if m is None or m.shape[1] != q.shape[0]:
        return None
    norms = np.linalg.norm(m, axis=1) * (np.linalg.norm(q) or 1)
    return (m @ q) / np.where(norms == 0, 1, norms)


def _wording_matches(question: str, phrasing: str) -> bool:
    """No key word in the question that the phrasing lacks, at least half the phrasing's
    key words present, and the same negation."""
    q, p = terms(question), terms(phrasing)
    if not q or not p or negated(question) != negated(phrasing):
        return False
    return q <= p and len(q) / len(p) >= 0.5


def direct_match(question: str, query_vector, faqs, min_score: float | None = None) -> Match | None:
    norm = normalise(question)
    for f in faqs:
        for p in f.phrasings():
            if normalise(p) == norm:
                return Match(f, p, 1.0)
    if query_vector is None:
        return None
    best = None
    for f in faqs:
        sims = _similarities(query_vector, f)
        if sims is None:
            continue
        for p, score in zip(f.phrasings(), sims.tolist()):
            cutoff = conf.get().faq_min_score if min_score is None else min_score
            if score >= cutoff and _wording_matches(question, p):
                if best is None or score > best.score:
                    best = Match(f, p, score)
    return best


def context_passages(query_vector, faqs, min_score: float | None = None) -> list[Passage]:
    """FAQs close to the question (but not direct matches), as passages for the model."""
    limit = conf.get().faq_context_max
    if query_vector is None or limit <= 0:
        return []
    scored = []
    for f in faqs:
        sims = _similarities(query_vector, f)
        if sims is not None and len(sims):
            score = float(sims.max())
            if score >= (conf.get().faq_context_min_score if min_score is None else min_score):
                scored.append((score, f))
    scored.sort(key=lambda sf: -sf[0])
    return [
        Passage(chunk_id=None, document_id=f"faq-{f.pk}", document="FAQ", index=0,
                text=f"Q: {f.question}\nA: {f.answer}", score=score)
        for score, f in scored[:limit]
    ]


async def record_hit(faq: FAQ) -> None:
    await FAQ.objects.filter(pk=faq.pk).aupdate(hit_count=F("hit_count") + 1, last_hit_at=timezone.now())
