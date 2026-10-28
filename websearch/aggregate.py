"""Multi-engine search with de-duplication and rank fusion."""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, net
from .breaker import BREAKER
from .engines import BLOCKED, DEFAULT_ENGINES, ENGINES, get_engine
from .relevance import is_relevant
from .types import EngineError, HttpStatusError, Result, SearchResponse
from .urls import TRACKING_PARAMS, canonical_url, domain as _domain  # noqa: F401

log = logging.getLogger("websearch.aggregate")

# Reciprocal-rank-fusion constant. 60 is the value from the original Cormack et
# al. paper; it damps the influence of top-1 hits enough that several engines
# agreeing on positions 2-5 outranks one engine's outlier at position 1.
RRF_K = 60

STRATEGIES = {"rrf", "round-robin", "score"}

TRACKING_PARAMS = TRACKING_PARAMS  # re-exported for backwards compatibility


def resolve_engines(spec: str | None) -> tuple[list[str], dict[str, str]]:
    """Turn a user engine spec into ``(names, errors)``.

    Accepts ``""``/``"default"``, ``"all"``, ``"all-api"``, ``"fast"``,
    comma-separated names, and group names such as ``web``, ``academic``,
    ``code``, ``media``, ``social``, ``reference``.
    """
    errors: dict[str, str] = {}
    spec = (spec or "").strip().lower()
    if not spec or spec == "default":
        return list(DEFAULT_ENGINES), errors
    if spec == "all":
        return [n for n, e in ENGINES.items() if e.enabled], errors
    if spec in {"fast", "quick"}:
        return [n for n, e in ENGINES.items() if e.enabled and e.fast], errors
    if spec in {"api", "all-api", "apis"}:
        return [n for n, e in ENGINES.items() if e.enabled and e.kind == "api"], errors

    names: list[str] = []
    for token in re.split(r"[,\s]+", spec):
        token = token.strip()
        if not token:
            continue
        if token in ENGINES and ENGINES[token].enabled:
            names.append(token)
            continue
        # Group expansion.
        group = [n for n, e in ENGINES.items() if e.enabled and token in e.groups]
        if group:
            names.extend(group)
            continue
        if token in BLOCKED:
            errors[token] = f"disabled: {BLOCKED[token]}"
        elif token in ENGINES:
            errors[token] = f"engine '{token}' is disabled ({ENGINES[token].note or 'not enabled'})"
        else:
            errors[token] = f"unknown engine '{token}'"
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    ordered = [n for n in names if not (n in seen or seen.add(n))]
    return ordered, errors


def _apply_filters(results: list[Result], *, site: str, exclude: list[str]) -> list[Result]:
    if site:
        site_norm = site.lower().removeprefix("www.")
        results = [
            r for r in results
            if _domain(r.url) == site_norm or _domain(r.url).endswith("." + site_norm)
        ]
    if exclude:
        needles = [e.lower() for e in exclude if e]
        if needles:
            results = [
                r for r in results
                if not any(n in (r.url + " " + r.title + " " + r.snippet).lower() for n in needles)
            ]
    return results


