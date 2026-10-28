"""HTTP plumbing: shared client, retries, per-host rate limiting, caching.

Design notes
------------
* A single module-level :class:`httpx.Client` is reused so TLS handshakes and
  connection pools survive between tool calls. It is thread-safe, which is what
  lets :mod:`websearch.aggregate` fan out over a thread pool.
* Retries only happen for transient failures (connect/read errors, 429, 5xx).
  A 404 or a captcha page is not retried.
* Every non-2xx response raises :class:`HttpStatusError` by default. The old
  code silently parsed error pages and returned zero results, which made
  "blocked" indistinguishable from "no matches".
"""
from __future__ import annotations

import datetime
import email.utils
import logging
import random
import re
import threading
import time
import urllib.parse
from collections import OrderedDict

import httpx

from . import config
from .types import BlockedError, HttpStatusError

log = logging.getLogger("websearch.net")

# Upper bound on a server-supplied Retry-After. Long enough to respect real
# backoff requests, short enough that a hostile or buggy header cannot stall a
# tool call indefinitely. This is deliberately NOT config.RETRY_MAX_DELAY,
# which bounds our own exponential backoff, not the server's instruction.
_RETRY_AFTER_MAX = 60.0

USER_AGENTS = [
    # Desktop Chrome / Firefox / Safari. Rotated per request so a single host
    # does not see one static fingerprint.
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
}

# Phrases that show up on bot-challenge / captcha interstitials. Matching one of
# these lets us report "blocked" instead of "zero results".
_BLOCK_MARKERS = (
    "captcha",
    "unusual traffic",
    "are you a robot",
    "not a bot",
    "verify you are human",
    "enable javascript and cookies",
    "access denied",
    "request blocked",
    "rate limit exceeded",
    "too many requests",
)

_ua_lock = threading.Lock()
_ua_idx = 0


def next_user_agent() -> str:
    global _ua_idx
    with _ua_lock:
        ua = USER_AGENTS[_ua_idx % len(USER_AGENTS)]
        _ua_idx += 1
        return ua


# --------------------------------------------------------------------------- #
# Per-host rate limiting
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Minimum interval between requests to the same host, thread-safe."""

    def __init__(self, default_interval: float = 0.0):
        self._default = default_interval
        self._intervals: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def set_interval(self, host: str, seconds: float) -> None:
        with self._lock:
            self._intervals[host] = seconds

    def wait(self, host: str) -> None:
        interval = self._intervals.get(host, self._default)
        if interval <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                last = self._last.get(host)
                if last is None or now - last >= interval:
                    self._last[host] = now
                    return
                sleep_for = interval - (now - last)
            time.sleep(min(sleep_for, interval))


RATE_LIMITS: dict[str, float] = {
    # Politeness / documented limits.
    "export.arxiv.org": 3.0,       # arXiv asks for >=3s between requests
    "eutils.ncbi.nlm.nih.gov": 0.4,  # 3 req/s without an API key
    "api.openverse.org": 1.0,
    "api.openverse.engineering": 1.0,
    "search.brave.com": 2.0,
    "lite.duckduckgo.com": 2.0,
    "html.duckduckgo.com": 2.0,
    "www.bing.com": 1.0,
    "search.yahoo.com": 1.0,
    "search.marginalia.nu": 2.0,
    # The only Marginalia host that still renders results. Without an entry it
    # inherits the default interval (0) and gets hammered into the "barraged by
    # queries from bots" throttle after a couple of calls.
    "old-search.marginalia.nu": 4.0,
    "api.marginalia.nu": 2.0,
    "api.openalex.org": 0.2,
    "scholar.google.com": 3.0,
    "www.mojeek.com": 2.0,
    "crates.io": 1.0,
    "registry.npmjs.org": 0.2,
    "gitlab.com": 1.0,
    "lemmy.world": 1.0,
}

_limiter = RateLimiter()
for _h, _iv in RATE_LIMITS.items():
    _limiter.set_interval(_h, _iv)


# --------------------------------------------------------------------------- #
# TTL cache
# --------------------------------------------------------------------------- #
class TTLCache:
    """Tiny thread-safe LRU + TTL cache. Enough for search/fetch memoisation."""

    def __init__(self, max_entries: int, ttl: float):
        self.max_entries = max_entries
        self.ttl = ttl
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str):
        if not config.CACHE_ENABLED or self.ttl <= 0:
            return None
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            ts, value = item
            if time.monotonic() - ts > self.ttl:
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value) -> None:
        if not config.CACHE_ENABLED or self.ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = self.misses = 0


SEARCH_CACHE = TTLCache(config.CACHE_MAX_ENTRIES, config.SEARCH_CACHE_TTL)
FETCH_CACHE = TTLCache(config.CACHE_MAX_ENTRIES, config.FETCH_CACHE_TTL)


# --------------------------------------------------------------------------- #
# Shared client
# --------------------------------------------------------------------------- #
_client: httpx.Client | None = None
_client_lock = threading.Lock()


def client() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    timeout=httpx.Timeout(config.DEFAULT_TIMEOUT, connect=10.0),
                    follow_redirects=True,
                    max_redirects=config.MAX_REDIRECTS,
                    headers=BASE_HEADERS,
                    proxy=config.PROXY,
                    limits=httpx.Limits(
                        max_connections=32,
                        max_keepalive_connections=16,
                        keepalive_expiry=60.0,
                    ),
                    http2=False,
                )
    return _client


def close() -> None:
    """Close the shared client.

    In-flight requests hold their own reference to the client object, so
    ``client.close()`` while another thread is mid-request fails that request
    with "client has been closed". ``close()`` is only meant for interpreter
    shutdown, where that does not matter, but it is documented here so a
    future caller does not use it as a mid-run reset. Use :func:`reset_client`
    for tests instead, which swaps the client without closing a live one.
    """
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def reset_client() -> None:
    """Drop the shared client without closing it (for tests).

    The abandoned client is garbage-collected, which closes its connection
    pool only once no request is still using it.
    """
    global _client
    with _client_lock:
        _client = None


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse ``Retry-After`` (delta-seconds or an HTTP-date) into a delay.

    Two traps this handles:

    * ``str.isdigit()`` is true for superscripts like ``²``, and ``float('²')``
      raises ``ValueError``. That escaped the old code uncaught and would have
      crashed the retry loop on a malformed header, so the conversion is
      guarded and the regex only accepts ASCII digits.
    * ``parsedate_to_datetime`` returns a *naive* datetime for the ``-0000``
      zone and the legacy asctime form. ``timestamp()`` then interprets it in
      the machine's local timezone, shifting the wait by the UTC offset. Naive
      results are treated as UTC, which is what the RFC means.
    """
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    if re.fullmatch(r"[0-9]+", raw):
        try:
            return float(raw)
        except ValueError:
            return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, dt.timestamp() - time.time())


