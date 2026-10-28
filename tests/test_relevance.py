"""Offline tests for the relevance filter, passage extraction and news engines.

These never touch the network. The live behaviour of the engines they cover was
verified separately against the real endpoints; what is pinned here is the
parsing and selection logic, which is where regressions hide.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from websearch.passages import extract_passages
from websearch.relevance import coverage, is_relevant, query_terms


# --------------------------------------------------------------------------- #
# relevance.query_terms
# --------------------------------------------------------------------------- #
def test_query_terms_drops_stopwords_and_operators():
    terms = query_terms("how to use site:docs.python.org the asyncio event loop")
    assert "asyncio" in terms
    assert "event" in terms
    assert "loop" in terms
    assert "site" not in terms
    assert "docs.python.org" not in terms
    # "how", "to", "use", "the" are all stopwords.
    assert not {"how", "to", "use", "the"} & terms


def test_query_terms_treats_negation_as_a_positive_term():
    # ``-word`` is engine syntax for exclusion; the word itself is still what
    # the query is about, so a result mentioning it must not be discarded.
    assert "django" in query_terms("python web framework -django")


def test_query_terms_keeps_technical_tokens():
    terms = query_terms("c++ .net rust-lang c#")
    assert "c++" in terms
    assert ".net" in terms or "net" in terms
    assert "rust-lang" in terms or "rust" in terms


def test_query_terms_empty():
    assert query_terms("") == set()
    assert query_terms("   ") == set()


# --------------------------------------------------------------------------- #
# relevance.coverage / is_relevant
# --------------------------------------------------------------------------- #
def test_coverage_full_and_partial():
    assert coverage("python asyncio", "Python asyncio tutorial") == pytest.approx(1.0)
    assert 0.0 < coverage("python asyncio tutorial", "Python asyncio guide") < 1.0


def test_coverage_is_one_when_the_query_has_no_terms():
    # An all-stopword query must not filter everything out.
    assert coverage("the of and", "anything at all") == pytest.approx(1.0)


def test_coverage_matches_stems():
    # "containers" should match "container" and vice versa.
    assert coverage("kubernetes containers", "Kubernetes container runtime") > 0.0


@pytest.mark.parametrize(
    "query,title,snippet,url,relevant",
    [
        # A SERP for a completely different query must be dropped. These are
        # real pairs observed from Bing/Yahoo answering with junk.
        ("python asyncio tutorial", "WhatsApp Web", "Log in to WhatsApp", "https://web.whatsapp.com/", False),
        ("python asyncio tutorial", "Scratch - Imagine, Program, Share", "Create stories", "https://scratch.mit.edu/", False),
        ("kubernetes ingress", "Trezor Hardware Wallet", "Buy a Trezor", "https://trezor.io/", False),
        # Genuine hits must survive, including ones that do not repeat every
        # query word in the title.
        ("python asyncio tutorial", "Coroutines and Tasks", "asyncio documentation", "https://docs.python.org/3/library/asyncio-task.html", True),
        ("python asyncio tutorial", "A Practical Guide to Async Python", "Learn asyncio", "https://realpython.com/async-io-python/", True),
        ("kubernetes ingress", "Ingress", "Kubernetes documentation", "https://kubernetes.io/docs/concepts/services-networking/ingress/", True),
    ],
)
def test_is_relevant(query, title, snippet, url, relevant):
    assert is_relevant(query, title, snippet, url) is relevant


def test_is_relevant_checks_the_url_too():
    # The answer is often only in the URL (a slug), not the display text.
    assert is_relevant("asyncio taskgroup", "", "", "https://example.com/asyncio-taskgroup")


# --------------------------------------------------------------------------- #
# passages.extract_passages
# --------------------------------------------------------------------------- #
_DOC = """# Introduction

This page is about installing the package. It has nothing to do with the
question below.

## Configuration

The configuration file lives in /etc/example.conf and accepts three keys.

## Retry behaviour

Requests retry three times with exponential backoff. The first retry waits
one second, the second two seconds, the third four seconds.

```python
client = Client(retries=3)
```

## Unrelated appendix

