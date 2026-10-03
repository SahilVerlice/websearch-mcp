#!/usr/bin/env python3
"""
websearch-mcp — a self-hosted web search MCP server.

Searches the web through search engines that require NO API key, and also
fetches pages as clean markdown/text. No third-party search API, no billing.

Engines that work reliably from a datacenter IP (verified):
    brave      HTML    https://search.brave.com/search
    bing       HTML    https://www.bing.com/search
    yahoo      HTML    https://search.yahoo.com/search
    wiby       HTML    https://wiby.me/            (indie/old-web index)
    wikipedia  API     MediaWiki action=query
    mwmbl      API     https://api.mwmbl.org       (open-source crawler)
    github     API     https://api.github.com
    stackexchange API  https://api.stackexchange.com
    hn         API     Hacker News via Algolia
    lemmy      API     https://lemmy.world         (federated forums)
    openverse  API     https://api.openverse.org  (CC-licensed media)
    arxiv      API     http://export.arxiv.org
    crossref   API     https://api.crossref.org
    openlibrary API    https://openlibrary.org
    pubmed     API     NCBI E-utilities
    wikidata   API     https://www.wikidata.org

Engines that are known to block datacenter IPs (captcha / JS-only) and are
therefore NOT enabled by default: google, duckduckgo, mojeek, startpage,
ecosia, searx instances, yandex, baidu.

Run:
    python3 server.py              # stdio MCP server
    python3 server.py --selftest   # run every engine once and print results
    python3 server.py --list       # list registered engines
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sys
import urllib.parse
import warnings
from dataclasses import dataclass, field
from typing import Callable

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
# httpx logs every request at INFO; keep the MCP stdio channel quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    print(f"FATAL: the 'mcp' python package is required: {exc}", file=sys.stderr)
    raise

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

_ua_idx = 0


def _headers(extra: dict | None = None) -> dict:
    global _ua_idx
    h = dict(BASE_HEADERS)
    h["User-Agent"] = USER_AGENTS[_ua_idx % len(USER_AGENTS)]
    _ua_idx += 1
    if extra:
        h.update(extra)
    return h


def http_get(url: str, *, params: dict | None = None, extra: dict | None = None,
             timeout: float = 20.0) -> httpx.Response:
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=_headers(extra)) as c:
        return c.get(url, params=params)


def http_post(url: str, data: dict, *, extra: dict | None = None,
              timeout: float = 20.0) -> httpx.Response:
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=_headers(extra)) as c:
        return c.post(url, data=data)


def soup_of(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    rank: int = 0
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"title": self.title, "url": self.url, "snippet": self.snippet, "source": self.source}
        if self.meta:
            d.update(self.meta)
        return d


# --------------------------------------------------------------------------- #
# HTML engine parsers
# --------------------------------------------------------------------------- #
def parse_brave(q: str, limit: int) -> list[Result]:
    r = http_get("https://search.brave.com/search", params={"q": q, "source": "web"})
    s = soup_of(r.text)
    out: list[Result] = []
    for sn in s.select("div.snippet[data-type=web], div.snippet"):
        a = sn.select_one("a.l1") or sn.select_one("a[href^=http]")
        if not a:
            continue
        url = a.get("href", "")
        title_el = sn.select_one("div.title")
        title = clean(title_el.get_text()) if title_el else clean(a.get_text())
        if not title:
            continue
        # description lives in a sibling/child block; grab the longest text block
        desc = ""
        for sel in ["div.snippet-description", "div.description", "div.snippet-content",
                    "p", "div.content"]:
            el = sn.select_one(sel)
            if el:
                desc = clean(el.get_text())
                if desc:
                    break
        out.append(Result(title, url, desc, "brave"))
        if len(out) >= limit:
            break
    return out


def parse_bing(q: str, limit: int) -> list[Result]:
    r = http_get("https://www.bing.com/search", params={"q": q, "count": max(limit, 10)})
    s = soup_of(r.text)
    out: list[Result] = []
    for li in s.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        url = decode_bing_url(a.get("href", ""))
        title = clean(a.get_text())
        if not title:
            continue
        cap = li.select_one(".b_caption p") or li.select_one("p") or li.select_one("div.b_caption")
        desc = clean(cap.get_text()) if cap else ""
        out.append(Result(title, url, desc, "bing"))
        if len(out) >= limit:
            break
    return out


def decode_bing_url(href: str) -> str:
    """Bing wraps result links in /ck/a?...&u=a1<base64url>."""
    if not href:
        return href
    m = re.search(r"[?&]u=a1([A-Za-z0-9_\-]+)", href)
    if m:
        b = m.group(1)
        b += "=" * (-len(b) % 4)
        try:
            return base64.urlsafe_b64decode(b).decode("utf-8", "replace")
        except Exception:
            return href
    return href


def parse_yahoo(q: str, limit: int) -> list[Result]:
    r = http_get("https://search.yahoo.com/search", params={"p": q, "n": max(limit, 10)})
    s = soup_of(r.text)
    out: list[Result] = []
    for d in s.select("div.algo, div.dd.algo"):
        h3 = d.select_one("h3")
        a = d.select_one("h3 a[href]") or d.select_one("a[href]")
        if not h3 or not a:
            continue
        url = decode_yahoo_url(a.get("href", ""))
        title = clean(h3.get_text())
        if not title:
            continue
        p = d.select_one("div.compText p") or d.select_one("p") or d.select_one("div.compText")
        desc = clean(p.get_text()) if p else ""
        out.append(Result(title, url, desc, "yahoo"))
        if len(out) >= limit:
            break
    return out


def decode_yahoo_url(href: str) -> str:
    if "r.search.yahoo.com" not in href:
        return href
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    ru = qs.get("RU", [""])[0]
    if ru:
        return urllib.parse.unquote(ru)
    # newer yahoo format: /RU=<encoded>/RK=...
    m = re.search(r"/RU=([^/]+)", href)
    if m:
        return urllib.parse.unquote(m.group(1))
    return href


def parse_wiby(q: str, limit: int) -> list[Result]:
    r = http_get("https://wiby.me/", params={"q": q})
    s = soup_of(r.text)
    out: list[Result] = []
    for a in s.select("a.tlink[href^=http]"):
        url = a.get("href", "")
        title = clean(a.get_text())
        if not title or not url:
            continue
        out.append(Result(title, url, "", "wiby"))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# JSON API parsers
# --------------------------------------------------------------------------- #
def parse_wikipedia(q: str, limit: int) -> list[Result]:
    r = http_get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "list": "search", "srsearch": q,
        "format": "json", "srlimit": limit,
    })
    out = []
    for item in r.json().get("query", {}).get("search", []):
        title = item.get("title", "")
        out.append(Result(
            title,
            "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
            clean(re.sub(r"<[^>]+>", "", item.get("snippet", ""))),
            "wikipedia",
        ))
    return out


def parse_mwmbl(q: str, limit: int) -> list[Result]:
    r = http_get("https://api.mwmbl.org/api/v1/search/", params={"s": q})
    data = r.json()
    if isinstance(data, dict):
        data = data.get("results", [])
    out = []
    for item in data[:limit]:
        def _frag(v):
            if isinstance(v, list):
                return clean("".join(str(x.get("value", "")) for x in v if isinstance(x, dict)))
            return clean(str(v or ""))
        out.append(Result(_frag(item.get("title")), item.get("url", ""), _frag(item.get("extract")), "mwmbl"))
    return out


def parse_github(q: str, limit: int) -> list[Result]:
    r = http_get("https://api.github.com/search/repositories",
                 params={"q": q, "per_page": limit},
                 extra={"Accept": "application/vnd.github+json"})
    out = []
    for it in r.json().get("items", [])[:limit]:
        stars = it.get("stargazers_count", 0)
        lang = it.get("language") or "?"
        out.append(Result(
            it.get("full_name", ""),
            it.get("html_url", ""),
            f"[{lang} ★{stars}] " + clean(it.get("description")),
            "github",
        ))
    return out


def parse_stackexchange(q: str, limit: int) -> list[Result]:
    r = http_get("https://api.stackexchange.com/2.3/search/advanced", params={
        "order": "desc", "sort": "relevance", "q": q,
        "site": "stackoverflow", "pagesize": limit,
    })
    out = []
    for it in r.json().get("items", [])[:limit]:
        out.append(Result(
            clean(re.sub(r"<[^>]+>", "", it.get("title", ""))),
            it.get("link", ""),
            f"score={it.get('score', 0)} answers={it.get('answer_count', 0)} "
            f"accepted={bool(it.get('is_answered'))}",
            "stackexchange",
        ))
    return out


def parse_hn(q: str, limit: int) -> list[Result]:
    r = http_get("https://hn.algolia.com/api/v1/search", params={"query": q, "hitsPerPage": limit})
    out = []
    for it in r.json().get("hits", [])[:limit]:
        url = it.get("url") or f"https://news.ycombinator.com/item?id={it.get('objectID')}"
        title = it.get("title") or it.get("story_title") or ""
        if not title:
            continue
        out.append(Result(clean(title), url,
                          f"points={it.get('points', 0)} comments={it.get('num_comments', 0)}", "hn"))
    return out


def parse_lemmy(q: str, limit: int) -> list[Result]:
    """Lemmy federated forum search (no key)."""
    r = http_get("https://lemmy.world/api/v3/search", params={
        "q": q, "type_": "Posts", "limit": limit, "sort": "TopAll",
    })
    out = []
    for wrapper in r.json().get("posts", [])[:limit]:
        p = wrapper.get("post", {})
        name = p.get("name") or ""
        if not name:
            continue
        url = p.get("url") or p.get("ap_id") or ""
        counts = wrapper.get("counts", {})
        comm = (wrapper.get("community") or {}).get("name", "")
        out.append(Result(name, url,
                          f"c/{comm} score={counts.get('score', 0)} "
                          f"comments={counts.get('comments', 0)}", "lemmy"))
    return out


def parse_openverse(q: str, limit: int) -> list[Result]:
    """Openverse — openly-licensed media search (no key)."""
    r = http_get("https://api.openverse.org/v1/images/", params={"q": q, "page_size": limit})
    out = []
    for it in r.json().get("results", [])[:limit]:
        out.append(Result(
            clean(it.get("title") or "Untitled image"),
            it.get("foreign_landing_url") or it.get("url", ""),
            f"by {it.get('creator','?')} ({it.get('license','?')}) [{it.get('provider','')}]",
            "openverse",
        ))
    return out


def parse_arxiv(q: str, limit: int) -> list[Result]:
    r = http_get("http://export.arxiv.org/api/query", params={
        "search_query": f"all:{q}", "max_results": limit,
    })
    s = BeautifulSoup(r.text, "xml")
    out = []
    for e in s.find_all("entry"):
        title = clean(e.title.get_text()) if e.title else ""
        link = e.id.get_text() if e.id else ""
        summ = clean(e.summary.get_text()) if e.summary else ""
        if title:
            out.append(Result(title, link, summ, "arxiv"))
    return out


def parse_crossref(q: str, limit: int) -> list[Result]:
    r = http_get("https://api.crossref.org/works", params={"query": q, "rows": limit})
    out = []
    for it in r.json().get("message", {}).get("items", [])[:limit]:
        title = clean((it.get("title") or [""])[0])
        url = it.get("URL") or ("https://doi.org/" + it.get("DOI", "") if it.get("DOI") else "")
        if title:
            out.append(Result(title, url, f"DOI={it.get('DOI','')}", "crossref"))
    return out


def parse_openlibrary(q: str, limit: int) -> list[Result]:
    r = http_get("https://openlibrary.org/search.json", params={"q": q, "limit": limit})
    out = []
    for it in r.json().get("docs", [])[:limit]:
        key = it.get("key", "")
        authors = ", ".join(it.get("author_name", [])[:2])
        out.append(Result(clean(it.get("title", "")), "https://openlibrary.org" + key,
                          f"by {authors}" if authors else "", "openlibrary"))
    return out


def parse_pubmed(q: str, limit: int) -> list[Result]:
    r = http_get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                 params={"db": "pubmed", "term": q, "retmax": limit, "retmode": "json"})
    ids = r.json().get("esearchresult", {}).get("idlist", [])
    out = []
    for pmid in ids:
        out.append(Result(f"PubMed {pmid}", f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                          "", "pubmed"))
    return out


def parse_wikidata(q: str, limit: int) -> list[Result]:
    r = http_get("https://www.wikidata.org/w/api.php", params={
        "action": "wbsearchentities", "search": q, "language": "en",
        "format": "json", "limit": limit,
    })
    out = []
    for it in r.json().get("search", [])[:limit]:
        out.append(Result(it.get("label", it.get("id", "")), it.get("concepturi") or it.get("url", ""),
                          clean(it.get("description")), "wikidata"))
    return out


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
@dataclass
class Engine:
    name: str
    parser: Callable[[str, int], list[Result]]
    kind: str          # "html" | "api"
    reliable: bool     # works from datacenter IP without keys
    note: str = ""


ENGINES: dict[str, Engine] = {}


def register(name, parser, kind, reliable, note=""):
    ENGINES[name] = Engine(name, parser, kind, reliable, note)


register("brave", parse_brave, "html", True, "Brave Search — HTML, no key")
register("bing", parse_bing, "html", True, "Bing — HTML, no key")
register("yahoo", parse_yahoo, "html", True, "Yahoo Search — HTML, no key")
register("wiby", parse_wiby, "html", True, "Wiby — indie/old-web index")
register("wikipedia", parse_wikipedia, "api", True, "Wikipedia — MediaWiki API")
register("mwmbl", parse_mwmbl, "api", True, "Mwmbl — open-source crawler API")
register("github", parse_github, "api", True, "GitHub repo search API")
register("stackexchange", parse_stackexchange, "api", True, "Stack Exchange API")
register("hn", parse_hn, "api", True, "Hacker News (Algolia)")
register("lemmy", parse_lemmy, "api", True, "Lemmy federated forums (lemmy.world)")
register("openverse", parse_openverse, "api", True, "Openverse — CC-licensed images")
register("arxiv", parse_arxiv, "api", True, "arXiv papers")
register("crossref", parse_crossref, "api", True, "Crossref scholarly works")
register("openlibrary", parse_openlibrary, "api", True, "Open Library books")
register("pubmed", parse_pubmed, "api", True, "PubMed (NCBI)")
register("wikidata", parse_wikidata, "api", True, "Wikidata entities")

# Known-blocked / JS-only from a datacenter IP. Registered so callers get a clear
# message instead of a silent failure.
BLOCKED = {
    "google": "HTML is JS-gated / bot-blocked from datacenter IPs (needs an API key)",
    "duckduckgo": "returns an anti-bot challenge page from datacenter IPs",
    "mojeek": "captcha from datacenter IPs",
    "startpage": "captcha from datacenter IPs",
    "ecosia": "403 from datacenter IPs",
    "searx": "public instances rate-limit / captcha",
    "yandex": "captcha from datacenter IPs",
    "baidu": "bot-blocked from datacenter IPs",
}

DEFAULT_ENGINES = ["brave", "bing", "yahoo"]


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def normalize_url(u: str) -> str:
    if not u:
        return u
    try:
        p = urllib.parse.urlsplit(u)
        host = p.netloc.lower().removeprefix("www.")
        path = p.path.rstrip("/")
        return f"{host}{path}"
    except Exception:
        return u


def run_engine(name: str, query: str, limit: int) -> tuple[list[Result], str | None]:
    eng = ENGINES.get(name)
    if not eng:
        if name in BLOCKED:
            return [], f"'{name}' is disabled: {BLOCKED[name]}"
        return [], f"unknown engine '{name}'"
    try:
        res = eng.parser(query, limit)
        for i, r in enumerate(res):
            r.rank = i + 1
            r.source = name
        return res, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {str(exc)[:160]}"


def aggregate(query: str, engines: list[str], limit: int, per_engine: int | None = None):
    per_engine = per_engine or limit
    by_engine: dict[str, list[Result]] = {}
    errors: dict[str, str] = {}
    per_source: dict[str, int] = {}
    for name in engines:
        res, err = run_engine(name, query, per_engine)
        if err:
            errors[name] = err
            continue
        per_source[name] = len(res)
        by_engine[name] = res

    # Round-robin across engines so a single source cannot dominate the list.
    merged: list[Result] = []
    seen: set[str] = set()
    order = list(by_engine.keys())
    idx = 0
    while order and len(merged) < limit:
        name = order[idx % len(order)]
        bucket = by_engine[name]
        added = False
        while bucket:
            r = bucket.pop(0)
            key = normalize_url(r.url)
            if key and key not in seen:
                seen.add(key)
                merged.append(r)
                added = True
                break
        if not bucket:
            order.remove(name)
            continue
        if added:
            idx += 1
    return merged[:limit], errors, per_source


# --------------------------------------------------------------------------- #
# Page fetching
# --------------------------------------------------------------------------- #
def html_to_text(html: str) -> tuple[str, str]:
    s = soup_of(html)
    for tag in s(["script", "style", "noscript", "svg", "header", "footer", "nav", "form"]):
        tag.decompose()
    title = clean(s.title.get_text()) if s.title else ""
    main = s.select_one("main") or s.select_one("article") or s.body or s
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
mcp = FastMCP("websearch")


@mcp.tool()
def web_search(query: str, limit: int = 10, engines: str = "") -> str:
    """Search the web using keyless search engines and return aggregated results.

    Args:
        query: The search query.
        limit: Maximum number of results to return (default 10).
        engines: Optional comma-separated engine list. Default is
            "brave,bing,yahoo". Available: brave, bing, yahoo, wiby, wikipedia,
            mwmbl, github, stackexchange, hn, lemmy, openverse, arxiv, crossref,
            openlibrary, pubmed, wikidata. Use "all" for every enabled engine.
    """
    if engines.strip().lower() == "all":
        names = list(ENGINES.keys())
    elif engines.strip():
        names = [e.strip().lower() for e in engines.split(",") if e.strip()]
    else:
        names = DEFAULT_ENGINES
    merged, errors, per_source = aggregate(query, names, limit)
    payload = {
        "query": query,
        "engines_used": names,
        "per_engine_counts": per_source,
        "result_count": len(merged),
        "results": [r.as_dict() for r in merged],
        "errors": errors,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


@mcp.tool()
def search_engine(engine: str, query: str, limit: int = 10) -> str:
    """Run a single named search engine and return its raw results.

    Args:
        engine: One of: brave, bing, yahoo, wiby, wikipedia, mwmbl, github,
            stackexchange, hn, lemmy, openverse, arxiv, crossref, openlibrary, pubmed,
            wikidata.
        query: The search query.
        limit: Maximum results (default 10).
    """
    res, err = run_engine(engine.strip().lower(), query, limit)
    return json.dumps({
        "engine": engine,
        "query": query,
        "result_count": len(res),
        "results": [r.as_dict() for r in res],
        "error": err,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def fetch_url(url: str, max_chars: int = 20000) -> str:
    """Fetch a URL and return its readable text (scripts/styles stripped).

    Args:
        url: The URL to fetch.
        max_chars: Truncate the extracted text to this many characters.
    """
    try:
        r = http_get(url, timeout=25.0)
    except Exception as exc:
        return json.dumps({"url": url, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    ctype = r.headers.get("content-type", "")
    if "application/json" in ctype:
        return json.dumps({"url": url, "status": r.status_code,
                           "content_type": ctype, "text": r.text[:max_chars]},
                          indent=2, ensure_ascii=False)
    title, text = html_to_text(r.text)
    truncated = len(text) > max_chars
    return json.dumps({
        "url": str(r.url), "status": r.status_code, "content_type": ctype,
        "title": title, "truncated": truncated, "text": text[:max_chars],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def list_engines() -> str:
    """List the search engines this server can use, and which are blocked."""
    enabled = [{"name": e.name, "kind": e.kind, "note": e.note} for e in ENGINES.values()]
    return json.dumps({
        "default_engines": DEFAULT_ENGINES,
        "enabled": enabled,
        "blocked_from_datacenter_ips": BLOCKED,
    }, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def selftest(query: str = "python tutorial"):
    print(f"selftest query: {query!r}\n")
    ok = 0
    for name in ENGINES:
        res, err = run_engine(name, query, 5)
        if err:
            print(f"[FAIL] {name:14} {err}")
        elif res:
            ok += 1
            top = res[0]
            print(f"[ OK ] {name:14} {len(res):>2} results | {top.title[:52]}")
            print(f"        {top.url[:88]}")
        else:
            print(f"[WARN] {name:14} 0 results (parsed nothing)")
    print(f"\n{ok}/{len(ENGINES)} engines returned results")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Keyless web search MCP server")
    ap.add_argument("--selftest", action="store_true", help="test every engine and exit")
    ap.add_argument("--query", default="python tutorial", help="query for --selftest")
    ap.add_argument("--list", action="store_true", help="list engines and exit")
    ap.add_argument("--search", metavar="QUERY", help="one-shot CLI search (no MCP), prints JSON")
    ap.add_argument("--engines", default="", help="comma-separated engines for --search")
    ap.add_argument("--limit", type=int, default=10, help="result limit for --search")
    ap.add_argument("--fetch", metavar="URL", help="one-shot CLI page fetch, prints JSON")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest(args.query))
    if args.list:
        for e in ENGINES.values():
            print(f"{e.name:14} {e.kind:4} {e.note}")
        print("\nblocked:")
        for k, v in BLOCKED.items():
            print(f"  {k:14} {v}")
        sys.exit(0)
    if args.search:
        names = ([e.strip().lower() for e in args.engines.split(",") if e.strip()]
                 or DEFAULT_ENGINES)
        print(web_search(args.search, limit=args.limit, engines=",".join(names)))
        sys.exit(0)
    if args.fetch:
        print(fetch_url(args.fetch))
        sys.exit(0)
    mcp.run()


if __name__ == "__main__":
    main()
