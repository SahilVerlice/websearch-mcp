"""HTML-scraping engines. Selectors verified live; see each docstring."""
from __future__ import annotations

import base64
import re
import urllib.parse

from .. import net
from ..extract import html_to_text  # noqa: F401  (re-exported for engines that need it)
from ..urls import canonical_url
from ..types import BlockedError, EngineError, Result
from .base import Engine, register

UA_DESKTOP = net.USER_AGENTS[0]


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _absolute(url: str, base: str = "") -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return urllib.parse.urljoin(base, url)
    return url


# --------------------------------------------------------------------------- #
# DuckDuckGo Lite
# --------------------------------------------------------------------------- #
DDG_SPONSORED_SELECTORS = "tr.result-sponsored"


def search_duckduckgo(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """DuckDuckGo Lite — table-based HTML, no JS, no key.

    Verified: result rows are ``tr`` elements in the last ``<table>``; the title
    anchor carries ``.result-link`` and wraps ``//duckduckgo.com/l/?uddg=<url>``.
    Sponsored rows are marked ``tr.result-sponsored`` (their anchors also carry
    ``.result-link``, so they must be filtered before parsing).
    Anomaly pages answer HTTP 202 and contain ``form#challenge-form``.
    """
    resp = net.post(
        "https://lite.duckduckgo.com/lite/",
        data={"q": query},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Referer": "https://lite.duckduckgo.com/"},
        timeout=timeout,
        engine="duckduckgo",
        allow_status=(202,),
    )
    if resp.status_code == 202:
        raise BlockedError("duckduckgo", "anti-bot anomaly page (HTTP 202)", status=202)

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(resp.text, "html.parser")
    if soup.select_one("form#challenge-form") or soup.select_one("form#img-form"):
        raise BlockedError("duckduckgo", "anti-bot challenge page")

    tables = soup.select("table")
    if not tables:
        raise EngineError("duckduckgo", "no results table in response")
    table = tables[-1]

    out: list[Result] = []
    for row in table.select("tr"):
        if "result-sponsored" in (row.get("class") or []):
            continue
        anchor = row.select_one("a.result-link")
        if not anchor:
            continue
        href = anchor.get("href", "")
        title = _clean(anchor.get_text(" ", strip=True))
        if not title or title.lower().startswith("more info"):
            continue
        url = _decode_ddg(href)
        if not url or _is_ddg_ad(url):
            continue
        snippet = ""
        snip_td = row.select_one("td.result-snippet")
        if snip_td:
            snippet = _clean(snip_td.get_text(" ", strip=True))
        out.append(Result(title, url, snippet, "duckduckgo"))
        if len(out) >= limit:
            break
    if not out:
        raise EngineError("duckduckgo", "0 results parsed (markup may have changed)")
    return out


def _decode_ddg(href: str) -> str:
    href = _absolute(href, "https://lite.duckduckgo.com/")
    if "uddg=" not in href:
        return href
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
    raw = qs.get("uddg", [""])[0]
    return urllib.parse.unquote(raw) if raw else href


def _is_ddg_ad(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return True
    host = (p.hostname or "").lower()
    if host.endswith("duckduckgo.com") and p.path.startswith("/y.js"):
        return True
    if "bing.com" in host and p.path.startswith("/aclick"):
        return True
    return "ad_provider=" in url or "ad_domain=" in url


# --------------------------------------------------------------------------- #
# Bing
# --------------------------------------------------------------------------- #
def search_bing(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Bing HTML. Verified: ``li.b_algo`` -> ``h2 a`` + ``.b_caption p``.

    Pagination is attempted but currently ineffective: from this client Bing
    returns the same first page for every ``first=`` / ``count=`` value, so the
    loop dedupes and stops at ~10 hits rather than repeating them.

    The market parameters (``cc``, ``mkt``, ``setlang``) are deliberately NOT
    sent. Measured from a datacenter IP: a bare ``q`` returns 20/20 on-topic
    results, while adding ``cc=us`` (or ``mkt=en-US``) returns 0/20 on-topic —
    Bing serves a generic, unrelated SERP instead. ``Accept-Language`` is
    enough to get English results.
    """
    out: list[Result] = []
    seen: set[str] = set()
    first = 1
    pages = 0
    while len(out) < limit and pages < 3:
        resp = net.get(
            "https://www.bing.com/search",
            params={"q": query, "count": min(50, max(10, limit)), "first": first},
            headers={"Accept-Language": "en-US,en;q=0.9"},
            timeout=timeout,
            engine="bing",
        )
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "lxml")
        added = 0
        for li in soup.select("li.b_algo"):
            a = li.select_one("h2 a") or li.select_one("a[href^=http]")
            if not a:
                continue
            title = _clean(a.get_text(" ", strip=True))
            url = decode_bing_url(a.get("href", ""))
            if not title or not url.startswith("http"):
                continue
            # Bing currently ignores both ``first`` and ``count`` from this
            # kind of client: every page returns the same 10 results. Dedupe
            # so a limit > 10 does not fill the list with copies, and stop as
            # soon as a page adds nothing new.
            key = canonical_url(url)
            if key in seen:
                continue
            seen.add(key)
            cap = (li.select_one(".b_caption p") or li.select_one("div.b_caption")
                   or li.select_one("p"))
            snippet = _clean(cap.get_text(" ", strip=True)) if cap else ""
            out.append(Result(title, url, snippet, "bing"))
            added += 1
            if len(out) >= limit:
                break
        if added == 0:
            break
        first += 10
        pages += 1
    if not out:
        raise EngineError("bing", "0 results parsed (markup may have changed)")
    return out


def decode_bing_url(href: str) -> str:
    """Bing wraps links as ``/ck/a?...&u=a1<base64url>``."""
    if not href:
        return href
    href = _absolute(href, "https://www.bing.com")
    m = re.search(r"[?&]u=a1([A-Za-z0-9_\-]+)", href)
    if m:
        b = m.group(1) + "=" * (-len(m.group(1)) % 4)
        try:
            return base64.urlsafe_b64decode(b).decode("utf-8", "replace")
        except Exception:
            return href
    return href


# --------------------------------------------------------------------------- #
# Yahoo
# --------------------------------------------------------------------------- #
def search_yahoo(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Yahoo Search HTML. Verified: ``div.algo`` -> ``h3 a`` + ``div.compText p``."""
    resp = net.get(
        "https://search.yahoo.com/search",
        params={"p": query, "n": min(100, max(10, limit))},
        timeout=timeout,
        engine="yahoo",
    )
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(resp.text, "lxml")
    out: list[Result] = []
    for d in soup.select("div.algo, div.dd.algo"):
        h3 = d.select_one("h3")
        a = d.select_one("h3 a[href]") or d.select_one("a[href]")
        if not h3 or not a:
            continue
        title = _clean(h3.get_text(" ", strip=True))
        url = decode_yahoo_url(a.get("href", ""))
        if not title or not url.startswith("http"):
            continue
        p = d.select_one("div.compText p") or d.select_one("div.compText") or d.select_one("p")
        snippet = _clean(p.get_text(" ", strip=True)) if p else ""
        out.append(Result(title, url, snippet, "yahoo"))
        if len(out) >= limit:
            break
    if not out:
        raise EngineError("yahoo", "0 results parsed (markup may have changed)")
    return out


def decode_yahoo_url(href: str) -> str:
    if "r.search.yahoo.com" not in href and "/RU=" not in href:
        return _absolute(href, "https://search.yahoo.com")
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
    ru = qs.get("RU", [""])[0]
    if ru:
        return urllib.parse.unquote(ru)
    m = re.search(r"/RU=([^/]+)", href)
    if m:
        return urllib.parse.unquote(m.group(1))
    return href


# --------------------------------------------------------------------------- #
# Wiby (indie / old-web index)
# --------------------------------------------------------------------------- #
def search_wiby(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Wiby — indie / old-web index.

    Wiby exposes a JSON endpoint (``/json/``) that returns ``[{URL, Title,
    Snippet, Description}]``. It is used first because the HTML page is tiny
    and its shape changes; the HTML path remains as a fallback.

    A genuine no-match is *not* an error. Wiby answers an empty corpus with a
    1 KB page saying "That's everything I could find." — the old code treated
    that as "markup may have changed", which is both wrong and alarming. Only
    a page that has neither results nor the no-results marker is a parse
    failure.
    """
    out: list[Result] = []
    try:
        data = net.get_json("https://wiby.me/json/", params={"q": query},
                            timeout=timeout, engine="wiby")
    except EngineError:
        data = None
    if isinstance(data, list):
        for item in data[:limit]:
            if not isinstance(item, dict):
                continue
            url = item.get("URL") or item.get("url") or ""
            title = _clean(item.get("Title") or item.get("title"))
            if not url or not title:
                continue
            snippet = _clean(item.get("Snippet") or item.get("Description") or "")
            out.append(Result(title, url, snippet, "wiby"))
        if out:
            return out

    from bs4 import BeautifulSoup

    resp = net.get("https://wiby.me/", params={"q": query}, timeout=timeout,
                   engine="wiby")
    soup = BeautifulSoup(resp.text, "lxml")
    for a in soup.select("a.tlink[href^=http]"):
        title = _clean(a.get_text(" ", strip=True))
        url = a.get("href", "")
        if not title or not url:
            continue
        # Wiby renders the description in a sibling <p>.
        snippet = ""
        parent = a.find_parent()
        if parent:
            p = parent.find_next_sibling("p") or parent.find("p")
            if p:
                snippet = _clean(p.get_text(" ", strip=True))
        out.append(Result(title, url, snippet, "wiby"))
        if len(out) >= limit:
            break
    if out:
        return out

    # No hits and the page says so: an honest empty result, not a bug.
    page_text = soup.get_text(" ", strip=True).lower()
    if "that's everything i could find" in page_text or not page_text:
        return []
    raise EngineError("wiby", "0 results parsed (markup may have changed)")


# --------------------------------------------------------------------------- #
# Brave (frequently rate-limited from datacenter IPs)
# --------------------------------------------------------------------------- #
BRAVE_HEADERS = {
    # Brave is UA-sensitive in a way that breaks naive rotation: every
    # non-Firefox UA (including Chrome, Edge and Brave's own) gets a permanent
    # 429 wall. A Firefox UA plus the Sec-Fetch-* navigation headers is what
    # makes the page load, so this engine deliberately opts out of the shared
    # rotating UA pool.
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def search_brave(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Brave Search HTML.

    ``div.snippet`` also matches the LLM-answer block, video rail and other
    modules, so results must be scoped to ``[data-type="web"]`` (20 per page).
    Snippet text lives in ``.generic-snippet .content``; the old
    ``.snippet-description`` selector is dead.
    """
    resp = net.get(
        "https://search.brave.com/search",
        params={"q": query, "source": "web"},
        headers=BRAVE_HEADERS,
        timeout=timeout,
        engine="brave",
    )
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[Result] = []
    for sn in soup.select("div.snippet[data-type='web']"):
        a = sn.select_one("a.l1") or sn.select_one("a[href^=http]")
        if not a:
            continue
        title_el = sn.select_one("div.title")
        title = _clean(title_el.get_text(" ", strip=True)) if title_el else _clean(a.get_text(" ", strip=True))
        url = a.get("href", "")
        if not title or not url.startswith("http"):
            continue
        desc_el = sn.select_one(".generic-snippet .content")
        if desc_el is None:
            for sel in ("div.snippet-description", "div.description", "p"):
                desc_el = sn.select_one(sel)
                if desc_el is not None:
                    break
        desc = _clean(desc_el.get_text(" ", strip=True)) if desc_el else ""
        out.append(Result(title, url, desc, "brave"))
        if len(out) >= limit:
            break
    if not out:
        raise EngineError("brave", "0 results parsed (rate-limited or markup changed)")
    return out


register(Engine("duckduckgo", search_duckduckgo, "html",
                groups=("web", "general"), fast=True, site_syntax=True,
                index_family="bing", note="DuckDuckGo Lite — table HTML, no JS"))
register(Engine("bing", search_bing, "html",
                groups=("web", "general"), fast=True, site_syntax=True,
                index_family="bing", note="Bing — HTML, ~10 results per query"))
register(Engine("yahoo", search_yahoo, "html",
                groups=("web", "general"), fast=True, site_syntax=True,
                index_family="bing", note="Yahoo Search — HTML (Bing index)"))
register(Engine("wiby", search_wiby, "html",
                groups=("web", "indie"), fast=True,
                note="Wiby — indie / old-web index"))
register(Engine("brave", search_brave, "html",
                groups=("web", "general"), fast=True, site_syntax=True,
                index_family="brave", weight=1.2,
                note="Brave — HTML; needs a pinned Firefox UA + Sec-Fetch headers"))