def _is_retryable(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.RemoteProtocolError,
            httpx.ReadError,
        ),
    )


def looks_blocked(resp: httpx.Response) -> str | None:
    """Return the matched marker if the body looks like a bot challenge."""
    if resp.status_code in (401, 403, 429):
        # Not necessarily a captcha, but definitely refused.
        return f"HTTP {resp.status_code}"
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype:
        return None
    head = resp.text[:6000].lower()
    for marker in _BLOCK_MARKERS:
        if marker in head:
            return marker
    return None


def request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    data: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: float | None = None,
    retries: int | None = None,
    allow_status: tuple[int, ...] = (),
    engine: str = "",
) -> httpx.Response:
    """Perform an HTTP request with retries, rate limiting and status checks.

    Raises :class:`BlockedError` when the response looks like a bot challenge
    and :class:`HttpStatusError` for other unexpected statuses.
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    retries = config.MAX_RETRIES if retries is None else retries
    timeout = config.DEFAULT_TIMEOUT if timeout is None else timeout
    last_exc: Exception | None = None

    for attempt in range(retries + 1):
        _limiter.wait(host)
        # Callers that need a pinned fingerprint pass their own User-Agent;
        # otherwise we rotate through the browser pool.
        hdrs = {"User-Agent": next_user_agent()}
        if headers:
            hdrs.update(headers)
        try:
            resp = client().request(
                method,
                url,
                params=params,
                data=data,
                json=json_body,
                headers=hdrs,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            last_exc = exc
            if not _is_retryable(exc) or attempt >= retries:
                raise
            delay = min(
                config.RETRY_MAX_DELAY,
                config.RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 0.25),
            )
            log.debug("retry %s %s after %s (%.2fs)", method, url, type(exc).__name__, delay)
            time.sleep(delay)
            continue

        if resp.status_code in allow_status:
            return resp

        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt < retries:
                retry_after = _retry_after_seconds(resp)
                if retry_after is not None:
                    # Honour the server's own number. Clamping it to
                    # RETRY_MAX_DELAY (8 s by default) made the client retry
                    # while the server had explicitly said "not before N
                    # seconds", which is what turns a soft 429 into a ban.
                    # The wait is still bounded so a hostile Retry-After
                    # cannot stall a call forever.
                    delay = min(retry_after, _RETRY_AFTER_MAX)
                else:
                    delay = min(
                        config.RETRY_MAX_DELAY,
                        config.RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 0.25),
                    )
                log.debug(
                    "retry %s %s on HTTP %s (%.2fs)", method, url, resp.status_code, delay
                )
                time.sleep(delay)
                continue
            # Exhausted: fall through to raise with a useful message.

        if resp.status_code >= 400:
            marker = looks_blocked(resp)
            if marker and resp.status_code in (401, 403, 429):
                raise BlockedError(
                    engine or host,
                    f"{marker} (refused after {attempt + 1} attempt(s))",
                    status=resp.status_code,
                )
            raise HttpStatusError(str(resp.url), resp.status_code, body=resp.text[:400])

        return resp

    assert last_exc is not None
    raise last_exc


def get(url: str, **kwargs) -> httpx.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> httpx.Response:
    return request("POST", url, **kwargs)


def get_json(url: str, **kwargs):
    resp = get(url, **kwargs)
    try:
        return resp.json()
    except ValueError as exc:
        raise HttpStatusError(str(resp.url), resp.status_code, body="invalid JSON") from exc


def stream_bytes(url: str, *, max_bytes: int, headers: dict | None = None,
                 timeout: float | None = None, validator=None,
                 max_redirects: int | None = None) -> tuple[bytes, str, str]:
    """Stream a URL into memory, aborting once ``max_bytes`` is exceeded.

    Returns ``(body, content_type, final_url)``. Streaming keeps a hostile or
    accidental multi-gigabyte response from exhausting memory.

    ``validator`` (a ``callable(url) -> url`` that raises on rejection) is
    applied to **every** hop of the redirect chain, not just the first URL.
    Without it, a public page could 302 to ``http://169.254.169.254/`` or
    ``http://127.0.0.1:8080/`` and the SSRF policy would never see it.

    Redirects are always followed manually here, even with no validator, so
    that ``max_redirects`` is actually enforced. Delegating to httpx (the old
    behaviour when ``validator`` was ``None``) silently ignored the argument
    and applied httpx's own limit instead.
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    _limiter.wait(host)
    hdrs = {"User-Agent": next_user_agent()}
    if headers:
        hdrs.update(headers)
    max_redirects = config.MAX_REDIRECTS if max_redirects is None else max_redirects

    current = url
    for hop in range(max_redirects + 1):
        if validator is not None and hop > 0:
            # Each redirect target gets the same policy as the original URL.
            current = validator(current)
        if hop > 0:
            # Politeness applies to the hop's host, which may differ.
            _limiter.wait(urllib.parse.urlsplit(current).hostname or "")
        chunks: list[bytes] = []
        total = 0
        with client().stream(
            "GET",
            current,
            headers=hdrs,
            timeout=timeout or config.FETCH_TIMEOUT,
            follow_redirects=False,
        ) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise HttpStatusError(str(resp.url), resp.status_code,
                                          body="redirect without Location")
                current = urllib.parse.urljoin(str(resp.url), location)
                if hop >= max_redirects:
                    raise HttpStatusError(str(resp.url), resp.status_code,
                                          body=f"exceeded {max_redirects} redirects")
                continue
            if resp.status_code >= 400:
                raise HttpStatusError(str(resp.url), resp.status_code)
            ctype = resp.headers.get("content-type", "")
            for chunk in resp.iter_bytes(65536):
                total += len(chunk)
                if total > max_bytes:
                    raise HttpStatusError(
                        str(resp.url), resp.status_code,
                        body=f"response exceeded {max_bytes} bytes",
                    )
                chunks.append(chunk)
            return b"".join(chunks), ctype, str(resp.url)
    raise HttpStatusError(url, 0, body=f"exceeded {max_redirects} redirects")


def decode_body(body: bytes, content_type: str) -> str:
    """Decode bytes using the declared charset, then charset_normalizer."""
    # The charset may be quoted (``charset="utf-8"``) and may have spaces
    # around the ``=``; the old ``charset=([\w\-]+)`` matched neither, so a
    # perfectly well-declared page fell through to sniffing and came back as
    # mojibake ("café" -> "捡曩").
    m = re.search(r"""charset\s*=\s*["']?([\w\-:.]+)["']?""", content_type or "", re.I)
    if m:
        try:
            return body.decode(m.group(1), "replace")
        except LookupError:
            pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(body).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    return body.decode("utf-8", "replace")
