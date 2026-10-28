"""websearch-mcp — keyless web search and page fetching for MCP.

Public surface::

    from websearch import search, fetch, ENGINES, BLOCKED

Everything else is an implementation detail and may move.
"""
from __future__ import annotations

__version__ = "2.0.0"

from .types import Result, SearchResponse, EngineError, BlockedError, UnsafeURLError
from .engines import ENGINES, BLOCKED, DEFAULT_ENGINES, ENGINE_ORDER, get_engine
from .aggregate import search
from .fetch import fetch, extract_links

__all__ = [
    "__version__",
    "Result",
    "SearchResponse",
    "EngineError",
    "BlockedError",
    "UnsafeURLError",
    "ENGINES",
    "BLOCKED",
    "DEFAULT_ENGINES",
    "ENGINE_ORDER",
    "get_engine",
    "search",
    "fetch",
    "extract_links",
]