def search(
    query: str,
    *,
    limit: int = 10,
    engines: str | None = None,
    per_engine: int | None = None,
    site: str = "",
    exclude: list[str] | None = None,
    strategy: str = "rrf",
    max_per_domain: int = 0,
    timeout: float | None = None,
    use_cache: bool = True,
) -> SearchResponse:
    """Search several engines concurrently and fuse the results.

    Args:
        query: search terms.
        limit: number of results to return.
        engines: engine spec (see :func:`resolve_engines`).
        per_engine: how many hits to ask each engine for (default: ``limit``,
            floored at 5 so fusion has something to work with).
        site: restrict results to one domain.
        exclude: drop results whose url/title/snippet contain any of these.
        strategy: ``rrf`` (reciprocal rank fusion), ``round-robin`` or ``score``.
        max_per_domain: if > 0, cap how many results one domain may contribute.
        timeout: per-engine timeout in seconds.
        use_cache: read and write the result cache. ``False`` re-runs the
            search without disturbing other callers' cached entries.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), config.MAX_RESULTS))
    per_engine = max(limit, 5) if per_engine is None else max(1, min(int(per_engine), 50))
    # Normalise before it reaches the cache key, and reject typos instead of
    # silently falling back to rrf.
    strategy = (strategy or "rrf").strip().lower().replace("_", "-")
    if strategy not in STRATEGIES:
        raise ValueError(
            f"strategy must be one of {', '.join(sorted(STRATEGIES))}; got {strategy!r}"
        )

    names, errors = resolve_engines(engines)
    if not names:
        return SearchResponse(query, [], [], {}, errors or {"engines": "no usable engines"})

    # Every parameter that changes the result set must be in the key. The
    # exclude list was missing, so a filtered search could be served the
    # unfiltered cached copy (and vice versa). The timeout is in it for the
    # same reason: a 2s call that times an engine out caches a degraded result
    # set that a default-timeout caller would otherwise be served for 5 minutes.
    #
    # The exclude list is JSON-encoded rather than comma-joined: a comma is a
    # legal character *inside* a term, so ``["a,b"]`` and ``["a","b"]`` both
    # rendered as ``a,b`` and collided on one cache entry.
    exclude_key = json.dumps(sorted(e.lower() for e in (exclude or []) if e))
    cache_key = (
        f"{query}|{','.join(names)}|{limit}|{per_engine}|{site}|{strategy}"
        f"|{max_per_domain}|{exclude_key}|{timeout}"
    )
    cached = net.SEARCH_CACHE.get(cache_key) if use_cache else None
    if cached is not None:
        return SearchResponse.from_cache(cached)

    started = time.monotonic()
    per_engine_results: dict[str, list[Result]] = {}
    timings: dict[str, float] = {}
    dropped: dict[str, int] = {}
    raw_results: dict[str, list[Result]] = {}

    def run(name: str) -> tuple[str, list[Result], list[Result], str | None, float]:
        """Return ``(name, kept, raw, error, elapsed)``."""
        t0 = time.monotonic()
        if not BREAKER.available(name):
            wait = BREAKER.remaining(name)
            return name, [], [], f"cooldown: blocked recently, retry in {wait:.0f}s", 0.0
        try:
            engine = get_engine(name)
            # Push the site restriction into engines that understand the
            # operator. Filtering only after the fact has terrible recall: the
            # engine's top hits rarely happen to be on the target domain.
            effective_query = query
            if site and engine.site_syntax and "site:" not in query:
                effective_query = f"{query} site:{site}"
            res = engine.search(effective_query, per_engine, timeout=timeout)
            # Drop results that share nothing with the query before fusion.
            # Keyless engines sometimes answer with a SERP for an entirely
            # different query (see relevance.py); letting those into the
            # fusion pool both pollutes the output and earns them bogus
            # "agreement" credit from a second junk-serving engine.
            #
            # This is strict per engine on purpose. A junk-serving engine
            # returns *only* junk, so a per-engine "keep everything if nothing
            # matches" fallback would let exactly the bad case through. The
            # global fallback lives below: if filtering removed everything
            # from every engine, the unfiltered results are used instead.
            kept = res
            if config.RELEVANCE_FILTER:
                kept = [r for r in res
                        if is_relevant(query, r.title, r.snippet, r.url)]
            BREAKER.record_success(name)
            return name, kept, res, None, time.monotonic() - t0
        except EngineError as exc:
            BREAKER.record_failure(name, exc)
            return name, [], [], str(exc), time.monotonic() - t0
        except HttpStatusError as exc:
            # ``net.request`` raises this — not ``EngineError`` — for any
            # non-2xx it does not classify as a challenge, so without this
            # branch a 5xx-ing engine was reported to the breaker as a generic
            # crash and never cooled down.
            BREAKER.record_failure(name, exc)
            return name, [], [], f"{name}: {exc}", time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001 - one engine must not kill the call
            log.debug("engine %s crashed", name, exc_info=True)
            return name, [], [], f"{type(exc).__name__}: {str(exc)[:200]}", time.monotonic() - t0

    workers = max(1, min(config.MAX_CONCURRENCY, len(names)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, n) for n in names]
        for fut in as_completed(futures):
            name, kept, raw, err, elapsed = fut.result()
            timings[name] = elapsed
            if err:
                errors[name] = err
                per_engine_results[name] = []
                continue
            per_engine_results[name] = kept
            raw_results[name] = raw
            if len(raw) > len(kept):
                dropped[name] = len(raw) - len(kept)

    # Every engine answered with a SERP for a different question.
    #
    # This is a real, measured failure mode, not a theoretical one: from a
    # datacenter IP, Bing answered "how to use git rebase" with eight Gmail
    # login pages and Yahoo answered it with dictionary entries for "use"
    # (2 of 9 common queries hit this). Returning those unfiltered — which an
    # earlier version did, on the theory that a silent empty result is worse —
    # hands the caller confident-looking nonsense, which is strictly worse than
    # an honest zero: a model cannot tell a Gmail link from a rebase tutorial,
    # but it can act on an error.
    #
    # The condition is narrow by construction. A result is dropped only when it
    # shares *no* significant term with the query (see relevance.is_relevant),
    # so this branch is reachable only when every single hit is off-topic.
    # Queries the tokenizer cannot read (non-Latin, all-stopword) produce no
    # terms and therefore never reach it.
    if config.RELEVANCE_FILTER and not any(per_engine_results.values()) \
            and any(raw_results.values()):
        dropped.clear()
        errors["relevance_filter"] = (
            "every engine returned off-topic results (a known keyless-engine "
            "failure mode); refusing to return them. Retry with a different "
            "engine set, or set WEBSEARCH_RELEVANCE_FILTER=0 to see them anyway"
        )

    merged = _fuse(per_engine_results, strategy)
    merged = _apply_filters(merged, site=site, exclude=exclude or [])
    if max_per_domain > 0:
        merged = _cap_per_domain(merged, max_per_domain)
    merged = merged[:limit]
    for i, r in enumerate(merged, 1):
        r.rank = i

    resp = SearchResponse(
        query=query,
        results=merged,
        engines_requested=names,
        per_engine_counts={n: len(v) for n, v in per_engine_results.items()},
        errors=errors,
        timings=timings,
        strategy=strategy,
        filtered=dropped,
    )
    resp.timings["total"] = time.monotonic() - started
    # Do not cache a response that is empty *because something went wrong*.
    # The cooldown error text literally tells the caller "retry in Ns", yet the
    # empty result set used to be pinned for the whole TTL, so the retry they
    # were told to make returned the same stale failure long after the breaker
    # had expired. Only an empty answer with no errors is a real "no results".
    degraded = not merged and bool(errors)
    if use_cache and not degraded:
        net.SEARCH_CACHE.set(cache_key, resp.to_cache())
    return resp


def _fuse(per_engine_results: dict[str, list[Result]], strategy: str) -> list[Result]:
    """Merge per-engine result lists into one ranked list.

    Returns **every** distinct URL, not a truncated pool. The caller applies
    the site/exclude/per-domain filters afterwards, and those can discard an
    arbitrary share of the pool: truncating here first meant a filtered search
    could return two or three results while dozens of valid hits sat just past
    the cut, with no way to backfill. Ranking is cheap; losing results is not.
    """
    if strategy == "round-robin":
        return _round_robin(per_engine_results, sum(len(v) for v in per_engine_results.values()))

    buckets: dict[str, list[Result]] = {}
    scores: dict[str, float] = {}
    for name, results in per_engine_results.items():
        engine = ENGINES.get(name)
        weight = engine.weight if engine else 1.0
        for idx, r in enumerate(results):
            key = canonical_url(r.url)
            if not key:
                continue
            buckets.setdefault(key, []).append(r)
            if strategy == "score":
                scores[key] = scores.get(key, 0.0) + max(0.0, r.score)
            else:
                scores[key] = scores.get(key, 0.0) + weight / (RRF_K + idx + 1)

    fused: list[Result] = []
    for key, group in buckets.items():
        primary = max(group, key=lambda r: len(r.snippet))
        primary.meta = dict(primary.meta)
        sources = sorted({r.source for r in group})
        # Count independent corroboration by index family, not engine name:
        # DuckDuckGo, Yahoo and Bing all serve the Bing index, so three hits
        # there are one opinion, not three.
        families = sorted({_family(s) for s in sources})
        if len(sources) > 1:
            primary.meta["also_in"] = [s for s in sources if s != primary.source]
        primary.meta["agreement"] = len(sources)
        primary.meta["independent"] = len(families)
        primary.score = scores[key]
        fused.append(primary)

    # Rank by fused score, then by independent corroboration, then snippet
    # length (a longer snippet is usually a better-described hit).
    fused.sort(key=lambda r: (r.score, r.meta.get("independent", 1), len(r.snippet)),
               reverse=True)
    return fused


def _family(engine_name: str) -> str:
    engine = ENGINES.get(engine_name)
    if engine and engine.index_family:
        return engine.index_family
    return engine_name


def _round_robin(per_engine_results: dict[str, list[Result]], limit: int) -> list[Result]:
    queues = {k: list(v) for k, v in per_engine_results.items() if v}
    out: list[Result] = []
    seen: set[str] = set()
    order = list(queues)
    idx = 0
    while order and len(out) < limit:
        name = order[idx % len(order)]
        bucket = queues[name]
        advanced = False
        while bucket:
            r = bucket.pop(0)
            key = canonical_url(r.url)
            if key and key not in seen:
                seen.add(key)
                out.append(r)
                advanced = True
                break
        if not bucket:
            order.remove(name)
            continue
        if advanced:
            idx += 1
    return out


def _cap_per_domain(results: list[Result], cap: int) -> list[Result]:
    counts: dict[str, int] = {}
    out: list[Result] = []
    for r in results:
        d = _domain(r.url)
        if counts.get(d, 0) >= cap:
            continue
        counts[d] = counts.get(d, 0) + 1
        out.append(r)
    return out
