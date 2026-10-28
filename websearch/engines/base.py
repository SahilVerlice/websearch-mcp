"""Engine definition + registry."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..types import EngineError, Result

SearchFn = Callable[..., list[Result]]


@dataclass
class Engine:
    """One search backend.

    ``search(query, limit, timeout=None)`` must return a list of
    :class:`Result` and may raise :class:`EngineError` on failure.
    """

    name: str
    search: SearchFn
    kind: str                      # "html" | "api"
    groups: tuple[str, ...] = ()
    fast: bool = True              # safe to include in latency-sensitive calls
    enabled: bool = True
    note: str = ""
    requires: tuple[str, ...] = () # optional python deps
    # Whether the engine understands ``site:example.com`` in the query. Only
    # general web engines do; API engines need post-filtering instead.
    site_syntax: bool = False
    # Which web index the engine actually searches. Several "different" engines
    # are fronts for the same index (DuckDuckGo, Yahoo and Bing all serve
    # Bing's index), so their agreement is NOT independent corroboration. The
    # fusion counts agreement per family, not per engine name.
    index_family: str = ""
    # Relative trust in this engine's ordering. 1.0 is neutral.
    weight: float = 1.0
    # Query used by health checks. Narrow corpora (Wikinews, Commons, Wikidata)
    # legitimately have no matches for a generic query like "python tutorial",
    # which would otherwise look like a broken engine.
    probe_query: str = "python tutorial"

    def __call__(self, query: str, limit: int, timeout: float | None = None) -> list[Result]:
        return self.search(query, limit, timeout=timeout)


ENGINES: dict[str, Engine] = {}
# Engines deliberately disabled, with the reason, so callers get an explanation
# rather than a silent zero-result.
BLOCKED: dict[str, str] = {}


def register(engine: Engine) -> Engine:
    ENGINES[engine.name] = engine
    return engine


def get_engine(name: str) -> Engine:
    eng = ENGINES.get(name)
    if eng is None:
        if name in BLOCKED:
            raise EngineError(name, f"disabled: {BLOCKED[name]}")
        raise EngineError(name, f"unknown engine '{name}'")
    if not eng.enabled:
        raise EngineError(name, f"engine '{name}' is disabled: {eng.note or 'not enabled'}")
    return eng
