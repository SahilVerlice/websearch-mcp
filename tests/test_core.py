"""Offline unit tests: URL canonicalisation, fusion, routing, extraction, safety.

These never touch the network, so they are safe to run anywhere::

    python3 -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from websearch.aggregate import _cap_per_domain, _fuse, _round_robin, canonical_url, resolve_engines
from websearch.extract import extract_links, html_to_markdown, html_to_text
from websearch.safety import validate_url
from websearch.types import Result, UnsafeURLError


# --------------------------------------------------------------------------- #
# canonical_url — the old host+path key collapsed distinct pages
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "a,b,distinct",
    [
        ("https://youtube.com/watch?v=AAA", "https://youtube.com/watch?v=BBB", True),
        ("https://example.com/a", "https://example.com/a/", False),
        ("https://www.example.com/a", "http://example.com/a", False),
        ("https://example.com/a?utm_source=x&id=7", "https://example.com/a?id=7", False),
        ("https://example.com/a?id=7&q=2", "https://example.com/a?q=2&id=7", False),
        ("https://example.com/A", "https://example.com/a", True),
        ("https://example.com:443/a", "https://example.com/a", False),
    ],
)
def test_canonical_url(a, b, distinct):
    assert (canonical_url(a) != canonical_url(b)) is distinct


def test_canonical_url_handles_garbage():
    assert canonical_url("") == ""
    assert canonical_url("not a url") == "not a url"


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #
def _r(title, url, snippet="", source="x"):
    return Result(title, url, snippet, source)


def test_fusion_prefers_agreement():
    a = [_r("Only Bing", "https://only-bing.example/", "x" * 100, "bing"),
         _r("Shared", "https://shared.example/page", "short", "bing")]
    b = [_r("Shared", "https://shared.example/page", "a much longer snippet here", "ddg"),
         _r("Only DDG", "https://only-ddg.example/", "y" * 100, "ddg")]
    fused = _fuse({"bing": a, "duckduckgo": b}, "rrf")
    top = fused[0]
    assert top.url == "https://shared.example/page"
    assert top.meta["agreement"] == 2
    assert set(top.meta["also_in"]) == {"bing"}


def test_fusion_dedupes_tracking_params():
    a = [_r("A", "https://example.com/x?utm_source=bing", "", "bing")]
    b = [_r("A", "https://example.com/x", "", "duckduckgo")]
    fused = _fuse({"bing": a, "duckduckgo": b}, "rrf")
    assert len(fused) == 1


def test_fusion_counts_independence_by_index_family():
    # bing/yahoo/duckduckgo share one index: three sources, ONE independent
    # opinion. Brave is a separate index.
    results = {
        "bing": [_r("Shared", "https://shared.example/p", "snippet", "bing")],
        "yahoo": [_r("Shared", "https://shared.example/p", "snippet", "yahoo")],
        "duckduckgo": [_r("Shared", "https://shared.example/p", "snippet", "duckduckgo")],
    }
    fused = _fuse(results, "rrf")
    assert fused[0].meta["agreement"] == 3
    assert fused[0].meta["independent"] == 1

    results["brave"] = [_r("Shared", "https://shared.example/p", "snippet", "brave")]
    fused = _fuse(results, "rrf")
    assert fused[0].meta["independent"] == 2


def test_fusion_weights_engines():
    # Brave carries weight 1.2; at equal rank it should outscore a bing-only hit.
    a = [_r("Brave hit", "https://brave-only.example/", "x" * 50, "brave")]
    b = [_r("Bing hit", "https://bing-only.example/", "x" * 50, "bing")]
    fused = _fuse({"brave": a, "bing": b}, "rrf")
    assert fused[0].url == "https://brave-only.example/"


def test_fusion_empty():
    assert _fuse({}, "rrf") == []


def test_round_robin_interleaves():
    a = [_r(f"a{i}", f"https://a.example/{i}", source="a") for i in range(3)]
    b = [_r(f"b{i}", f"https://b.example/{i}", source="b") for i in range(3)]
    out = _round_robin({"a": a, "b": b}, 6)
    assert [r.source for r in out] == ["a", "b", "a", "b", "a", "b"]


def test_round_robin_terminates_with_empty_bucket():
    # Regression: the old loop spun forever when one engine returned nothing.
    a = [_r("a0", "https://a.example/0", source="a")]
    out = _round_robin({"a": a, "empty": []}, 5)
    assert len(out) == 1


def test_cap_per_domain():
    results = [_r(str(i), f"https://same.example/{i}") for i in range(5)]
    results.append(_r("other", "https://other.example/x"))
    capped = _cap_per_domain(results, 2)
    assert sum(1 for r in capped if "same.example" in r.url) == 2
    assert any("other.example" in r.url for r in capped)


# --------------------------------------------------------------------------- #
# Cache keys — every result-affecting parameter must be part of the key
# --------------------------------------------------------------------------- #
def test_search_cache_key_includes_exclude(monkeypatch):
    from websearch import aggregate, net
    from websearch.engines.base import Engine, ENGINES

    calls = []

    def fake(query, limit, timeout=None):
        calls.append(query)
        # Both results mention the query, so this test exercises the cache key
        # rather than the relevance filter.
        return [_r("Python on W3Schools", "https://w3schools.com/x"),
                _r("Python tutorial", "https://good.example/y")]

    monkeypatch.setitem(ENGINES, "demo", Engine("demo", fake, "api", probe_query="x"))
    net.SEARCH_CACHE.clear()

    first = aggregate.search("python", limit=5, engines="demo", exclude=["w3schools"])
    second = aggregate.search("python", limit=5, engines="demo", exclude=["good"])
    assert [r.url for r in first.results] == ["https://good.example/y"]
    # Must not be served the first (differently filtered) cached copy.
    assert second.cached is False
    assert [r.url for r in second.results] == ["https://w3schools.com/x"]
    assert len(calls) == 2


def test_decode_body_honours_quoted_and_spaced_charset():
    """``charset="utf-8"`` and ``charset = utf-8`` are both legal.

    The old regex only matched the bare form, so a correctly declared
    ISO-8859-1 page fell through to sniffing and decoded to mojibake.
    """
    from websearch.net import decode_body

    body = "café".encode("latin-1")
    assert decode_body(body, 'text/html; charset="iso-8859-1"') == "café"
    assert decode_body(body, "text/html; charset = 'iso-8859-1'") == "café"
    assert decode_body(body, "text/html; charset=iso-8859-1") == "café"


def test_retry_after_is_not_clamped_to_our_backoff_ceiling(monkeypatch):
    """A server that says "wait 30s" must not be retried after 8s.

    Retrying early against an explicit Retry-After is what turns a soft 429
    into a ban, so the server's number wins over RETRY_MAX_DELAY.
    """
    import importlib

    net = importlib.import_module("websearch.net")

    class Resp:
        status_code = 429
        headers = {"retry-after": "30"}
        url = "https://example.com/"
        text = "busy"

        def json(self):
            return {}

    sleeps = []
    monkeypatch.setattr(net, "client", lambda: _FakeClient(Resp()))
    monkeypatch.setattr(net, "_limiter", _NoLimit())
    monkeypatch.setattr(net.time, "sleep", lambda s: sleeps.append(s))

    try:
        net.get("https://example.com/", retries=1, engine="x")
    except Exception:  # noqa: BLE001 - the retry is what matters
        pass
    assert sleeps, "no retry happened"
    assert sleeps[0] >= 30, f"retried after {sleeps[0]}s despite Retry-After: 30"


def test_stream_bytes_enforces_max_redirects_without_a_validator(monkeypatch):
    """``max_redirects`` used to be ignored unless a validator was passed."""
    import importlib

    net = importlib.import_module("websearch.net")

    class Resp:
        is_redirect = True
        status_code = 302
        headers = {"location": "/hop"}
        url = "https://example.com/"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Client:
        def stream(self, *a, **k):
            assert k.get("follow_redirects") is False, (
                "redirects must be followed manually so max_redirects applies"
            )
            return Resp()

    monkeypatch.setattr(net, "client", lambda: Client())
    monkeypatch.setattr(net, "_limiter", _NoLimit())

    with pytest.raises(Exception):
        net.stream_bytes("https://example.com/", max_bytes=1000, max_redirects=2)


class _NoLimit:
    def wait(self, host):
        return None

    def set_interval(self, host, seconds):
        return None


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def request(self, *a, **k):
        return self._resp


def test_junk_serp_is_dropped_not_returned(monkeypatch):
    """An all-junk result set must be an error, not a pass-through.

    Measured live: Bing answered "how to use git rebase" with eight Gmail
    login pages. An earlier version fell back to the unfiltered set whenever
    filtering emptied everything, which handed the caller exactly those Gmail
    links. A confident-looking wrong answer is worse than an honest zero.
    """
    from websearch import aggregate, net
    from websearch.engines.base import Engine, ENGINES

    def junk(query, limit, timeout=None):
        return [_r("Gmail - Email from Google", "https://mail.google.com/"),
                _r("Sign in - Google Accounts", "https://accounts.google.com/")]

    monkeypatch.setitem(ENGINES, "junkeng", Engine("junkeng", junk, "api", probe_query="x"))
    net.SEARCH_CACHE.clear()

    resp = aggregate.search("how to use git rebase", limit=5, engines="junkeng")
    assert resp.results == []
    assert "relevance_filter" in resp.errors
    assert resp.per_engine_counts["junkeng"] == 0


def test_off_topic_count_is_reported(monkeypatch):
    """Callers must be able to see what was filtered, per engine."""
    from websearch import aggregate, net
    from websearch.engines.base import Engine, ENGINES

    def mixed(query, limit, timeout=None):
        return [_r("Git rebase", "https://git-scm.com/docs/git-rebase"),
                _r("Gmail", "https://mail.google.com/")]

    monkeypatch.setitem(ENGINES, "mixeng", Engine("mixeng", mixed, "api", probe_query="x"))
    net.SEARCH_CACHE.clear()

    resp = aggregate.search("git rebase", limit=5, engines="mixeng")
    assert [r.url for r in resp.results] == ["https://git-scm.com/docs/git-rebase"]
    assert resp.filtered == {"mixeng": 1}


def test_non_latin_query_is_never_filtered(monkeypatch):
    """The tokenizer cannot read non-Latin scripts, so nothing may be dropped."""
    from websearch import aggregate, net
    from websearch.engines.base import Engine, ENGINES

    def ru(query, limit, timeout=None):
        return [_r("Привет, мир", "https://example.ru/hello")]

    monkeypatch.setitem(ENGINES, "rueng", Engine("rueng", ru, "api", probe_query="x"))
    net.SEARCH_CACHE.clear()

    resp = aggregate.search("привет мир", limit=5, engines="rueng")
    assert [r.url for r in resp.results] == ["https://example.ru/hello"]
    assert resp.filtered == {}


def test_fetch_cache_key_includes_max_chars(monkeypatch):
    import importlib

    net = importlib.import_module("websearch.net")
    fetch_mod = importlib.import_module("websearch.fetch")

    html = b"<html><body><article><p>" + b"word " * 500 + b"</p></article></body></html>"
    calls = []

    def fake_stream(url, **kwargs):
        calls.append(url)
        return html, "text/html", url

    monkeypatch.setattr(net, "stream_bytes", fake_stream)
    net.FETCH_CACHE.clear()

    short = fetch_mod.fetch("https://example.com/big", max_chars=300)
    long = fetch_mod.fetch("https://example.com/big", max_chars=20000)
    assert len(short["text"]) == 300
    assert len(long["text"]) > 300, "second fetch was served the truncated cache entry"
    assert long["cached"] is False
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# Engine routing
# --------------------------------------------------------------------------- #
def test_resolve_defaults():
    names, errors = resolve_engines("")
    assert names == ["brave", "bing", "yahoo", "duckduckgo"]
    assert errors == {}


def test_resolve_group_and_dedupe():
    names, _ = resolve_engines("academic,academic")
    assert "arxiv" in names and "pubmed" in names
    assert len(names) == len(set(names))


def test_resolve_all_and_fast():
    all_names, _ = resolve_engines("all")
    fast_names, _ = resolve_engines("fast")
    assert len(all_names) > len(fast_names) > 0


def test_resolve_reports_blocked_and_unknown():
    names, errors = resolve_engines("bing,google,madeup")
    assert names == ["bing"]
    assert "google" in errors and "disabled" in errors["google"]
    assert "madeup" in errors


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
HTML = """
<!DOCTYPE html>
<html><head><title>My Page</title>
<meta name="description" content="A description">
<style>.x{color:red}</style></head>
<body>
<nav><a href="/home">Home</a><a href="/about">About</a></nav>
<div class="cookie-banner">We use cookies. Accept?</div>
<article>
  <h1>Real Heading</h1>
  <p>First paragraph with a <a href="/link">link</a> and <strong>bold</strong> text.</p>
  <ul><li>one</li><li>two</li></ul>
  <pre><code class="language-python">print("hi")</code></pre>
  <table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
