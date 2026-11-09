"""Lexical check that a question's key terms actually appear in retrieved passages.

Embedding similarity alone matches the *shape* of a question ("What is X?")
as well as its subject, so an unrelated question can score above the cut-off.
Requiring term overlap stops those from reaching the model.
"""
import re

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# Common English function/question words that carry no subject matter.
STOPWORDS = frozenset("""
a about above after again against all am an and any are as at be because been before being
below between both but by can cannot could did do does doing down during each few for from
further get gets got had has have having he her here hers herself him himself his how i if in
into is it its itself just let me more most my myself no nor not now of off on once only or
other our ours ourselves out over own please same she should so some such tell than that the
their theirs them themselves then there these they this those through to too under until up
us very was we were what when where which while who whom why will with would you your yours
yourself yourselves many much know want need explain describe give show list also
abeg wetin dey una dem wey na don fit sabi abi sef sha oya jare biko wan dis dat deh im ehn ehen
oo kuku shey make go far
""".split())  # last two lines: Nigerian Pidgin function words ("Abeg, wetin be X?" asks about X)


def stem(word: str) -> str:
    """Very light suffix stripping so 'costs'~'cost', 'carried'~'carry', 'offers'~'offer'."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("ied"):
        return word[:-3] + "y"
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def terms(text: str) -> set[str]:
    """Stemmed content terms of a text."""
    return {
        stem(w)
        for w in (m.group().lower() for m in _WORD.finditer(text))
        if w not in STOPWORDS and (len(w) > 1 or w.isdigit())
    }


def coverage(question: str, passage_texts: list[str]) -> tuple[float | None, list[set[str]]]:
    """Fraction of the question's terms found across the passages, and the terms each passage matched.

    Returns (None, ...) when the question has no content terms (nothing to check).
    """
    wanted = terms(question)
    matched = [wanted & terms(t) for t in passage_texts]
    if not wanted:
        return None, matched
    found = set().union(*matched) if matched else set()
    return len(found) / len(wanted), matched
