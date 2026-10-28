"""News engines backed by public RSS feeds. All keyless.

RSS is the most robust keyless search surface available: the payload is
structured XML rather than scraped HTML, so a redesign cannot break the parser,
and the feeds are meant to be polled by machines (no anti-bot wall).

Link handling is the interesting part. Google News returns opaque
``news.google.com/rss/articles/CBMi...`` links whose real URL is only computed
by JavaScript — verified: the wrapper page contains no article URL, and the
``batchexecute`` RPC that used to decode them now answers ``null``. Rather than
return a link the caller cannot open, the Google engine reports the publisher
host in the URL field and keeps the wrapper as ``meta["google_news_url"]``.
Bing News wraps links in ``apiclick.aspx?...&url=<percent-encoded>``, which
*is* decodable, so that engine returns real destinations.
"""
from __future__ import annotations

import urllib.parse

from .. import net
from ..types import EngineError, Result
from .base import Engine, register

# Google News caps a feed at 100 items regardless of what is asked for.
_MAX_ITEMS = 100


def _clean(text) -> str:
    import re

    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def _rss_items(url: str, params: dict, *, engine: str,
               timeout: float | None) -> list:
    """Fetch an RSS feed and return its ``<item>`` elements."""
    from bs4 import BeautifulSoup

    resp = net.get(url, params=params, timeout=timeout, engine=engine)
    soup = BeautifulSoup(resp.text, "xml")
    items = soup.find_all("item")
    if not items:
        # Some feeds answer 200 with an error document. Distinguish a real
        # outage from a query with genuinely no coverage: a feed that parsed as
        # XML but carries no items is empty, not broken.
        if soup.find("rss") is not None or soup.find("feed") is not None:
            return []
        raise EngineError(engine, "response is not an RSS document")
    return items


def _child_text(item, *names: str) -> str:
    for name in names:
        el = item.find(name)
        if el is not None:
            text = el.get_text(" ", strip=True)
            if text:
                return _clean(text)
    return ""


def _html_to_plain(text: str) -> str:
    """Turn an RSS description into plain text.

    RSS descriptions are escaped HTML: Google News puts an ``<a href=...>``
    around the headline, Bing wraps the summary in ``<b>``. Returning that raw
    means the caller sees markup instead of a snippet, so it is parsed and
    flattened here.
    """
    if not text or "<" not in text:
        return text
    from bs4 import BeautifulSoup

    return _clean(BeautifulSoup(text, "html.parser").get_text(" ", strip=True))


def search_bing_news(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Bing News via its RSS output (``format=RSS``).

    Each ``<link>`` is a ``bing.com/news/apiclick.aspx?...&url=<encoded>``
    redirect. The ``url`` parameter holds the real article address, so it is
    decoded here; without that the caller would be handed a tracking redirect
    that expires.
    """
    items = _rss_items(
        "https://www.bing.com/news/search",
        {"q": query, "format": "RSS", "count": min(_MAX_ITEMS, max(10, limit))},
        engine="bingnews", timeout=timeout,
    )
    out: list[Result] = []
    for item in items[:limit]:
        title = _child_text(item, "title")
        if not title:
            continue
        raw_link = _child_text(item, "link")
        url = raw_link
        if "apiclick.aspx" in raw_link:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_link).query)
            url = (qs.get("url") or [""])[0] or raw_link
        if not url.startswith("http"):
            continue
        snippet = _html_to_plain(_child_text(item, "description"))
        published = _child_text(item, "pubDate")
        meta: dict = {}
        if url != raw_link:
            meta["rss_link"] = raw_link
        out.append(Result(title, url, snippet, "bingnews",
                          published=published, meta=meta))
    if not out:
        raise EngineError("bingnews", "0 results")
    return out


def search_gnews(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Google News RSS search.

    The article link Google hands out is an opaque
    ``news.google.com/rss/articles/<id>`` URL that only resolves after
    JavaScript runs. Since the caller needs something it can actually open,
    results point at the publisher's home page (taken from the ``<source>``
    element, which *is* the real publisher) and the undecodable wrapper is kept
    in ``meta``. ``fetch_url`` on the wrapper would return a JS shell, so
    returning it as the primary URL would be actively misleading.
    """
    items = _rss_items(
        "https://news.google.com/rss/search",
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
        engine="gnews", timeout=timeout,
    )
    out: list[Result] = []
    for item in items[:limit]:
        title = _child_text(item, "title")
        if not title:
            continue
        wrapper = _child_text(item, "link")
        source_el = item.find("source")
        publisher_url = _clean(source_el.get("url", "")) if source_el is not None else ""
        publisher = _child_text(item, "source")
        url = publisher_url or wrapper
        if not url.startswith("http"):
            continue
        # Google's description is a short HTML anchor + the publisher name.
        snippet = _html_to_plain(_child_text(item, "description"))
        meta = {"publisher": publisher} if publisher else {}
        if wrapper and wrapper != url:
            meta["google_news_url"] = wrapper
        out.append(Result(title, url, snippet, "gnews",
                          published=_child_text(item, "pubDate"), meta=meta))
    if not out:
        raise EngineError("gnews", "0 results")
    return out


register(Engine("bingnews", search_bing_news, "api", groups=("news", "web"),
                probe_query="technology", index_family="bing",
                note="Bing News — RSS, decodes apiclick links to real articles"))
register(Engine("gnews", search_gnews, "api", groups=("news",),
                probe_query="technology",
                note="Google News — RSS; links resolve to the publisher, not the wrapper"))
