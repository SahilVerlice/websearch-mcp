"""URL normalisation helpers shared by the aggregator and the engines.

Kept in its own module because both :mod:`websearch.aggregate` and
:mod:`websearch.engines.html` need it, and importing one from the other would
be circular (the aggregator imports the engine registry).
"""
from __future__ import annotations

import re
import urllib.parse

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src",
    "igshid", "spm", "si", "feature", "yclid", "_openstat",
}


def canonical_url(url: str) -> str:
    """Normalise a URL for de-duplication.

    Keeps the query string (stripped of tracking params) and sorts it, so
    ``/watch?v=AAA`` and ``/watch?v=BBB`` stay distinct — a ``host+path`` key
    collapses every YouTube video into one hit.
    """
    if not url:
        return url
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", p.path).rstrip("/")
    pairs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    pairs.sort()
    query = urllib.parse.urlencode(pairs)
    out = f"{host}{path}"
    if query:
        out += "?" + query
    return out


def domain(url: str) -> str:
    """Bare hostname, ``www.`` stripped. Empty string for unparseable URLs."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host
