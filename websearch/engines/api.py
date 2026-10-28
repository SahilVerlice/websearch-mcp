"""JSON/XML API engines. All keyless; endpoints verified live."""
from __future__ import annotations

import html as html_mod
import re
import urllib.parse

from .. import net
from ..types import BlockedError, EngineError, Result
from .base import Engine, register

# ``<[^>]+>`` is quadratic on input with many ``<`` and no closing ``>``: each
# start position scans to the end of the string. Search snippets are short, but
# they are attacker-controlled, so bound the work: only strip tags in the first
# 20k characters and use a linear scan.
TAG_RE = re.compile(r"<[^<>]{0,2000}>")
_MAX_HTML_STRIP = 20_000


def _strip_tags(text: str) -> str:
    if len(text) > _MAX_HTML_STRIP:
        text = text[:_MAX_HTML_STRIP]
    return TAG_RE.sub("", text)


def _clean(text) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def _plain(text) -> str:
    """Strip HTML tags and unescape entities (``&quot;`` -> ``\"``)."""
    return _clean(html_mod.unescape(_strip_tags(str(text or ""))))


def _frag(value) -> str:
    """Mwmbl returns title/extract as lists of {value: ...} fragments."""
    if isinstance(value, list):
        return _clean("".join(str(x.get("value", "")) for x in value if isinstance(x, dict)))
    if isinstance(value, dict):
        return _clean(value.get("value", ""))
    return _clean(value)


# --------------------------------------------------------------------------- #
# Wikipedia / Wikidata
# --------------------------------------------------------------------------- #
def search_wikipedia(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "query", "list": "search", "srsearch": query,
            "format": "json", "srlimit": min(50, limit), "srprop": "snippet|timestamp",
            "utf8": 1,
        },
        timeout=timeout, engine="wikipedia",
    )
    out: list[Result] = []
    for item in data.get("query", {}).get("search", [])[:limit]:
        title = item.get("title", "")
        if not title:
            continue
        url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        out.append(Result(title, url, _plain(item.get("snippet", "")), "wikipedia",
                          published=item.get("timestamp", "")))
    if not out:
        raise EngineError("wikipedia", "0 results")
    return out


def _mediawiki_search(api_url: str, query: str, limit: int, timeout: float | None,
                      source: str, url_for, engine: str) -> list[Result]:
    """Shared MediaWiki ``list=search`` implementation."""
    data = net.get_json(
        api_url,
        params={"action": "query", "list": "search", "srsearch": query,
                "format": "json", "srlimit": min(50, limit), "srprop": "snippet"},
        timeout=timeout, engine=engine,
    )
    out: list[Result] = []
    for item in data.get("query", {}).get("search", [])[:limit]:
        title = item.get("title", "")
        if not title:
            continue
        out.append(Result(title, url_for(title), _plain(item.get("snippet", "")), source))
    if not out:
        raise EngineError(source, "0 results")
    return out


def search_wikinews(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Wikinews — small corpus (~14k articles), so a strict AND search often
    finds nothing. Fall back to MediaWiki's default relevance mode (``cirrus``
    OR-ish matching) before giving up."""
    try:
        return _mediawiki_search(
            "https://en.wikinews.org/w/api.php", query, limit, timeout, "wikinews",
            lambda t: "https://en.wikinews.org/wiki/" + urllib.parse.quote(t.replace(" ", "_")),
            "wikinews",
        )
    except EngineError:
        data = net.get_json(
            "https://en.wikinews.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "format": "json", "srlimit": min(50, limit), "srprop": "snippet",
                    "srwhat": "text"},
            timeout=timeout, engine="wikinews",
        )
        out: list[Result] = []
        for item in data.get("query", {}).get("search", [])[:limit]:
            title = item.get("title", "")
            if title:
                out.append(Result(
                    title,
                    "https://en.wikinews.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
                    _plain(item.get("snippet", "")), "wikinews",
                ))
        if not out:
            raise EngineError("wikinews", "0 results")
        return out


def search_commons(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Wikimedia Commons — freely licensed media files."""
    return _mediawiki_search(
        "https://commons.wikimedia.org/w/api.php", query, limit, timeout, "commons",
        lambda t: "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(t.replace(" ", "_")),
        "commons",
    )