</article>
<footer>Copyright nobody</footer>
<script>var x = 1;</script>
</body></html>
"""


def test_text_extraction_drops_chrome():
    title, text, meta = html_to_text(HTML)
    assert title == "My Page"
    assert "Real Heading" in text
    assert "First paragraph" in text
    for junk in ("We use cookies", "Copyright nobody", "var x = 1", "color:red", "html"):
        assert junk not in text, f"boilerplate leaked: {junk!r}"
    assert meta.get("description") == "A description"


def test_markdown_extraction_structure():
    title, md, _ = html_to_markdown(HTML)
    assert title == "My Page"
    assert "# Real Heading" in md
    assert "[link](/link)" in md
    assert "**bold**" in md
    assert "```python" in md and 'print("hi")' in md
    assert "- one" in md
    assert "| A | B |" in md


def test_extract_links_resolves_relative():
    links = extract_links(HTML, "https://example.com/base/")
    urls = {link["url"] for link in links}
    assert "https://example.com/home" in urls
    assert "https://example.com/link" in urls


def test_extraction_survives_empty_and_broken_html():
    assert html_to_text("")[0] == ""
    title, text, _ = html_to_text("<div><p>unclosed <b>bold")
    assert "unclosed" in text


def test_markdown_does_not_crash_on_deep_nesting():
    deep = "<div>" * 200 + "text" + "</div>" * 200
    _, md, _ = html_to_markdown(deep)
    assert "text" in md


def test_snippet_tag_stripping_is_linear():
    """Snippets are attacker-controlled; the old ``<[^>]+>`` was quadratic."""
    import time

    from websearch.engines.api import _plain

    hostile = "<a " * 200_000  # no closing '>' anywhere
    started = time.monotonic()
    _plain(hostile)
    assert time.monotonic() - started < 2.0, "tag stripping is not linear"
    assert _plain("<b>Hello</b> &quot;world&quot;") == 'Hello "world"'


def test_boilerplate_hints_match_segments_not_substrings():
    # Regression: class="protocol" matched the "toc" hint and class="commentary"
    # matched "comment", so the extractor silently deleted the article body.
    from websearch.extract import _is_boilerplate

    def bp(html):
        from bs4 import BeautifulSoup

        return _is_boilerplate(BeautifulSoup(html, "lxml").find("div"))

    assert not bp('<div class="protocol">x</div>')
    assert not bp('<div class="commentary">x</div>')
    assert not bp('<div class="main-content">x</div>')
    assert bp('<div class="nav-menu">x</div>')
    assert bp('<div id="site-header">x</div>')
    assert bp('<div class="adsense">x</div>')

    # The article survives end to end.
    page = ('<html><body><div class="protocol"><p>' + "Protocol text. " * 20
            + "</p></div></body></html>")
    assert "Protocol text" in html_to_markdown(page)[1]


def test_markdown_nested_lists_keep_indentation():
    # Regression: nested <ul> collapsed into the parent item ("outerinner")
    # and the text normaliser stripped the indentation that encodes nesting.
    page = ("<html><body><article><p>" + "intro. " * 30 + "</p>"
            "<ul><li>outer<ul><li>inner</li></ul></li><li>second</li></ul>"
            "</article></body></html>")
    md = html_to_markdown(page)[1]
    lines = [ln for ln in md.splitlines() if ln.strip().startswith("-")]
    assert lines == ["- outer", "    - inner", "- second"], lines


# --------------------------------------------------------------------------- #
# Fetch safety
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/",
        "http://192.168.0.1/",
        "http://172.16.0.1/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "javascript:alert(1)",
        "https://user:pass@example.com/",
        "http://example.com:22/",
        "",
        "http://",
    ],
)
def test_ssrf_guard_blocks(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


def test_ssrf_guard_allows_public():
    assert validate_url("https://example.com/") == "https://example.com/"
    assert validate_url("https://example.com:8443/x") == "https://example.com:8443/x"


def test_redirect_targets_are_validated():
    """A public page must not be able to 302 into loopback / metadata.

    Regression: ``fetch`` validated only the URL the model supplied, then let
    httpx follow the redirect chain unvalidated — so any public page could
    reach ``http://127.0.0.1:8080/`` or ``http://169.254.169.254/``.
    """
    import http.server
    import threading

    from websearch import net

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/final":
                body = b"<html><body><article><p>internal secret</p></article></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        hops = []

        def validator(url):
            hops.append(url)
            # Simulate the policy: the first hop was approved, the redirect
            # target is not.
            if url.endswith("/final"):
                raise UnsafeURLError("redirect target is loopback")
            return url

        try:
            net.stream_bytes(f"http://127.0.0.1:{port}/", max_bytes=10_000,
                             validator=validator, timeout=5)
        except UnsafeURLError:
            pass
        else:
            raise AssertionError("redirect target was not validated")
        assert hops == [f"http://127.0.0.1:{port}/final"], hops
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
def test_breaker_trips_on_block_only():
    from websearch.breaker import Breaker
    from websearch.types import BlockedError, EngineError

    b = Breaker()
    assert b.available("x")
    # Ordinary failures do not trip the breaker.
    b.record_failure("x", EngineError("x", "timeout"))
    assert b.available("x")
    # A bot challenge does.
    b.record_failure("x", BlockedError("x", "captcha"))
    assert not b.available("x")
    assert b.remaining("x") > 0


def test_breaker_backoff_grows_and_success_resets():
    from websearch.breaker import Breaker
    from websearch.types import BlockedError

    b = Breaker()
    b.record_failure("x", BlockedError("x", "captcha"))
    first = b.remaining("x")
    b.record_failure("x", BlockedError("x", "captcha"))
    second = b.remaining("x")
    assert second > first
    b.record_success("x")
    assert b.available("x")
    assert b.remaining("x") == 0.0


def test_breaker_caps_cooldown():
    from websearch import breaker as breaker_mod
    from websearch.breaker import Breaker
    from websearch.types import BlockedError

    b = Breaker()
    for _ in range(20):
        b.record_failure("x", BlockedError("x", "captcha"))
    assert b.remaining("x") <= breaker_mod.MAX_COOLDOWN


@pytest.mark.parametrize(
    "url",
    [
        # Alternate numeric encodings of 127.0.0.1 — only caught post-resolution.
        "http://2130706433/",
        "http://0x7f000001/",
        "http://127.1/",
        "http://0177.0.0.1/",
        "http://017700000001/",
        "http://127.000.000.001/",
        "http://0/",
        # Wildcard DNS pointing at loopback.
        "http://127.0.0.1.nip.io/",
        # IPv6 forms that tunnel an IPv4 loopback address.
        "http://[::ffff:127.0.0.1]/",
        "http://[2002:7f00:1::]/",
        "http://[64:ff9b::7f00:1]/",
        # Multicast / reserved.
        "http://224.0.0.1/",
        "http://[ff02::1]/",
        "http://192.88.99.1/",
        "http://localhost./",
    ],
)
def test_ssrf_guard_blocks_encoded_bypasses(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


# --------------------------------------------------------------------------- #
# Regressions found by the second audit pass
# --------------------------------------------------------------------------- #
def test_degraded_empty_response_is_not_cached(monkeypatch):
    """An all-engines-in-cooldown response must not be pinned for the TTL.

    The cooldown error says "retry in Ns"; caching the empty result meant the
    retry was served the same stale failure long after the breaker expired.
    """
    from websearch import aggregate, net
    from websearch.engines.base import Engine, ENGINES
    from websearch.types import BlockedError

    def blocked(query, limit, timeout=None):
        raise BlockedError("downeng", "HTTP 429")

    monkeypatch.setitem(ENGINES, "downeng", Engine("downeng", blocked, "api", probe_query="x"))
    monkeypatch.setattr(aggregate, "BREAKER", aggregate.BREAKER.__class__())
    net.SEARCH_CACHE.clear()

    first = aggregate.search("python asyncio", limit=5, engines="downeng")
    assert first.results == [] and first.errors
    second = aggregate.search("python asyncio", limit=5, engines="downeng")
    assert second.cached is False, "a transient failure was served from cache"


def test_honest_empty_result_is_still_cached(monkeypatch):
    """No results and no errors is a real answer — cache it."""
    from websearch import aggregate, net
    from websearch.engines.base import Engine, ENGINES

    monkeypatch.setitem(ENGINES, "emptyeng",
                        Engine("emptyeng", lambda q, l, timeout=None: [], "api", probe_query="x"))
    net.SEARCH_CACHE.clear()

    aggregate.search("python asyncio", limit=5, engines="emptyeng")
    second = aggregate.search("python asyncio", limit=5, engines="emptyeng")
    assert second.cached is True


def test_http_status_failure_trips_the_breaker(monkeypatch):
    """A 5xx engine must cool down, not be retried forever.

    ``net.request`` raises HttpStatusError (not EngineError) for an
    unclassified non-2xx, so the old ``except EngineError`` branch never told
    the breaker anything.
    """
    from websearch import aggregate, net
    from websearch.engines.base import Engine, ENGINES
    from websearch.types import HttpStatusError

    def broken(query, limit, timeout=None):
        raise HttpStatusError("https://engine.example/", 503)

    monkeypatch.setitem(ENGINES, "brokeneng", Engine("brokeneng", broken, "api", probe_query="x"))
    breaker = aggregate.BREAKER.__class__()
    monkeypatch.setattr(aggregate, "BREAKER", breaker)
    net.SEARCH_CACHE.clear()

    aggregate.search("python asyncio", limit=5, engines="brokeneng")
    assert breaker.available("brokeneng") is False
    second = aggregate.search("python asyncio", limit=5, engines="brokeneng")
    assert "cooldown" in second.errors["brokeneng"]


def test_fuse_returns_the_whole_pool_for_later_filters():
    """Filters run after fusion; truncating first loses results permanently."""
    many = [_r(f"Hit {i}", f"https://a.example/{i}", source="a") for i in range(40)]
    fused = _fuse({"a": many}, "rrf")
    assert len(fused) == 40


def test_exclude_cache_key_distinguishes_comma_inside_a_term(monkeypatch):
    """``["a,b"]`` and ``["a","b"]`` are different filters, not one key."""
    from websearch import aggregate, net
    from websearch.engines.base import Engine, ENGINES

    calls = []

    def fake(query, limit, timeout=None):
        calls.append(1)
        return [_r("Python a b", "https://ab.example/x")]

    monkeypatch.setitem(ENGINES, "exeng", Engine("exeng", fake, "api", probe_query="x"))
    net.SEARCH_CACHE.clear()

    aggregate.search("python", limit=5, engines="exeng", exclude=["a,b"])
    second = aggregate.search("python", limit=5, engines="exeng", exclude=["a", "b"])
    assert second.cached is False
    assert len(calls) == 2


def test_search_cache_key_includes_timeout(monkeypatch):
    """A short-timeout call must not serve its degraded set to a patient one."""
    from websearch import aggregate, net
    from websearch.engines.base import Engine, ENGINES

    calls = []

    def fake(query, limit, timeout=None):
        calls.append(timeout)
        return [_r("Python asyncio", "https://docs.python.org/x")]

    monkeypatch.setitem(ENGINES, "teng", Engine("teng", fake, "api", probe_query="x"))
    net.SEARCH_CACHE.clear()

    aggregate.search("python asyncio", limit=5, engines="teng", timeout=2.0)
    second = aggregate.search("python asyncio", limit=5, engines="teng", timeout=20.0)
    assert second.cached is False
    assert calls == [2.0, 20.0]


def test_split_urls_handles_a_comma_followed_by_space():
    """``page, example.org`` must not fetch ``page,``."""
    from websearch.server import _split_urls

    assert _split_urls("https://example.com/page, example.org") == [
        "https://example.com/page", "example.org"]
    # Commas that belong to the URL are preserved.
    assert _split_urls("https://example.com/?q=a,b") == ["https://example.com/?q=a,b"]
    assert _split_urls("https://en.wikipedia.org/wiki/Foo_(bar,baz)") == [
        "https://en.wikipedia.org/wiki/Foo_(bar,baz)"]


def test_expand_query_uses_the_current_year():
    """A hardcoded year silently becomes "last year"."""
    import json as _json
    import time as _time

    from websearch.server import expand_query

    variants = _json.loads(expand_query("python asyncio"))["variants"]
    assert any(str(_time.gmtime().tm_year) in v for v in variants)


def test_short_page_keeps_its_body():
    """A page under the scoring threshold must not lose everything but the h1.

    ``_score`` is length-based and returns 0.0 below 120 characters, so every
    candidate on a short page tied and the first in document order won — which
    was the heading. A one-paragraph answer came back as the heading alone.
    """
    page = ("<html><body><h1>Release Notes</h1>"
            "<p>Version 1.2 fixes the parser.</p><p>Upgrade with pip.</p></body></html>")
    md = html_to_markdown(page)[1]
    assert "Version 1.2 fixes the parser." in md
    assert "Upgrade with pip." in md


def test_ties_in_container_selection_go_to_the_wider_element():
    """When scores tie, the element holding more text wins."""
    page = "<html><body><div><h2>Status</h2><p>No incidents today.</p></div></body></html>"
    md = html_to_text(page)[1]
    assert "No incidents today." in md


def test_empty_body_still_returns_a_string():
    assert html_to_markdown("<html><body></body></html>")[1] == ""


def test_boilerplate_escape_hatch_uses_segment_matching():
    """class="poster-ads" contains "post" but is not a content container.

    The deletion path used a substring test for the content hints, so an ad
    block whose class merely *contained* "post" survived into the output while
    a real <article class="post"> had to be preserved.
    """
    page = ('<html><body><div class="poster-ads"><p>Ad text here</p></div>'
            '<article><p>Real content.</p></article></body></html>')
    md = html_to_markdown(page)[1]
    assert "Real content." in md
    assert "Ad text here" not in md

    kept = '<html><body><article class="post"><p>Body text.</p></article></body></html>'
    assert "Body text." in html_to_markdown(kept)[1]


def test_fetch_cache_does_not_alias_nested_values(monkeypatch):
    """A caller mutating a returned page must not poison the cache.

    ``dict(payload)`` is a shallow copy: ``links`` and ``meta`` stayed the
    same objects the cache held, so appending to a returned link list made the
    entry grow for every later caller.
    """
    import importlib

    net = importlib.import_module("websearch.net")
    fetch_mod = importlib.import_module("websearch.fetch")
    monkeypatch.setattr(fetch_mod, "validate_url", lambda url, **k: url)
    body = (b"<html><body><article><h1>T</h1><a href='/a'>A</a><p>"
            + b"word " * 50 + b"</p></article></body></html>")
    monkeypatch.setattr(net, "stream_bytes", lambda url, **k: (body, "text/html", url))
    net.FETCH_CACHE.clear()

    first = fetch_mod.fetch("https://ex.example/p", include_links=True, max_chars=4000)
    first["links"].append({"text": "INJECTED", "url": "https://evil.example/"})
    first["text"] = "CORRUPTED"

    second = fetch_mod.fetch("https://ex.example/p", include_links=True, max_chars=4000)
    assert second["cached"] is True
    assert all(l["url"] != "https://evil.example/" for l in second["links"])
    assert second["text"] != "CORRUPTED"


def test_mcp_server_reports_its_own_version():
    """serverInfo.version must be ours, not the mcp SDK's.

    FastMCP takes no version argument and the low-level Server falls back to
    ``pkg_version("mcp")``, so clients were told this server was version
    "1.22.0" (the SDK's).
    """
    from websearch import __version__, server

    assert server.mcp._mcp_server.version == __version__


def test_code_fence_outgrows_backticks_inside_the_code():
    """A snippet containing ``` must not close its own fence."""
    page = "<html><body><article><pre>```inside```</pre></article></body></html>"
    md = html_to_markdown(page)[1]
    assert md.startswith("````"), md
    assert "```inside```" in md
