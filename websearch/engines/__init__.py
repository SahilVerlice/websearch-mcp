"""Engine registry.

Importing this package registers every engine. Order matters only for display
and for ``DEFAULT_ENGINES``; execution order is decided by the aggregator.
"""
from __future__ import annotations

from .base import BLOCKED, ENGINES, Engine, get_engine, register

# Import for side effects (registration).
from . import api as _api  # noqa: F401,E402
from . import html as _html  # noqa: F401,E402
from . import news as _news  # noqa: F401,E402

# Engines that block datacenter IPs with captchas or serve JS-only pages.
# Listed explicitly so callers get a reason instead of a silent empty result.
BLOCKED.update({
    "google": "JS-gated / bot-blocked from datacenter IPs (needs an API key)",
    "mojeek": "persistent captcha from datacenter IPs",
    "startpage": "captcha from datacenter IPs",
    "ecosia": "403 from datacenter IPs",
    "searx": "public instances rate-limit / captcha",
    "yandex": "captcha from datacenter IPs",
    "baidu": "bot-blocked from datacenter IPs",
    "google-scholar": "captcha from datacenter IPs",
    "reddit": "403 from datacenter IPs (requires OAuth)",
    "lobsters": "Anubis bot challenge",
    "dblp": "Anubis bot challenge",
    "bluesky": "403 for unauthenticated search",
    "semanticscholar": "429 without an API key",
    "googlebooks": "daily quota exhausted without an API key",
})

# Broad-web engines tried first. All verified working keyless from a datacenter
# IP. DuckDuckGo is listed last on purpose: it works from a cold IP but starts
# answering 202 anomaly pages after sustained use, and the circuit breaker then
# skips it for a cool-down.
DEFAULT_ENGINES = ["brave", "bing", "yahoo", "duckduckgo"]

# Stable display order: general web first, then the long tail.
ENGINE_ORDER = [
    "duckduckgo", "bing", "yahoo", "brave", "wiby", "marginalia", "mwmbl",
    "wikipedia", "wikinews", "wikidata", "github", "gitlab", "stackexchange",
    "npm", "crates", "hn", "lemmy", "arxiv", "crossref", "openalex", "pubmed",
    "openlibrary", "openverse", "commons", "archive", "bingnews", "gnews",
]

GROUPS = sorted({g for e in ENGINES.values() for g in e.groups})

__all__ = [
    "BLOCKED", "ENGINES", "Engine", "get_engine", "register",
    "DEFAULT_ENGINES", "ENGINE_ORDER", "GROUPS",
]