def search_wikidata(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://www.wikidata.org/w/api.php",
        params={
            "action": "wbsearchentities", "search": query, "language": "en",
            "uselang": "en", "format": "json", "limit": min(50, limit),
        },
        timeout=timeout, engine="wikidata",
    )
    out: list[Result] = []
    for it in data.get("search", [])[:limit]:
        qid = it.get("id", "")
        label = it.get("label") or qid
        url = it.get("concepturi") or (f"https://www.wikidata.org/wiki/{qid}" if qid else "")
        if not url:
            continue
        desc = _clean(it.get("description"))
        out.append(Result(label, url, desc, "wikidata", meta={"id": qid}))
    if not out:
        raise EngineError("wikidata", "0 results")
    return out


# --------------------------------------------------------------------------- #
# Developer sources
# --------------------------------------------------------------------------- #
def search_github(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://api.github.com/search/repositories",
        params={"q": query, "per_page": min(100, limit), "sort": "best-match"},
        headers={"Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=timeout, engine="github",
    )
    out: list[Result] = []
    for it in data.get("items", [])[:limit]:
        name = it.get("full_name", "")
        if not name:
            continue
        stars = it.get("stargazers_count", 0)
        lang = it.get("language") or "?"
        topics = ", ".join(it.get("topics", [])[:5])
        snippet = f"[{lang} ★{stars}] " + _clean(it.get("description"))
        if topics:
            snippet += f" — topics: {topics}"
        out.append(Result(name, it.get("html_url", ""), snippet, "github",
                          published=(it.get("pushed_at") or "")[:10],
                          meta={"stars": stars, "language": it.get("language") or ""}))
    if not out:
        raise EngineError("github", "0 results")
    return out


def search_stackexchange(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://api.stackexchange.com/2.3/search/advanced",
        params={
            "order": "desc", "sort": "relevance", "q": query, "site": "stackoverflow",
            "pagesize": min(50, limit), "filter": "default",
        },
        timeout=timeout, engine="stackexchange",
    )
    out: list[Result] = []
    for it in data.get("items", [])[:limit]:
        title = _plain(it.get("title", ""))
        if not title:
            continue
        answered = bool(it.get("is_answered"))
        snippet = (f"score={it.get('score', 0)} answers={it.get('answer_count', 0)} "
                   f"accepted={'yes' if answered else 'no'}")
        out.append(Result(title, it.get("link", ""), snippet, "stackexchange",
                          published=_iso(it.get("creation_date")),
                          meta={"score": it.get("score", 0), "tags": it.get("tags", [])}))
    if not out:
        raise EngineError("stackexchange", "0 results")
    return out


def _iso(epoch) -> str:
    import datetime as _dt

    try:
        return _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def search_hn(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://hn.algolia.com/api/v1/search",
        params={"query": query, "hitsPerPage": min(100, limit), "tags": "(story,comment)"},
        timeout=timeout, engine="hn",
    )
    out: list[Result] = []
    for it in data.get("hits", [])[:limit]:
        title = it.get("title") or it.get("story_title") or ""
        if not title:
            continue
        oid = it.get("objectID")
        url = it.get("url") or it.get("story_url") or f"https://news.ycombinator.com/item?id={oid}"
        points = it.get("points") or 0
        comments = it.get("num_comments") or 0
        out.append(Result(_clean(title), url,
                          f"points={points} comments={comments}", "hn",
                          published=(it.get("created_at") or "")[:10],
                          meta={"points": points, "comments": comments,
                                "hn_id": oid}))
    if not out:
        raise EngineError("hn", "0 results")
    return out


def search_lemmy(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://lemmy.world/api/v3/search",
        params={"q": query, "type_": "Posts", "limit": min(50, limit), "sort": "TopAll"},
        timeout=timeout, engine="lemmy",
    )
    out: list[Result] = []
    for wrapper in data.get("posts", [])[:limit]:
        p = wrapper.get("post", {})
        name = p.get("name") or ""
        if not name:
            continue
        counts = wrapper.get("counts", {})
        comm = (wrapper.get("community") or {}).get("name", "")
        url = p.get("url") or p.get("ap_id") or ""
        out.append(Result(name, url,
                          f"c/{comm} score={counts.get('score', 0)} "
                          f"comments={counts.get('comments', 0)}", "lemmy",
                          published=(p.get("published") or "")[:10]))
    if not out:
        raise EngineError("lemmy", "0 results")
    return out


def search_npm(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://registry.npmjs.org/-/v1/search",
        params={"text": query, "size": min(250, limit)},
        timeout=timeout, engine="npm",
    )
    out: list[Result] = []
    for obj in data.get("objects", [])[:limit]:
        pkg = obj.get("package", {})
        name = pkg.get("name", "")
        if not name:
            continue
        links = pkg.get("links", {})
        out.append(Result(
            name,
            links.get("npm") or f"https://www.npmjs.com/package/{name}",
            f"v{pkg.get('version', '?')} — {_clean(pkg.get('description'))}",
            "npm",
            published=(pkg.get("date") or "")[:10],
            meta={"version": pkg.get("version", ""), "homepage": links.get("homepage", "")},
        ))
    if not out:
        raise EngineError("npm", "0 results")
    return out


def search_crates(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://crates.io/api/v1/crates",
        params={"q": query, "per_page": min(100, limit)},
        # crates.io rejects requests without a descriptive UA.
        headers={"User-Agent": "websearch-mcp/2.0 (keyless search MCP server)"},
        timeout=timeout, engine="crates",
    )
    out: list[Result] = []
    for c in data.get("crates", [])[:limit]:
        name = c.get("name", "")
        if not name:
            continue
        out.append(Result(
            name,
            c.get("repository") or f"https://crates.io/crates/{name}",
            f"v{c.get('max_version', '?')} ↓{c.get('downloads', 0)} — "
            f"{_clean(c.get('description'))}",
            "crates",
            published=(c.get("updated_at") or "")[:10],
            meta={"downloads": c.get("downloads", 0), "version": c.get("max_version", "")},
        ))
    if not out:
        raise EngineError("crates", "0 results")
    return out


def search_gitlab(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://gitlab.com/api/v4/projects",
        params={"search": query, "per_page": min(100, limit), "order_by": "star_count",
                "sort": "desc"},
        timeout=timeout, engine="gitlab",
    )
    out: list[Result] = []
    for it in data[:limit] if isinstance(data, list) else []:
        name = it.get("path_with_namespace") or it.get("name", "")
        if not name:
            continue
        out.append(Result(name, it.get("web_url", ""),
                          f"★{it.get('star_count', 0)} — {_clean(it.get('description'))}",
                          "gitlab", meta={"stars": it.get("star_count", 0)}))
    if not out:
        raise EngineError("gitlab", "0 results")
    return out


# --------------------------------------------------------------------------- #
# Academic
# --------------------------------------------------------------------------- #
def search_arxiv(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    resp = net.get(
        "https://export.arxiv.org/api/query",
        params={"search_query": f"all:{query}", "max_results": min(50, limit),
                "sortBy": "relevance"},
        timeout=timeout or 40.0, engine="arxiv",
    )
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(resp.text, "xml")
    out: list[Result] = []
    for e in soup.find_all("entry")[:limit]:
        title = _clean(e.title.get_text()) if e.title else ""
        if not title:
            continue
        link = e.id.get_text() if e.id else ""
        summary = _clean(e.summary.get_text()) if e.summary else ""
        published = ""
        if e.published:
            published = e.published.get_text()[:10]
        authors = []
        for a in e.find_all("author")[:4]:
            n = a.find("name")
            if n:
                authors.append(n.get_text().strip())
        if authors:
            summary = f"[{', '.join(authors)}] " + summary
        out.append(Result(title, link, summary, "arxiv", published=published))
    if not out:
        raise EngineError("arxiv", "0 results")
    return out


def search_crossref(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://api.crossref.org/works",
        params={"query": query, "rows": min(50, limit),
                "select": "DOI,title,URL,author,issued,container-title,is-referenced-by-count"},
        headers={"User-Agent": "websearch-mcp/2.0 (mailto:websearch@example.invalid)"},
        timeout=timeout, engine="crossref",
    )
    out: list[Result] = []
    for it in data.get("message", {}).get("items", [])[:limit]:
        title = _clean((it.get("title") or [""])[0])
        if not title:
            continue
        doi = it.get("DOI", "")
        url = it.get("URL") or (f"https://doi.org/{doi}" if doi else "")
        authors = ", ".join(
            f"{a.get('family', '')}".strip() for a in (it.get("author") or [])[:3]
        )
        venue = _clean((it.get("container-title") or [""])[0])
        snippet = " · ".join(x for x in (authors, venue,
                                         f"cited by {it.get('is-referenced-by-count', 0)}") if x)
        published = ""
        parts = (it.get("issued", {}) or {}).get("date-parts", [[]])
        if parts and parts[0]:
            published = "-".join(str(p) for p in parts[0])
        out.append(Result(title, url, snippet, "crossref", published=published,
                          meta={"doi": doi}))
    if not out:
        raise EngineError("crossref", "0 results")
    return out


def search_openalex(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://api.openalex.org/works",
        params={"search": query, "per-page": min(50, limit),
                "mailto": "websearch@example.invalid"},
        timeout=timeout, engine="openalex",
    )
    out: list[Result] = []
    for it in data.get("results", [])[:limit]:
        title = _clean(it.get("display_name") or it.get("title"))
        if not title:
            continue
        doi = it.get("doi") or ""
        loc = it.get("primary_location") or {}
        url = doi or (loc.get("landing_page_url") or it.get("id", ""))
        if not url:
            continue
        authors = ", ".join(
            ((a.get("author") or {}).get("display_name") or "")
            for a in (it.get("authorships") or [])[:3]
        )
        snippet = " · ".join(x for x in (authors,
                                         str(it.get("publication_year") or ""),
                                         f"cited by {it.get('cited_by_count', 0)}") if x)
        out.append(Result(title, url, snippet, "openalex",
                          published=str(it.get("publication_date") or ""),
                          meta={"cited_by": it.get("cited_by_count", 0),
                                "open_access": bool((it.get("open_access") or {}).get("is_oa"))}))
    if not out:
        raise EngineError("openalex", "0 results")
    return out


def search_pubmed(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    common = {"db": "pubmed", "retmode": "json", "tool": "websearch-mcp",
              "email": "websearch@example.invalid"}
    ids_resp = net.get_json(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={**common, "term": query, "retmax": min(50, limit), "sort": "relevance"},
        timeout=timeout, engine="pubmed",
    )
    ids = ids_resp.get("esearchresult", {}).get("idlist", [])
    if not ids:
        raise EngineError("pubmed", "0 results")

    titles: dict[str, str] = {}
    dates: dict[str, str] = {}
    journals: dict[str, str] = {}
    try:
        summ = net.get_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={**common, "id": ",".join(ids)},
            timeout=timeout, engine="pubmed",
        )
        result = summ.get("result", {})
        for pmid in ids:
            item = result.get(pmid) or {}
            if item.get("title"):
                titles[pmid] = _plain(item["title"])
                dates[pmid] = (item.get("pubdate") or "")[:10]
                journals[pmid] = _clean(item.get("fulljournalname") or item.get("source"))
    except Exception:  # noqa: BLE001 - titles are a nice-to-have
        pass

    out: list[Result] = []
    for pmid in ids[:limit]:
        title = titles.get(pmid) or f"PubMed {pmid}"
        out.append(Result(title, f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                          journals.get(pmid, ""), "pubmed",
                          published=dates.get(pmid, ""), meta={"pmid": pmid}))
    return out


def search_openlibrary(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://openlibrary.org/search.json",
        params={"q": query, "limit": min(100, limit),
                "fields": "key,title,author_name,first_publish_year,edition_count"},
        timeout=timeout, engine="openlibrary",
    )
    out: list[Result] = []
    for it in data.get("docs", [])[:limit]:
        key = it.get("key", "")
        title = _clean(it.get("title", ""))
        if not title or not key:
            continue
        authors = ", ".join((it.get("author_name") or [])[:2])
        snippet = " · ".join(x for x in (
            f"by {authors}" if authors else "",
            str(it.get("first_publish_year") or ""),
            f"{it.get('edition_count', 0)} editions",
        ) if x)
        out.append(Result(title, "https://openlibrary.org" + key, snippet, "openlibrary",
                          published=str(it.get("first_publish_year") or "")))
    if not out:
        raise EngineError("openlibrary", "0 results")
    return out


# --------------------------------------------------------------------------- #
# Media / archives / community
# --------------------------------------------------------------------------- #
def search_openverse(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Openverse — CC-licensed images.

    Anonymous callers are capped at ``page_size=20``; asking for more returns
    401 (not a validation error), so the page size is clamped here.
    """
    data = net.get_json(
        "https://api.openverse.org/v1/images/",
        params={"q": query, "page_size": max(1, min(20, limit))},
        # A cold (uncached) Openverse query has been observed at ~55s; the
        # 20s default is far too short for this endpoint.
        timeout=timeout or 90.0, engine="openverse",
    )
    out: list[Result] = []
    for it in data.get("results", [])[:limit]:
        url = it.get("foreign_landing_url") or it.get("url", "")
        if not url:
            continue
        out.append(Result(
            _clean(it.get("title") or "Untitled image"), url,
            f"by {it.get('creator') or '?'} ({it.get('license') or '?'}) "
            f"[{it.get('provider') or ''}]",
            "openverse",
            meta={"license": it.get("license", ""), "creator": it.get("creator", ""),
                  "image_url": it.get("url", "")},
        ))
    if not out:
        raise EngineError("openverse", "0 results")
    return out


def search_archive(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Internet Archive advanced search.

    A bare ``q`` is a loose OR-ish full-text match that returns junk (a search
    for "python tutorial" ranks Oregon Trail first). Field-scoped ``title:(...)``
    is dramatically better, so try that first and fall back to the bare query
    only when the scoped search finds too little.
    """
    def _query(expr: str) -> list[dict]:
        data = net.get_json(
            "https://archive.org/advancedsearch.php",
            params={"q": expr,
                    "fl[]": ["identifier", "title", "creator", "mediatype", "year"],
                    "rows": min(50, max(limit, 5)), "output": "json",
                    "sort[]": "downloads desc"},
            timeout=timeout, engine="archive",
        )
        return data.get("response", {}).get("docs", [])

    docs = _query(f'title:({query})')
    if len(docs) < 3:
        seen = {d.get("identifier") for d in docs}
        docs += [d for d in _query(query) if d.get("identifier") not in seen]

    out: list[Result] = []
    for doc in docs[:limit]:
        ident = doc.get("identifier", "")
        title = doc.get("title")
        if isinstance(title, list):
            title = title[0] if title else ""
        title = _clean(title or ident)
        if not ident:
            continue
        creator = doc.get("creator")
        if isinstance(creator, list):
            creator = ", ".join(str(c) for c in creator[:2])
        snippet = " · ".join(x for x in (
            _clean(creator), doc.get("mediatype", ""), str(doc.get("year") or "")) if x)
        out.append(Result(title, f"https://archive.org/details/{ident}", snippet, "archive",
                          published=str(doc.get("year") or ""),
                          meta={"mediatype": doc.get("mediatype", "")}))
    if not out:
        raise EngineError("archive", "0 results")
    return out


def search_mwmbl(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    data = net.get_json(
        "https://api.mwmbl.org/api/v1/search/",
        params={"s": query},
        timeout=timeout, engine="mwmbl",
    )
    if isinstance(data, dict):
        data = data.get("results", [])
    out: list[Result] = []
    for item in data[:limit]:
        url = item.get("url", "")
        if not url:
            continue
        out.append(Result(_frag(item.get("title")), url, _frag(item.get("extract")), "mwmbl"))
    if not out:
        raise EngineError("mwmbl", "0 results")
    return out


MARGINALIA_HOSTS = (
    "https://old-search.marginalia.nu/search",
    "https://search.marginalia.nu/search",
)


def search_marginalia(query: str, limit: int, timeout: float | None = None) -> list[Result]:
    """Marginalia — indie web index.

    Two things were wrong with the previous version, both fixed here:

    1. **Wrong host.** ``marginalia-search.com`` (and ``search.marginalia.nu``)
       now serve the marketing/help page — "This is a keyword-based search
       engine" — for every query, so the parser found zero results while the
       page returned HTTP 200. ``old-search.marginalia.nu`` is the host that
       still renders actual results.
    2. **Wrong selector.** The old code looked for Tailwind ``bg-white p-4``
       classes from a redesign that no longer exists. The live markup is
       ``section.card.search-result`` with ``a.title`` and ``p.description``.

    The parameter is ``query=``, not ``q=``: ``q=`` returns the "Wait A Moment"
    anti-bot interstitial, and ``js=no-js`` triggers it too, so neither is sent.

    The JSON API (``api2.marginalia-search.com``) needs a personal API key and
    answers 429 with the public key from a datacenter IP, so it is not used.
    """
    from bs4 import BeautifulSoup

    def _fetch(host: str, extra: dict | None = None) -> BeautifulSoup:
        resp = net.get(host, params={"query": query, **(extra or {})},
                       timeout=timeout, engine="marginalia")
        return BeautifulSoup(resp.text, "lxml")

    def _gated(soup: BeautifulSoup) -> bool:
        head = soup.get_text(" ", strip=True)[:400].lower()
        return ("wait a moment" in head or "wait for a moment" in head
                or "barraged by queries" in head)

    def _parse(soup: BeautifulSoup) -> list[Result]:
        found: list[Result] = []
        for block in soup.select("section.card.search-result, section.search-result"):
            anchor = block.select_one("a.title") or (
                block.find(["h2", "h3"]) and
                block.find(["h2", "h3"]).find("a", href=True))
            if not anchor:
                continue
            url = anchor.get("href", "")
            title = _clean(anchor.get_text(" ", strip=True))
            if not title or not url.startswith("http"):
                continue
            if "/site/" in url or "web.archive.org" in url:
                continue
            p = block.select_one("p.description") or block.find("p")
            snippet = _clean(p.get_text(" ", strip=True)) if p else ""
            found.append(Result(title, url, snippet, "marginalia"))
            if len(found) >= limit:
                break
        return found

    last_soup: BeautifulSoup | None = None
    gated = False
    for host in MARGINALIA_HOSTS:
        soup = _fetch(host)
        last_soup = soup
        if _gated(soup):
            gated = True
            # Follow the one-shot ``sst`` token in the interstitial's proceed
            # link, then retry the same host once.
            token = None
            for a in soup.find_all("a", href=True):
                m = re.search(r"sst=(SE-[0-9a-f]+)", a["href"])
                if m:
                    token = m.group(1)
                    break
            if token:
                soup = _fetch(host, {"sst": token})
                last_soup = soup
                gated = _gated(soup)
            if gated:
                continue
        out = _parse(soup)
        if out:
            return out

    # No results from any host. Decide *why* before reporting, because the two
    # causes are indistinguishable from the result count alone:
    #
    # * Being rate-limited must be reported as such. The fallback host serves
    #   its marketing page ("This is a keyword-based search engine") for every
    #   query, so checking the help-page text first would silently convert a
    #   rate-limit into "0 results" — the exact bug this engine already had
    #   once. Check the gate first.
    if gated:
        # Rate-limited, not broken. A BlockedError puts the engine into the
        # circuit breaker's cooldown instead of pretending the parse failed.
        raise BlockedError("marginalia", "rate-limited (bot barricade)")
    page_text = (last_soup.get_text(" ", strip=True).lower()
                 if last_soup is not None else "")
    # "No search results found." is what a genuine zero-match page says (a
    # ~15 KB document). The marketing help page ("This is a keyword-based
    # search engine") is ~37 KB and is served by hosts that no longer render
    # results — treating that as an honest empty result would mask a dead
    # endpoint, which is precisely the bug this engine had.
    if "no search results found" in page_text:
        return []
    raise EngineError("marginalia", "0 results (markup may have changed)")


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
register(Engine("wikipedia", search_wikipedia, "api", groups=("reference", "web"), note="Wikipedia — MediaWiki API"))
register(Engine("wikinews", search_wikinews, "api", groups=("news", "reference"),
                probe_query="election", note="Wikinews articles"))
register(Engine("commons", search_commons, "api", groups=("media", "reference"),
                probe_query="cat", note="Wikimedia Commons files"))
register(Engine("wikidata", search_wikidata, "api", groups=("reference",),
                probe_query="Python", note="Wikidata entities"))
register(Engine("github", search_github, "api", groups=("code",), note="GitHub repositories"))
register(Engine("gitlab", search_gitlab, "api", groups=("code",), note="GitLab projects"))
register(Engine("stackexchange", search_stackexchange, "api", groups=("code",), note="Stack Overflow"))
register(Engine("npm", search_npm, "api", groups=("code",), note="npm registry"))
register(Engine("crates", search_crates, "api", groups=("code",), note="crates.io registry"))
register(Engine("hn", search_hn, "api", groups=("social", "news"), note="Hacker News (Algolia)"))
register(Engine("lemmy", search_lemmy, "api", groups=("social",), note="Lemmy federated posts"))
register(Engine("arxiv", search_arxiv, "api", groups=("academic",), fast=False, note="arXiv papers"))
register(Engine("crossref", search_crossref, "api", groups=("academic",), note="Crossref works"))
register(Engine("openalex", search_openalex, "api", groups=("academic",), note="OpenAlex works"))
register(Engine("pubmed", search_pubmed, "api", groups=("academic", "medical"), note="PubMed via NCBI E-utilities"))
register(Engine("openlibrary", search_openlibrary, "api", groups=("books", "reference"), note="Open Library books"))
register(Engine("openverse", search_openverse, "api", groups=("media",), fast=False, note="Openverse — CC-licensed images"))
register(Engine("archive", search_archive, "api", groups=("media", "reference"), note="Internet Archive items"))
register(Engine("mwmbl", search_mwmbl, "api", groups=("web", "indie"), note="Mwmbl open-source index"))
register(Engine("marginalia", search_marginalia, "html", groups=("web", "indie"), note="Marginalia — indie web index"))