Colophon and licence information. Thanks to all contributors.
"""


def test_passages_keeps_the_matching_section_and_drops_the_rest():
    picked = extract_passages(_DOC, "retry backoff", max_chars=2000)
    assert picked["matched"] is True
    assert "exponential backoff" in picked["text"]
    assert "Colophon" not in picked["text"]
    assert picked["kept_chars"] < picked["total_chars"]


def test_passages_keep_the_heading_with_its_section():
    picked = extract_passages(_DOC, "exponential backoff", max_chars=2000)
    assert "Retry behaviour" in picked["text"]


def test_passages_pull_in_an_adjacent_code_block():
    # The code sample after the matching paragraph is part of the answer even
    # though it repeats none of the query terms.
    picked = extract_passages(_DOC, "exponential backoff", max_chars=2000)
    assert "retries=3" in picked["text"]


def test_passages_preserve_document_order():
    picked = extract_passages(_DOC, "configuration retry", max_chars=4000)
    assert picked["text"].index("Configuration") < picked["text"].index("Retry behaviour")


def test_passages_fall_back_to_the_head_when_nothing_matches():
    picked = extract_passages(_DOC, "quantum chromodynamics", max_chars=200)
    assert picked["matched"] is False
    assert len(picked["text"]) <= 200
    assert picked["text"].startswith("# Introduction")


def test_passages_respect_the_character_budget():
    picked = extract_passages(_DOC, "retry backoff configuration", max_chars=150)
    assert len(picked["text"]) <= 150


def test_passages_handle_empty_text():
    picked = extract_passages("", "anything")
    assert picked == {"text": "", "passages": 0, "matched": False,
                      "total_chars": 0, "kept_chars": 0}


def test_passages_report_the_full_document_size():
    # total_chars must describe the whole document, not what was kept: the
    # caller uses it to judge how much it is missing.
    picked = extract_passages(_DOC, "retry backoff", max_chars=200)
    assert picked["total_chars"] == len(_DOC)


def test_passages_not_sliced_when_whitespace_is_normalised():
    """Re-joining blocks shrinks the text; that is not truncation.

    Blocks are split on ``\\n\\s*\\n`` and rejoined with ``\\n\\n``, so odd
    separators shorten the output even when every block is kept. Inferring
    ``truncated`` from kept_chars < total_chars therefore reported truncation
    on complete documents.
    """
    text = "alpha beta\n \ngamma delta\n\n\nepsilon zeta"
    picked = extract_passages(text, "alpha gamma epsilon", max_chars=5000)
    assert picked["passages"] == 3          # every block kept
    assert picked["kept_chars"] < picked["total_chars"]
    assert picked["sliced"] is False


def test_passages_flag_truncation_when_blocks_are_dropped():
    # A budget too small to hold every matching block: the caller must be told.
    picked = extract_passages(_DOC, "retry backoff", max_chars=150)
    assert picked["sliced"] is True


def test_passages_not_sliced_when_only_irrelevant_blocks_are_skipped():
    """Dropping off-topic sections is the feature, not truncation."""
    picked = extract_passages(_DOC, "retry backoff", max_chars=2000)
    assert picked["passages"] >= 1
    assert picked["sliced"] is False


def test_oversized_block_does_not_starve_other_passages():
    """One huge top-ranked block must not consume the whole budget."""
    big = ("alpha beta gamma delta epsilon zeta eta theta ") + "x" * 6000
    rest = "alpha answer is here\n\nbeta second paragraph"
    picked = extract_passages(big + "\n\n" + rest, "alpha beta gamma delta",
                              max_chars=500, max_passages=12)
    assert len(picked["text"]) <= 500
    assert "answer is here" in picked["text"], "smaller matching passage was starved"
    assert picked["sliced"] is True


def test_heading_is_kept_even_at_max_passages_one():
    """The documented "a heading is always kept with its section" contract."""
    picked = extract_passages("## needle title\n\nthe needle body is here",
                              "needle", max_passages=1)
    assert "## needle title" in picked["text"]
    assert "the needle body is here" in picked["text"]


def test_passages_handle_empty_text():
    picked = extract_passages("", "anything")
    assert picked["text"] == ""
    assert picked["passages"] == 0
    assert picked["matched"] is False


# --------------------------------------------------------------------------- #
# fetch — binary sniffing and the document-size contract
# --------------------------------------------------------------------------- #
def test_octet_stream_is_sniffed_not_assumed_binary():
    """``application/octet-stream`` is a shrug, not a binary declaration.

    It used to be in BINARY_TYPES, so the NUL-byte sniff beneath it was dead
    code and every mislabelled text file came back with an empty body.
    """
    from websearch.fetch import _is_binary

    assert _is_binary("application/octet-stream", b"hello world, plain text") is False
    assert _is_binary("application/octet-stream", b"ab\x00cd") is True
    # Real binaries are still binary.
    assert _is_binary("image/png", b"\x89PNG\r\n") is True
    assert _is_binary("application/pdf", b"%PDF-1.7") is True


def test_text_fetch_with_query_reports_the_full_document_size(monkeypatch):
    """total_chars must not be the length of the pre-truncated prefix."""
    import importlib

    net = importlib.import_module("websearch.net")
    fetch_mod = importlib.import_module("websearch.fetch")

    blocks = [f"auth paragraph {i} with token" for i in range(50)]
    blocks += [f"unrelated filler {i}" for i in range(50)]
    body = "\n\n".join(blocks).encode()

    monkeypatch.setattr(net, "stream_bytes", lambda url, **k: (body, "text/plain", url))
    monkeypatch.setattr(fetch_mod, "validate_url", lambda url, **k: url)
    net.FETCH_CACHE.clear()

    out = fetch_mod.fetch("https://ex.example/big.txt", max_chars=600, query="auth")
    assert out["passage_matched"] is True
    assert out["total_chars"] == len(body), "reported the truncated prefix as the document size"
    assert len(out["text"]) <= 600


# --------------------------------------------------------------------------- #
# news engines — parsing only, no network
# --------------------------------------------------------------------------- #
def _rss(*items: str) -> str:
    return ("<?xml version='1.0'?><rss version='2.0'><channel>"
            + "".join(items) + "</channel></rss>")


def test_bing_news_decodes_apiclick_links(monkeypatch):
    from websearch.engines import news

    feed = _rss(
        "<item><title>Real story</title>"
        "<link>https://www.bing.com/news/apiclick.aspx?ref=FexRss"
        "&amp;url=https%3a%2f%2fexample.com%2fstory&amp;c=1</link>"
        "<description>An example.</description>"
        "<pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item>"
    )
    monkeypatch.setattr(news, "_rss_items", lambda *a, **k: _parse_items(feed))
    out = news.search_bing_news("example", 5)
    assert out[0].url == "https://example.com/story"
    assert out[0].title == "Real story"
    assert out[0].published.startswith("Mon, 26 Oct 2026")
    # The tracking URL is preserved for callers that want it, but must not be
    # the primary link.
    assert out[0].meta["rss_link"].startswith("https://www.bing.com/news/apiclick")


def test_gnews_prefers_the_publisher_over_the_opaque_wrapper(monkeypatch):
    from websearch.engines import news

    feed = _rss(
        "<item><title>A headline - BBC News</title>"
        "<link>https://news.google.com/rss/articles/CBMiOPAQUE?oc=5</link>"
        "<description>Summary.</description>"
        "<source url='https://www.bbc.com'>BBC News</source>"
        "<pubDate>Tue, 27 Oct 2026 11:00:00 GMT</pubDate></item>"
    )
    monkeypatch.setattr(news, "_rss_items", lambda *a, **k: _parse_items(feed))
    out = news.search_gnews("headline", 5)
    # The wrapper is a JS shell that fetch_url cannot read, so the publisher
    # URL is what a caller can actually open.
    assert out[0].url == "https://www.bbc.com"
    assert out[0].meta["google_news_url"].startswith("https://news.google.com/rss/articles/")
    assert out[0].meta["publisher"] == "BBC News"


def test_gnews_snippet_is_plain_text(monkeypatch):
    """RSS descriptions are escaped HTML; callers must not receive markup."""
    from websearch.engines import news

    feed = _rss(
        "<item><title>A headline - BBC News</title>"
        "<link>https://news.google.com/rss/articles/OPAQUE?oc=5</link>"
        "<description>&lt;a href=\"https://news.google.com/rss/articles/OPAQUE\"&gt;"
        "A headline&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color=\"#6f6f6f\"&gt;"
        "BBC News&lt;/font&gt;</description>"
        "<source url='https://www.bbc.com'>BBC News</source></item>"
    )
    monkeypatch.setattr(news, "_rss_items", lambda *a, **k: _parse_items(feed))
    out = news.search_gnews("headline", 5)
    assert "<a" not in out[0].snippet
    assert "<font" not in out[0].snippet
    assert "A headline" in out[0].snippet


def test_news_engine_raises_on_an_empty_feed(monkeypatch):
    from websearch.engines import news
    from websearch.types import EngineError

    monkeypatch.setattr(news, "_rss_items", lambda *a, **k: [])
    with pytest.raises(EngineError):
        news.search_bing_news("nothing", 5)


def _parse_items(xml: str) -> list:
    from bs4 import BeautifulSoup

    return BeautifulSoup(xml, "xml").find_all("item")


def test_single_block_page_reports_matching_and_truncation():
    """The "nothing to rank" fast path must still answer the two questions.

    It returned ``matched=False, sliced=False`` unconditionally, so a
    one-paragraph page that plainly answered the query reported
    ``passage_matched=false``, and a page cut at ``max_chars`` reported
    ``truncated=false``.
    """
    one = "This domain is for use in documentation examples without permission."
    assert extract_passages(one, "documentation examples", max_chars=500)["matched"] is True
    assert extract_passages(one, "quantum chromodynamics", max_chars=500)["matched"] is False
    cut = extract_passages(one, "documentation", max_chars=20)
    assert cut["sliced"] is True
    assert len(cut["text"]) == 20
