"""Result relevance scoring and junk filtering.

Why this module exists
----------------------
Keyless engines occasionally answer a query with a SERP for a *different*
query. This is not a rare edge case: from a datacenter IP, Bing and Yahoo
frequently return unrelated results (a Python query answered with WhatsApp,
Trezor or Scratch pages), and when the two "good" engines (Brave, DuckDuckGo)
are rate-limited the fused result set is then 100% off-topic while every
engine reports success.

Downstream that is worse than an error: a model receives confident-looking
results with titles and snippets that have nothing to do with its question.

So results are scored for lexical relevance to the query before fusion. The
score is deliberately simple and explainable — query-term coverage over
title/snippet/url — because the failure mode is gross (0% overlap), not
subtle. It is *not* a ranking signal: fused rank order is unchanged.
"""
from __future__ import annotations

import re

# Very common words carry no signal and inflate the overlap score of any page.
STOPWORDS = frozenset("""
a about above after again against all am an and any are aren't as at be because
been before being below between both but by can cannot could couldn't did didn't
do does doesn't doing don't down during each few for from further had hadn't has
hasn't have haven't having he her here hers herself him himself his how i if in
into is isn't it its itself just let's me more most mustn't my myself no nor not
of off on once only or other ought our ours ourselves out over own same shan't
she should shouldn't so some such than that the their theirs them themselves then
there these they this those through to too under until up very was wasn't we were
weren't what when where which while who whom why with won't would wouldn't you
your yours yourself yourselves use using used
""".split())

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+.#_-]*")


def query_terms(query: str) -> set[str]:
    """Significant lowercase terms from a query.

    Quoted phrases and ``site:``/``-term`` operators are stripped so they do
    not distort coverage: the phrase words still count, the operator does not.
    """
    if not query:
        return set()
    cleaned = re.sub(r"\bsite:\S+", " ", query.lower())
    cleaned = re.sub(r"(?<!\w)-(\w+)", r" \1", cleaned)   # -term -> term
    cleaned = cleaned.replace('"', " ")
    terms = set()
    for raw in _WORD_RE.findall(cleaned):
        token = raw.strip("._-")
        if len(token) < 2 or token in STOPWORDS:
            continue
        terms.add(token)
    return terms


def _stem(token: str) -> str:
    """Crude suffix stripping so ``tutorial`` matches ``tutorials``."""
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(token) > 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def coverage(query: str, *texts: str) -> float:
    """Fraction of query terms present in the supplied texts, 0.0-1.0.

    Matching is by stem prefix so ``async`` matches ``asyncio`` and
    ``tutorial`` matches ``tutorials``. Returns 1.0 when the query has no
    significant terms (nothing to judge, so never penalise).
    """
    terms = query_terms(query)
    if not terms:
        return 1.0
    hay = " ".join(t or "" for t in texts).lower()
    if not hay:
        return 0.0
    hits = 0
    for term in terms:
        stem = _stem(term)
        # Cheap bidirectional containment: "async" in "asyncio" (prefix match)
        # and "asyncio" -> stem "asyncio" finds "asyncio".
        if stem in hay or (len(term) >= 4 and term[:4] in hay):
            hits += 1
    return hits / len(terms)


def is_relevant(query: str, title: str, snippet: str, url: str) -> bool:
    """True when a result plausibly answers ``query``.

    The rule is deliberately conservative, because a false drop is worse than
    a false keep: a model can ignore an off-topic hit, but cannot recover a
    result that was silently removed.

    A result is dropped only when it shares **no** significant term with the
    query anywhere in its title, snippet or URL. That single condition is
    enough to catch every junk SERP observed in practice (Bing and Yahoo
    answering a Python query with WhatsApp, Trezor, Scratch or MSDN pages —
    all scoring exactly 0.0), while never removing a result that plausibly
    addresses the query.

    Queries with no significant terms (``"the"``, ``"how to"``) keep
    everything: there is nothing to judge against.
    """
    return coverage(query, title, snippet, url) > 0.0
