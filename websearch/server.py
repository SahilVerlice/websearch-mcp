"""FastMCP tool definitions for websearch-mcp."""
from __future__ import annotations

import json
import logging
import re
import sys
import time

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__, config, net
from .aggregate import resolve_engines, search as run_search
from .engines import BLOCKED, DEFAULT_ENGINES, ENGINES, ENGINE_ORDER, GROUPS
from .fetch import fetch as run_fetch
from .fetch import fetch_many
from .safety import validate_url
from .types import WebsearchError

log = logging.getLogger("websearch.server")

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    openWorldHint=True,
    idempotentHint=True,
    destructiveHint=False,
)

mcp = FastMCP(
    "websearch",
    instructions=(
        "Keyless web search and page reading. Use web_search for discovery, "
        "fetch_url to read a specific page as markdown, and multi_fetch to read "
        "several result pages at once. No API keys are required."
    ),
)
# FastMCP exposes no version argument, so the low-level server reports the
# *mcp SDK's* version ("1.22.0") in serverInfo instead of ours. Clients show
# that as this server's version. Setting it on the wrapped Server is the only
# supported place to fix it.
mcp._mcp_server.version = __version__


def _json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _error(exc: Exception) -> str:
    return _json({"error": f"{type(exc).__name__}: {exc}"})


# A URL ends at whitespace or a comma. A comma is *also* legal inside a URL
# (``?q=a,b``, ``/wiki/Foo_(bar,baz)``), so the split is narrow: a comma splits
# when it is followed by whitespace (how a human writes a list: "url1, url2")
# or immediately by another URL. A comma with no space after it stays part of
# the URL. The old rule required a scheme after the comma, so ``page, example.org``
# kept the comma glued to the first URL and fetched ``page,`` — a 404.
_URL_SPLIT_RE = re.compile(r"[\s]+|,(?=\s|https?://|ftp://|www\.)", re.I)


