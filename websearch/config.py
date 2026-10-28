"""Runtime configuration, all overridable through environment variables."""
from __future__ import annotations

import os


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- networking ----------------------------------------------------------- #
DEFAULT_TIMEOUT = _env_float("WEBSEARCH_TIMEOUT", 20.0)
FETCH_TIMEOUT = _env_float("WEBSEARCH_FETCH_TIMEOUT", 30.0)
MAX_RETRIES = _env_int("WEBSEARCH_RETRIES", 2)
RETRY_BASE_DELAY = _env_float("WEBSEARCH_RETRY_BASE", 0.4)
RETRY_MAX_DELAY = _env_float("WEBSEARCH_RETRY_MAX", 8.0)
MAX_CONCURRENCY = _env_int("WEBSEARCH_CONCURRENCY", 8)
PROXY = os.environ.get("WEBSEARCH_PROXY") or None

# --- caching -------------------------------------------------------------- #
CACHE_ENABLED = _env_bool("WEBSEARCH_CACHE", True)
SEARCH_CACHE_TTL = _env_float("WEBSEARCH_SEARCH_TTL", 300.0)
FETCH_CACHE_TTL = _env_float("WEBSEARCH_FETCH_TTL", 900.0)
CACHE_MAX_ENTRIES = _env_int("WEBSEARCH_CACHE_MAX", 512)

# --- result shaping ------------------------------------------------------- #
MAX_RESULTS = _env_int("WEBSEARCH_MAX_RESULTS", 50)
DEFAULT_MAX_CHARS = _env_int("WEBSEARCH_MAX_CHARS", 20000)
MAX_DOWNLOAD_BYTES = _env_int("WEBSEARCH_MAX_DOWNLOAD", 5 * 1024 * 1024)
# A PDF-specific cap on top of the download cap. It defaults to the download
# cap because a *larger* value is unreachable: the body is streamed under
# MAX_DOWNLOAD_BYTES first, so a 20 MB PDF limit could never trigger and the
# caller got the generic "response exceeded N bytes" error instead of the PDF
# one. Raise WEBSEARCH_MAX_DOWNLOAD too if you want bigger PDFs; this value is
# then the tighter, PDF-only bound.
MAX_PDF_BYTES = min(_env_int("WEBSEARCH_MAX_PDF", MAX_DOWNLOAD_BYTES), MAX_DOWNLOAD_BYTES)

# Drop results that share no significant term with the query. Some keyless
# engines answer with a SERP for a completely different query from a
# datacenter IP; without this the fused output can be 100% off-topic while
# every engine reports success. See websearch/relevance.py.
RELEVANCE_FILTER = _env_bool("WEBSEARCH_RELEVANCE_FILTER", True)

# --- fetch safety --------------------------------------------------------- #
# Refuse to fetch loopback/private/link-local targets unless explicitly
# allowed. This matters because fetch_url takes URLs straight from a model.
ALLOW_PRIVATE_ADDRESSES = _env_bool("WEBSEARCH_ALLOW_PRIVATE", False)
MAX_REDIRECTS = _env_int("WEBSEARCH_MAX_REDIRECTS", 5)
