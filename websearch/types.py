"""Core value types and error hierarchy."""
from __future__ import annotations

from dataclasses import dataclass, field


class WebsearchError(Exception):
    """Base class for all errors raised by this package."""


class EngineError(WebsearchError):
    """A search engine failed (network, HTTP status, parse, ...)."""

    def __init__(self, engine: str, message: str, *, status: int | None = None):
        self.engine = engine
        self.status = status
        self.message = message
        super().__init__(f"{engine}: {message}")


class BlockedError(EngineError):
    """The engine answered with a captcha / bot-challenge page."""


class HttpStatusError(WebsearchError):
    """An upstream returned an unexpected HTTP status."""

    def __init__(self, url: str, status: int, *, body: str = ""):
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status} from {url}")


class UnsafeURLError(WebsearchError):
    """A URL was rejected by the fetch safety policy (SSRF / scheme)."""


@dataclass
class Result:
    """A single search hit."""

    title: str
    url: str
    snippet: str = ""
    source: str = ""
    rank: int = 0
    published: str = ""
    score: float = 0.0
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "rank": self.rank,
        }
        if self.published:
            d["published"] = self.published
        if self.score:
            d["score"] = round(self.score, 6)
        if self.meta:
            # Nested on purpose: engine metadata must never clobber the core
            # fields (the old code did d.update(self.meta) and could overwrite
            # title/url if an engine ever returned those keys).
            d["meta"] = self.meta
        return d


@dataclass
class SearchResponse:
    """Aggregated result set for one query."""

    query: str
    results: list[Result]
    engines_requested: list[str]
    per_engine_counts: dict[str, int]
    errors: dict[str, str]
    timings: dict[str, float] = field(default_factory=dict)
    strategy: str = "rrf"
    cached: bool = False
    # Per-engine count of results removed as irrelevant to the query. Reported
    # rather than silent: a caller must be able to tell "this engine had
    # nothing" from "this engine answered a different question".
    filtered: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "result_count": len(self.results),
            "engines_used": self.engines_requested,
            "per_engine_counts": self.per_engine_counts,
            "errors": self.errors,
            "timings_ms": {k: round(v * 1000, 1) for k, v in self.timings.items()},
            "strategy": self.strategy,
            "cached": self.cached,
            **({"filtered_off_topic": self.filtered} if self.filtered else {}),
            "results": [r.as_dict() for r in self.results],
        }

    def to_cache(self) -> dict:
        """A detached, plain-data snapshot for the TTL cache.

        The cache must not hold the live :class:`Result` objects: a caller
        that touches ``meta`` on a returned result would then mutate every
        later cache hit for the same query. Plain dicts are copied in and
        rebuilt out.
        """
        return {
            "query": self.query,
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet,
                 "source": r.source, "rank": r.rank, "published": r.published,
                 "score": r.score, "meta": dict(r.meta)}
                for r in self.results
            ],
            "engines_requested": list(self.engines_requested),
            "per_engine_counts": dict(self.per_engine_counts),
            "errors": dict(self.errors),
            "timings": dict(self.timings),
            "strategy": self.strategy,
            "filtered": dict(self.filtered),
        }

    @classmethod
    def from_cache(cls, data: dict) -> "SearchResponse":
        return cls(
            query=data["query"],
            results=[Result(**r) for r in data["results"]],
            engines_requested=list(data["engines_requested"]),
            per_engine_counts=dict(data["per_engine_counts"]),
            errors=dict(data["errors"]),
            timings=dict(data.get("timings") or {}),
            strategy=data.get("strategy", "rrf"),
            cached=True,
            filtered=dict(data.get("filtered") or {}),
        )
