"""Page fetching: HTML -> text/markdown, JSON passthrough, PDF extraction."""
from __future__ import annotations

import copy
import io
import json
import re
import urllib.parse

from . import config, net
from .extract import extract_links, html_to_markdown, html_to_text
from .passages import extract_passages
from .safety import validate_url
from .types import HttpStatusError, UnsafeURLError

PDF_MAGIC = b"%PDF-"
TEXTUAL_TYPES = (
    "text/", "application/json", "application/xml", "application/xhtml",
    "application/rss+xml", "application/atom+xml", "application/ld+json",
    "application/javascript",
)
BINARY_TYPES = (
    "image/", "audio/", "video/", "font/", "application/zip",
    "application/gzip", "application/octet-stream", "application/pdf",
)


def _is_binary(ctype: str, body: bytes) -> bool:
    ctype = (ctype or "").split(";")[0].strip().lower()
    if ctype.startswith("application/pdf") or body[:5] == PDF_MAGIC:
        return True
    # ``application/octet-stream`` means "unknown" — servers use it for plain
    # text, CSV and JSON as often as for real binaries. It must be sniffed
    # *before* the generic prefix check, which used to swallow it and classify
    # every octet-stream response as binary (so mislabelled text files came
    # back with an empty body).
    if not ctype or ctype == "application/octet-stream":
        return b"\x00" in body[:4096]
    if ctype.startswith(BINARY_TYPES):
        return True
    return False


