"""Readable-content extraction from HTML.

Two output modes:

``text``
    Plain text with paragraph breaks — cheap, token-friendly, no markup.
``markdown``
    Markdown with headings, lists, links and fenced code blocks preserved.

The container selection uses a small readability-style density heuristic: score
candidate blocks by text length penalised by link density and boilerplate class
names, then pick the best one. That beats "take ``<main>`` or ``<body>``" on
sites that stuff navigation inside the body (docs sites, Wikipedia, news).
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, Doctype, Tag

# Elements that never carry article prose.
STRIP_TAGS = (
    "script", "style", "noscript", "svg", "canvas", "template", "iframe",
    "form", "button", "input", "select", "textarea", "nav", "header", "footer",
    "aside", "menu", "dialog",
)

# Class/id fragments that indicate chrome rather than content.
BOILERPLATE_HINTS = (
    "nav", "navbar", "navigation", "nav-menu", "site-nav", "main-nav",
    "menu", "sidebar", "side-bar", "breadcrumb", "breadcrumbs", "footer",
    "site-footer", "header", "site-header", "masthead", "cookie", "cookies",
    "consent", "gdpr", "banner", "cookie-banner", "advert", "advertisement",
    "ads", "ads-", "ad-slot", "sponsor", "sponsored", "promo", "newsletter",
    "subscribe", "social", "share", "related", "recommend", "recommended",
    "comment", "comments", "disqus", "popup", "modal", "overlay", "skip-link",
    "toolbar", "topbar", "pagination", "pager", "toc", "table-of-contents",
)

CONTENT_HINTS = (
    "article", "content", "post", "entry", "main", "body", "story", "prose",
    "markdown", "documentation", "doc-content", "wiki", "text",
)


def _hint_match(hay: str, hints: tuple[str, ...]) -> bool:
    """True if a class/id token matches one of ``hints``.

    Matching is per hyphen-delimited segment, not by substring: a plain ``in``
    test made ``class="protocol"`` match the ``toc`` hint and
    ``class="commentary"`` match ``comment``, which silently deleted real
    article bodies. A hint ending in ``-`` (``ads-``) matches as a prefix.
    """
    if not hay:
        return False
    segments = [s for s in re.split(r"[^a-z0-9]+", hay) if s]
    for hint in hints:
        if hint.endswith("-"):
            stem = hint[:-1]
            if any(s == stem or s.startswith(stem) for s in segments):
                return True
        elif hint in segments:
            return True
    return False

WHITESPACE_RE = re.compile(r"[ \t ]+")
BLANKLINES_RE = re.compile(r"\n{3,}")


def _classes(el: Tag) -> str:
    cls = el.get("class") or []
    if isinstance(cls, str):
        cls = [cls]
    return " ".join(cls + [el.get("id") or ""]).lower()


# Hints must match a whole hyphen-delimited segment of the class/id string,
# not a substring: a plain ``in`` test made class="protocol" match the "toc"
# hint and class="commentary" match "comment", silently deleting real bodies.
def _is_boilerplate(el: Tag) -> bool:
    return _hint_match(_classes(el), BOILERPLATE_HINTS)


def _text_len(el: Tag) -> int:
    return len(el.get_text(" ", strip=True))


def _link_density(el: Tag) -> float:
    total = _text_len(el)
    if total == 0:
        return 1.0
    link_text = sum(_text_len(a) for a in el.find_all("a"))
    return min(1.0, link_text / total)


def _score(el: Tag) -> float:
    """Higher is more article-like."""
    length = _text_len(el)
    if length < 120:
        return 0.0
    density = _link_density(el)
    score = length * (1.0 - density) ** 2
    hay = _classes(el)
    if any(h in hay for h in CONTENT_HINTS):
        score *= 1.35
    if _is_boilerplate(el):
        score *= 0.15
    # Paragraph-rich blocks are almost always the real article.
    score *= 1.0 + 0.1 * min(len(el.find_all("p")), 12)
    return score


def _strip(soup: BeautifulSoup) -> None:
    # <head> holds title/meta only; leaving it in inflates container scores and
    # leaks document text into the output.
    for tag in soup(["head"] + list(STRIP_TAGS)):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, (Comment, Doctype))):
        comment.extract()
    for el in soup.find_all(True):
        if el.attrs and _is_boilerplate(el):
            # Keep it only if it is clearly the content container itself.
            # The check must use the same segment matching as _is_boilerplate:
            # a plain substring test let class="poster-ads" (or "sponsored-posts")
            # escape deletion because it contains "post", so an ad block
            # survived into the extracted text.
            if not _hint_match(_classes(el), ("article", "content", "post", "entry")):
                el.decompose()
    for tag in soup.find_all(["img", "picture", "source", "link", "meta", "br", "hr"]):
        if tag.name in ("link", "meta"):
            tag.decompose()
    for tag in soup.find_all(True):
        keep = {"href", "src", "alt", "colspan", "rowspan", "lang"}
        if tag.name in ("code", "pre"):
            # The class carries the fenced-code language hint (language-python).
            keep.add("class")
        for attr in list(tag.attrs):
            if attr not in keep:
                del tag.attrs[attr]


def pick_container(soup: BeautifulSoup) -> Tag:
    """Return the most article-like subtree.

    The scoring below is length-based, so it only discriminates on pages with
    real prose. On a short page — a status note, a changelog entry, a
    one-paragraph answer — every candidate scores 0.0, and "first candidate
    with the best score" then picked whichever element happened to come first
    in document order. That was usually the ``<h1>``, so the entire body was
    silently dropped and the caller received the heading alone.

    Two guards make that impossible:

    * A candidate that contains no body text at all is not a container. (An
      element is only skipped when it has no text *and* no scored descendants,
      so a legitimately empty page still returns something.)
    * Ties are broken by *coverage*, not document order: the element holding
      more of the page's text wins, so ``<body>`` beats its own ``<h1>``.
    """
    candidates: list[Tag] = []
    for sel in ("article", "main", "[role=main]", "div.post", "div.entry-content",
                "div.article-body", "div.markdown-body", "div#content", "div.content"):
        candidates.extend(soup.select(sel))
    if soup.body is not None:
        candidates.append(soup.body)
    candidates.append(soup)

    scored = [(el, _score(el)) for el in candidates]
    scored = [(el, s) for el, s in scored if s > 0 or _text_len(el) > 0]
    if scored:
        best_score = max(s for _, s in scored)
        # Among everything tied for the best score, keep the one that covers
        # the most text: a wrapper always contains at least as much as the
        # child that scored the same, and the wrapper is the honest answer.
        tied = [el for el, s in scored if s == best_score]
        return max(tied, key=_text_len)

    # Nothing scored and nothing has text: fall back to the densest direct
    # child of <body>, then to the document itself.
    if soup.body is not None:
        children = [c for c in soup.body.find_all(recursive=False) if isinstance(c, Tag)]
        if children:
            return max(children, key=_text_len)
    return soup


def _normalize_text(text: str) -> str:
    text = WHITESPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def _normalize_markdown(text: str) -> str:
    """Like :func:`_normalize_text` but keeps leading indentation.

    Markdown encodes list nesting and code indentation in leading whitespace,
    so stripping every line (as the text normaliser does) flattened nested
    lists into sibling items.
    """
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def html_to_text(html: str) -> tuple[str, str, dict]:
    """Extract ``(title, text, meta)`` from an HTML document."""
    soup = BeautifulSoup(html, "lxml")
    title = ""
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(" ", strip=True)
    meta: dict = {}
    for name in ("description", "og:description", "author"):
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            meta[name] = tag["content"][:500]
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = og["content"]

    _strip(soup)
    container = pick_container(soup)
    text = container.get_text("\n", strip=True)
    return title, _normalize_text(text), meta


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "br", "li", "tr", "blockquote",
    "pre", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "dl", "dt",
    "dd", "figure", "figcaption", "hr",
}


def _inline_nodes(nodes) -> str:
    """Render a run of sibling nodes as inline markdown.

    Taking the whole run (rather than one node at a time) is what lets the
    wrapper's own markup survive: ``<a href=..>text</a>`` only becomes
    ``[text](..)`` when the anchor itself is rendered, not when its text child
    is.
    """
    parts: list[str] = []
    for child in nodes:
        if isinstance(child, Comment):
            continue
        if isinstance(child, str):
            parts.append(WHITESPACE_RE.sub(" ", child))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        inner = _inline_nodes(child.children)
        if name in ("strong", "b"):
            parts.append(f"**{inner.strip()}**" if inner.strip() else "")
        elif name in ("em", "i"):
            parts.append(f"*{inner.strip()}*" if inner.strip() else "")
        elif name == "code":
            parts.append(f"`{inner.strip()}`" if inner.strip() else "")
        elif name == "a" and child.get("href"):
            href = child["href"]
            label = inner.strip() or href
            parts.append(f"[{label}]({href})")
        elif name == "img":
            alt = (child.get("alt") or "").strip()
            src = child.get("src") or ""
            if src:
                parts.append(f"![{alt}]({src})")
        elif name == "br":
            parts.append("\n")
        elif name in _BLOCK_TAGS:
            # A block nested inside an inline run: keep its text but do not
            # let it swallow the surrounding inline markup.
            parts.append(inner)
        else:
            parts.append(inner)
    return "".join(parts)


def _inline(node: Tag) -> str:
    """Render the children of ``node`` as inline markdown."""
    return _inline_nodes(node.children)


def _has_block_children(el: Tag) -> bool:
    return any(isinstance(c, Tag) and c.name in _BLOCK_TAGS for c in el.children)


def _render_block(el: Tag, depth: int = 0) -> str:
    name = el.name
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        text = _normalize_text(_inline(el))
        return f"{'#' * level} {text}" if text else ""
    if name == "pre":
        code = el.get_text()
        lang = ""
        code_el = el.find("code")
        if code_el:
            cls = " ".join(code_el.get("class") or [])
            m = re.search(r"language-([\w+#-]+)", cls)
            if m:
                lang = m.group(1)
        code = code.strip()
        # A fence must be longer than any run of backticks inside the code, or
        # a snippet that itself contains ``` closes the block early and the
        # rest of the page is rendered as prose.
        longest = max((len(m) for m in re.findall(r"`+", code)), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}{lang}\n{code}\n{fence}"
    if name in ("ul", "ol"):
        lines = []
        for i, li in enumerate(el.find_all("li", recursive=False), 1):
            marker = f"{i}." if name == "ol" else "-"
            # A list item with its own blocks (nested list, paragraph, code)
            # must be rendered block-wise, otherwise a nested <ul> collapses
            # into the parent item's text ("outerinner").
            if _has_block_children(li):
                # Collapse paragraph breaks inside one item so the nested
                # block stays part of the same list item (a blank line would
                # start a new, loose list in most renderers).
                body = re.sub(r"\n{2,}", "\n", _render_children(li, depth + 1))
            else:
                body = _normalize_text(_inline(li))
            body = body.replace("\n", "\n  ")
            lines.append(f"{'  ' * depth}{marker} {body.strip()}")
        return "\n".join(lines)
    if name == "blockquote":
        inner = _render_children(el, depth)
        return "\n".join("> " + ln for ln in inner.splitlines() if ln.strip())
    if name == "table":
        return _render_table(el)
    if name == "hr":
        return "---"
    if name == "li":
        if _has_block_children(el):
            return _render_children(el, depth)
        return _normalize_text(_inline(el))
    return _render_children(el, depth)


def _render_children(el: Tag, depth: int = 0) -> str:
    out: list[str] = []
    buf: list = []  # pending inline nodes, rendered together when flushed

    def flush():
        if buf:
            chunk = _normalize_text(_inline_nodes(buf))
            if chunk:
                out.append(chunk)
            buf.clear()

    for child in el.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, Tag) and child.name in _BLOCK_TAGS:
            flush()
            rendered = _render_block(child, depth)
            if rendered.strip():
                out.append(rendered)
        else:
            buf.append(child)
    flush()
    return "\n\n".join(out)


def _render_table(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [(_normalize_text(_inline(td)) or " ").replace("|", "\\|")
                 for td in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |",
             "| " + " | ".join("---" for _ in range(width)) + " |"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _flat_text(container: Tag) -> str:
    """Depth-independent fallback: text with block boundaries as newlines.

    Used when the recursive renderer blows the stack on pathologically nested
    markup (thousands of nested <div>s). Losing the markdown structure beats
    returning nothing.
    """
    parts: list[str] = []
    for el in container.descendants:
        if isinstance(el, str):
            if el.strip():
                parts.append(WHITESPACE_RE.sub(" ", el))
        elif isinstance(el, Tag) and el.name in _BLOCK_TAGS:
            parts.append("\n")
    return _normalize_text(" ".join(parts))


def html_to_markdown(html: str) -> tuple[str, str, dict]:
    """Extract ``(title, markdown, meta)`` from an HTML document."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta: dict = {}
    for name in ("description", "og:description", "author"):
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            meta[name] = tag["content"][:500]

    _strip(soup)
    container = pick_container(soup)
    try:
        markdown = _normalize_markdown(_render_children(container))
    except RecursionError:
        # A page nested thousands of levels deep overflows the recursive
        # renderer. Degrade to flat text rather than returning an empty page.
        markdown = _flat_text(container)
    return title, markdown, meta


def extract_links(html: str, base_url: str = "") -> list[dict]:
    """Return ``[{text, url}]`` for every absolute-izable anchor."""
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    seen: set[str] = set()
    out: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        url = urljoin(base_url, href) if base_url else href
        if url in seen:
            continue
        seen.add(url)
        out.append({"text": _normalize_text(a.get_text(" ", strip=True))[:200], "url": url})
    return out