def _split_urls(raw: str) -> list[str]:
    """Split a multi-URL argument without breaking commas inside URLs."""
    if not raw:
        return []
    return [u.strip().rstrip(",") for u in _URL_SPLIT_RE.split(raw) if u and u.strip()]


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
@mcp.tool(annotations=READ_ONLY)
def web_search(
    query: str,
    limit: int = 10,
    engines: str = "",
    site: str = "",
    exclude: str = "",
    per_engine: int = 0,
    max_per_domain: int = 0,
    strategy: str = "rrf",
    no_cache: bool = False,
) -> str:
    """Search the web across several keyless engines and return fused results.

    Engines are queried in parallel and merged with reciprocal rank fusion, so
    a hit several engines agree on outranks a single engine's outlier. Duplicate
    URLs are collapsed and the merged entry records which engines found it.

    Args:
        query: The search query. Supports engine syntax such as quotes and
            ``-term`` where the underlying engine does.
        limit: Maximum results to return (1-50, default 10).
        engines: Which engines to use. Empty = default web set
            (brave,bing,yahoo,duckduckgo). Accepts:
              * a comma-separated list: "bing,hn,wikipedia"
              * "all" for every enabled engine, "fast" for low-latency engines,
                "api" for API-only engines
              * a group name: web, general, indie, code, academic, reference,
                social, news, media, books, medical
            Call list_engines to see everything available.
        site: Restrict results to a domain, e.g. "docs.python.org".
        exclude: Comma-separated terms; drop results whose URL/title/snippet
            contain any of them.
        per_engine: Hits requested per engine (0 = automatic, at least `limit`).
        max_per_domain: If > 0, cap results per domain to improve diversity.
        strategy: "rrf" (default), "round-robin", or "score".
        no_cache: Bypass the 5-minute result cache for this call. Other
            callers' cached entries are left alone (the previous behaviour
            cleared the whole cache).
    """
    try:
        resp = run_search(
            query,
            limit=limit,
            engines=engines,
            site=site,
            exclude=[e.strip() for e in exclude.split(",") if e.strip()],
            per_engine=per_engine or None,
            max_per_domain=max_per_domain,
            strategy=strategy,
            use_cache=not no_cache,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return _json(resp.as_dict())


@mcp.tool(annotations=READ_ONLY)
def search_engine(engine: str, query: str, limit: int = 10) -> str:
    """Run a single named engine and return its raw, unmerged results.

    Use this when you want one source's ordering, or to check whether a
    specific engine is currently working.

    Args:
        engine: Engine name, e.g. "bing", "arxiv", "github". See list_engines.
        query: The search query.
        limit: Maximum results (1-50, default 10).
    """
    from .engines import get_engine

    name = (engine or "").strip().lower()
    try:
        eng = get_engine(name)
        results = eng.search(query, max(1, min(int(limit), 50)))
        payload = {
            "engine": name,
            "query": query,
            "result_count": len(results),
            "results": [r.as_dict() for r in results],
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        payload = {
            "engine": name,
            "query": query,
            "result_count": 0,
            "results": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    return _json(payload)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
@mcp.tool(annotations=READ_ONLY)
def fetch_url(
    url: str,
    max_chars: int = 20000,
    fmt: str = "markdown",
    include_links: bool = False,
    query: str = "",
) -> str:
    """Fetch a URL and return its readable content.

    HTML is reduced to the main article using a readability-style heuristic
    (navigation, cookie banners, sidebars and footers are removed). JSON is
    pretty-printed, plain text is passed through, and PDFs are text-extracted.

    Pass ``query`` when you are reading a page to answer a specific question:
    only the passages that match it are returned, in document order. On a
    typical docs page that is ~5% of the text for the same answer, so prefer it
    when reading search results.

    Args:
        url: The http(s) URL to fetch.
        max_chars: Truncate the extracted content to this many characters.
        fmt: "markdown" (default, keeps headings/lists/links/code fences),
            "text" (plain text), or "html" (raw HTML, for debugging parsers).
        include_links: Also return the page's links as {text, url} entries.
        query: Optional. Keep only passages relevant to this question.
    """
    try:
        return _json(run_fetch(url, max_chars=max_chars, fmt=fmt,
                               include_links=include_links, query=query))
    except Exception as exc:  # noqa: BLE001
        return _json({"url": url, "error": f"{type(exc).__name__}: {exc}"})


@mcp.tool(annotations=READ_ONLY)
def multi_fetch(urls: str, max_chars: int = 6000, fmt: str = "markdown",
                query: str = "") -> str:
    """Fetch several URLs concurrently and return their extracted content.

    Ideal after web_search: read the top few results in one call instead of
    one round-trip per page. At most 12 URLs are fetched.

    Args:
        urls: Newline-separated URLs (commas also separate, but a comma inside
            a URL's query string is preserved).
        max_chars: Per-page character cap (default 6000).
        fmt: "markdown" (default) or "text".
        query: Optional. Keep only passages relevant to this question.
    """
    parts = _split_urls(urls)
    if not parts:
        return _json({"error": "no URLs given"})
    max_urls = 12
    dropped = parts[max_urls:]
    parts = parts[:max_urls]
    pages = fetch_many(parts, max_chars=max_chars, fmt=fmt, query=query)
    ok = sum(1 for p in pages if "error" not in p)
    payload = {"requested": len(parts) + len(dropped), "fetched": len(pages),
               "ok": ok, "pages": pages}
    if dropped:
        # Never truncate silently: the caller must know 3 URLs were not read.
        payload["skipped"] = dropped
    return _json(payload)


# --------------------------------------------------------------------------- #
# Discovery / diagnostics
# --------------------------------------------------------------------------- #
@mcp.tool(annotations=READ_ONLY)
def list_engines(verbose: bool = False) -> str:
    """List search engines, groups, and the ones intentionally disabled.

    Args:
        verbose: Include the per-engine notes and group memberships.
    """
    enabled = []
    for name in ENGINE_ORDER:
        eng = ENGINES.get(name)
        if not eng or not eng.enabled:
            continue
        item = {"name": name, "kind": eng.kind}
        if verbose:
            item["groups"] = list(eng.groups)
            item["fast"] = eng.fast
            item["note"] = eng.note
        enabled.append(item)
    return _json({
        "version": __version__,
        "default_engines": DEFAULT_ENGINES,
        "enabled_count": len(enabled),
        "enabled": enabled,
        "groups": {g: [n for n in ENGINE_ORDER
                       if n in ENGINES and g in ENGINES[n].groups] for g in GROUPS},
        "disabled": BLOCKED,
    })


@mcp.tool(annotations=READ_ONLY)
def check_engines(query: str = "python tutorial", timeout: float = 15.0) -> str:
    """Health-check every enabled engine with one query and report status.

    Args:
        query: Query used for the probe.
        timeout: Per-engine timeout in seconds.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time

    names = [n for n in ENGINE_ORDER if n in ENGINES and ENGINES[n].enabled]

    def probe(name: str) -> dict:
        started = time.monotonic()
        engine = ENGINES[name]
        # Engines over narrow corpora get their own probe query; "python
        # tutorial" legitimately has no Wikinews or Commons matches.
        probe_query = query if query != "python tutorial" else engine.probe_query
        try:
            results = engine.search(probe_query, 3, timeout=timeout)
            elapsed = time.monotonic() - started
            return {
                "engine": name,
                "ok": bool(results),
                "count": len(results),
                "seconds": round(elapsed, 2),
                "query": probe_query,
                "top": results[0].title[:80] if results else "",
                "error": None if results else "0 results",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "engine": name,
                "ok": False,
                "count": 0,
                "seconds": round(time.monotonic() - started, 2),
                "query": probe_query,
                "top": "",
                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            }

    with ThreadPoolExecutor(max_workers=min(config.MAX_CONCURRENCY, len(names))) as pool:
        futures = [pool.submit(probe, n) for n in names]
        rows = [f.result() for f in as_completed(futures)]
    rows.sort(key=lambda r: (not r["ok"], r["engine"]))
    ok = sum(1 for r in rows if r["ok"])
    return _json({"query": query, "ok": ok, "total": len(rows), "engines": rows})


@mcp.tool(annotations=READ_ONLY)
def suggest(query: str, limit: int = 8) -> str:
    """Get search-query autocomplete suggestions.

    Useful for broadening or reformulating a query before searching.

    Args:
        query: Partial query text.
        limit: Maximum suggestions (1-20).
    """
    try:
        data = net.get_json(
            "https://duckduckgo.com/ac/",
            params={"q": query, "type": "list"},
            engine="duckduckgo-suggest",
        )
        # The endpoint answers with ``[query, [phrase, ...]]`` (an OpenSearch
        # completion document), not a list of ``{"phrase": ...}`` objects.
        # Parsing it as the latter returned an empty list for every query.
        phrases: list[str] = []
        if isinstance(data, list):
            if len(data) >= 2 and isinstance(data[1], list):
                phrases = [str(p) for p in data[1] if isinstance(p, str)]
            else:
                phrases = [str(d.get("phrase", "")) for d in data
                           if isinstance(d, dict)]
        elif isinstance(data, dict):
            raw = data.get("suggestions") or data.get("phrases") or []
            if isinstance(raw, list):
                phrases = [str(d.get("phrase", d)) if isinstance(d, dict) else str(d)
                           for d in raw]
        phrases = [p.strip() for p in phrases if p and p.strip()]
        phrases = phrases[: max(1, min(int(limit), 20))]
        return _json({"query": query, "suggestions": phrases})
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READ_ONLY)
def expand_query(query: str) -> str:
    """Explain how a query will be routed and what related terms to try.

    Purely local: no network call. Reports the engines that would run, and
    generates variant phrasings (site:, quoted phrase, minus-term, recency)
    that tend to retrieve different results.

    Args:
        query: The query to analyse.
    """
    names, errors = resolve_engines("")
    tokens = [t for t in query.split() if t]
    variants = []
    if tokens:
        variants.append(f'"{query}"')
        variants.append(" ".join(tokens[:4]) + " tutorial")
        variants.append(" ".join(tokens[:4]) + " documentation")
        variants.append(" ".join(tokens[:4]) + " -site:reddit.com")
        # Derive the year: a hardcoded one silently becomes "last year" and
        # biases retrieval toward stale material, the opposite of what a
        # recency variant is for.
        variants.append(f"{' '.join(tokens[:4])} {time.gmtime().tm_year}")
    return _json({
        "query": query,
        "default_engines": names,
        "routing_errors": errors,
        "tokens": len(tokens),
        "variants": variants,
        "tips": [
            "Use engines='academic' for papers, 'code' for repositories.",
            "Use site= to restrict a search to one domain.",
            "Use exclude= to drop low-value domains.",
        ],
    })


@mcp.tool(annotations=READ_ONLY)
def server_info() -> str:
    """Report version, cache statistics, engine cooldowns and configuration."""
    from .breaker import BREAKER

    return _json({
        "version": __version__,
        "python": sys.version.split()[0],
        "default_engines": DEFAULT_ENGINES,
        "enabled_engines": sum(1 for e in ENGINES.values() if e.enabled),
        "engine_cooldowns_s": BREAKER.snapshot(),
        "cache": {
            "enabled": config.CACHE_ENABLED,
            "search_ttl_s": config.SEARCH_CACHE_TTL,
            "fetch_ttl_s": config.FETCH_CACHE_TTL,
            "search_hits": net.SEARCH_CACHE.hits,
            "search_misses": net.SEARCH_CACHE.misses,
            "fetch_hits": net.FETCH_CACHE.hits,
            "fetch_misses": net.FETCH_CACHE.misses,
        },
        "limits": {
            "max_results": config.MAX_RESULTS,
            "max_download_bytes": config.MAX_DOWNLOAD_BYTES,
            "max_concurrency": config.MAX_CONCURRENCY,
            "timeout_s": config.DEFAULT_TIMEOUT,
            "retries": config.MAX_RETRIES,
        },
        "fetch_safety": {
            "allow_private_addresses": config.ALLOW_PRIVATE_ADDRESSES,
            "max_redirects": config.MAX_REDIRECTS,
        },
    })


def run(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the MCP server."""
    if transport in ("http", "streamable-http", "sse"):
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http" if transport == "http" else transport)
    else:
        mcp.run(transport="stdio")
