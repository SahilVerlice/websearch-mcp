"""Live network tests — deselected by default.

Run them explicitly::

    python3 -m pytest tests/test_live.py -q -m live

They are kept separate because they depend on third-party sites staying up and
on the network being reachable from this host.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.live

from websearch.aggregate import search  # noqa: E402
from websearch.engines import ENGINES, ENGINE_ORDER  # noqa: E402
from websearch.fetch import fetch  # noqa: E402

API_ENGINES = [n for n in ENGINE_ORDER
               if n in ENGINES and ENGINES[n].enabled and ENGINES[n].kind == "api"]


@pytest.mark.parametrize("engine", API_ENGINES)
def test_api_engine_returns_results(engine):
    eng = ENGINES[engine]
    results = eng.search(eng.probe_query, 3, timeout=45)
    assert results, f"{engine} returned nothing"
    for r in results:
        assert r.title
        assert r.url.startswith("http")


def test_default_search_fuses_engines():
    resp = search("python asyncio", limit=5)
    assert resp.results
    assert resp.per_engine_counts
    assert len(resp.results) <= 5
    # At least one hit should be corroborated by two engines on a common query.
    assert any(r.meta.get("agreement", 1) > 1 for r in resp.results)


def test_search_respects_site_filter():
    resp = search("tutorial", limit=5, site="docs.python.org")
    assert resp.results
    assert all("docs.python.org" in r.url for r in resp.results)


def test_search_exclude_filter():
    resp = search("python tutorial", limit=8, exclude="w3schools,geeksforgeeks")
    assert all("w3schools" not in r.url for r in resp.results)


def test_fetch_extracts_article_text():
    page = fetch("https://docs.python.org/3/tutorial/index.html", max_chars=3000)
    assert page["kind"] == "html"
    assert "Python" in page["title"]
    assert "tutorial" in page["text"].lower()
    # Boilerplate must not survive.
    assert "Skip to content" not in page["text"]


def test_fetch_json_passthrough():
    page = fetch("https://api.github.com/zen", max_chars=500)
    assert page["kind"] in {"json", "text"}
    assert page["text"].strip()


def test_fetch_rejects_private_address():
    from websearch.types import UnsafeURLError

    with pytest.raises(UnsafeURLError):
        fetch("http://127.0.0.1:8080/")


def test_fetch_is_cached():
    url = "https://example.com/"
    first = fetch(url, max_chars=200)
    second = fetch(url, max_chars=200)
    assert first["cached"] is False
    assert second["cached"] is True