def _pdf_to_text(body: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return "[PDF content: pypdf is not installed, cannot extract text]"
    try:
        reader = PdfReader(io.BytesIO(body))
        pages = []
        for i, page in enumerate(reader.pages):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001
                pages.append(f"[page {i + 1}: {type(exc).__name__}]")
            # Only claim truncation when there really is another page. The old
            # check (``i >= 99``) appended the notice to a document with
            # exactly 100 pages, telling the caller content was dropped when
            # the whole thing had been extracted.
            if i >= 99 and i + 1 < len(reader.pages):
                pages.append("[... truncated after 100 pages]")
                break
        return "\n\n".join(p.strip() for p in pages if p.strip())
    except Exception as exc:  # noqa: BLE001
        return f"[PDF parse failed: {type(exc).__name__}: {exc}]"


def _html_to_markdown_fallback(html: str) -> tuple[str, str, dict]:
    """html2text fallback used only if the built-in renderer throws."""
    try:
        import html2text

        h = html2text.HTML2Text()
        h.body_width = 0
        h.ignore_images = False
        h.ignore_links = False
        title, _, meta = html_to_text(html)
        return title, h.handle(html).strip(), meta
    except Exception:
        title, text, meta = html_to_text(html)
        return title, text, meta


def fetch(
    url: str,
    *,
    max_chars: int | None = None,
    fmt: str = "markdown",
    include_links: bool = False,
    include_metadata: bool = True,
    query: str = "",
    max_passages: int = 12,
) -> dict:
    """Fetch ``url`` and return a JSON-serialisable dict describing the page.

    ``fmt`` is one of ``markdown`` (default), ``text`` or ``html``.

    When ``query`` is given, only the passages that answer it are returned
    (see :mod:`websearch.passages`). This is the difference between reading a
    54k-character page and reading the 2k that matter, so it is the preferred
    way to read a search result in an agent loop.
    """
    max_chars = config.DEFAULT_MAX_CHARS if max_chars is None else max(200, int(max_chars))
    fmt = (fmt or "markdown").lower()
    if fmt not in {"markdown", "text", "html"}:
        raise ValueError("fmt must be 'markdown', 'text' or 'html'")
    query = (query or "").strip()

    # ``max_chars`` belongs in the key: without it a small fetch (say 300
    # chars) poisons the cache and every later request for the same URL
    # silently returns the truncated copy. The query and passage count are
    # part of the key for the same reason.
    cache_key = (f"{fmt}|{int(include_links)}|{int(include_metadata)}|{max_chars}"
                 f"|{query}|{max_passages}|{url}")
    cached = net.FETCH_CACHE.get(cache_key)
    if cached is not None:
        out = copy.deepcopy(cached)
        out["cached"] = True
        return out

    validate_url(url, allow_private=config.ALLOW_PRIVATE_ADDRESSES)

    headers = {"Accept": "text/html,application/xhtml+xml,application/pdf,"
                         "application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5"}

    def _check(target: str) -> str:
        return validate_url(target, allow_private=config.ALLOW_PRIVATE_ADDRESSES)

    body, ctype, final_url = net.stream_bytes(
        url,
        max_bytes=config.MAX_DOWNLOAD_BYTES,
        headers=headers,
        timeout=config.FETCH_TIMEOUT,
        # Re-validate every redirect hop: a public page can otherwise 302 to
        # loopback or the cloud metadata endpoint and bypass the SSRF policy.
        validator=_check,
    )

    out: dict = {
        "url": url,
        "final_url": final_url,
        "content_type": ctype,
        "bytes": len(body),
        "fmt": fmt,
        "cached": False,
    }

    def _finish(payload: dict) -> dict:
        """Single exit point: apply passages, then cache.

        Every branch funnels through here. Previously only the HTML branch
        cached, so JSON, text, PDF and binary fetches hit the network again on
        every call even with the cache enabled.

        ``payload`` may carry a private ``_full_text`` key: the untruncated
        document, when the branch stored a ``max_chars``-sized prefix in
        ``text``. Passage selection must see the whole document — ranking only
        the prefix would miss the section that answers the question and would
        report the prefix's length as the document size.
        """
        full_text = payload.pop("_full_text", None)
        if query and payload.get("kind") != "html_raw":
            source = full_text or payload.get("text") or ""
            if source:
                picked = extract_passages(source, query,
                                          max_chars=max_chars,
                                          max_passages=max_passages)
                payload["text"] = picked["text"]
                payload["passages"] = picked["passages"]
                payload["passage_matched"] = picked["matched"]
                if picked["matched"]:
                    # total_chars describes the whole document, not what was
                    # kept — the caller uses it to judge what it is missing.
                    payload["total_chars"] = picked["total_chars"]
                    # Trust ``sliced`` alone. Comparing kept_chars to
                    # total_chars re-introduced the exact false positive that
                    # field exists to avoid: block splitting rejoins with a
                    # normalised separator, so a fully returned document with
                    # odd whitespace reports fewer characters than it started
                    # with and was flagged as truncated.
                    payload["truncated"] = bool(picked.get("sliced"))
        net.FETCH_CACHE.set(cache_key, copy.deepcopy(payload))
        return payload

    if _is_binary(ctype, body):
        if ctype.split(";")[0].strip().lower() == "application/pdf" or body[:5] == PDF_MAGIC:
            if len(body) > config.MAX_PDF_BYTES:
                out["error"] = f"PDF exceeds {config.MAX_PDF_BYTES} bytes"
                out["text"] = ""
                return _finish(out)
            text = _pdf_to_text(body)
            out.update({"title": "", "text": text[:max_chars],
                        "truncated": len(text) > max_chars, "kind": "pdf",
                        "total_chars": len(text), "_full_text": text})
            return _finish(out)
        out.update({
            "kind": "binary",
            "text": "",
            "note": f"binary content ({ctype or 'unknown type'}) is not converted to text",
        })
        return _finish(out)

    text_body = net.decode_body(body, ctype)
    ctype_main = ctype.split(";")[0].strip().lower()

    if ctype_main in {"application/json", "application/ld+json"} or (
        ctype_main.startswith("application/") and text_body.lstrip()[:1] in "[{"
    ):
        try:
            pretty = json.dumps(json.loads(text_body), indent=2, ensure_ascii=False)
        except ValueError:
            pretty = text_body
        out.update({"kind": "json", "title": "", "text": pretty[:max_chars],
                    "truncated": len(pretty) > max_chars,
                    "total_chars": len(pretty), "_full_text": pretty})
        if include_links:
            out["links"] = []
        return _finish(out)

    if ctype_main.startswith(("text/plain", "text/csv", "text/markdown")) or (
        ctype_main.startswith("text/") and "html" not in ctype_main
    ):
        out.update({"kind": "text", "title": "", "text": text_body[:max_chars],
                    "truncated": len(text_body) > max_chars,
                    "total_chars": len(text_body), "_full_text": text_body})
        return _finish(out)

    # HTML (or a mislabelled document that parses as HTML).
    if fmt == "html":
        # Raw HTML is for parser debugging; passages would corrupt it, so it
        # is marked so _finish leaves it alone.
        out.update({"kind": "html_raw", "title": "",
                    "text": text_body[:max_chars],
                    "truncated": len(text_body) > max_chars,
                    "total_chars": len(text_body)})
        if include_links:
            out["links"] = extract_links(text_body, final_url)
        return _finish(out)

    try:
        if fmt == "markdown":
            title, content, meta = html_to_markdown(text_body)
        else:
            title, content, meta = html_to_text(text_body)
    except Exception:  # noqa: BLE001 - malformed markup should not kill the tool
        title, content, meta = _html_to_markdown_fallback(text_body)

    out.update({
        "kind": "html",
        "title": title,
        # Pass the *whole* extracted document to passage selection; truncating
        # first would only ever search the first `max_chars` characters and
        # silently miss the part of the page that answers the question.
        "text": content if query else content[:max_chars],
        "truncated": len(content) > max_chars,
        "total_chars": len(content),
    })
    if include_metadata and meta:
        out["meta"] = meta
    if include_links:
        links = extract_links(text_body, final_url)
        out["links"] = links[:200]
        out["link_count"] = len(links)

    return _finish(out)


def fetch_many(urls: list[str], *, max_chars: int = 6000, fmt: str = "markdown",
               concurrency: int = 4, query: str = "", max_passages: int = 12,
               max_urls: int = 12) -> list[dict]:
    """Fetch several URLs concurrently. Per-URL failures become error entries.

    ``max_urls`` bounds the fan-out and is applied by slicing, so a caller that
    passes more URLs than that gets fewer results than it asked for. The bound
    is explicit and documented for that reason: it used to be a hardcoded
    ``urls[:12]`` that silently dropped the rest. Callers that need to report
    what was skipped should slice first and compare (as ``multi_fetch`` does).
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(u: str) -> dict:
        try:
            return fetch(u, max_chars=max_chars, fmt=fmt, query=query,
                         max_passages=max_passages)
        except Exception as exc:  # noqa: BLE001
            return {"url": u, "error": f"{type(exc).__name__}: {exc}"}

    urls = urls[: max(1, max_urls)]
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(concurrency, len(urls))) as pool:
        return list(pool.map(one, urls))
