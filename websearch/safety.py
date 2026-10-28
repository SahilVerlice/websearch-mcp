"""URL safety policy for :func:`websearch.fetch.fetch`.

``fetch_url`` accepts arbitrary model-supplied URLs, so it must not become an
SSRF primitive. The policy is deny-by-default for anything that is not a public
http(s) destination.

Threats this covers
-------------------
* non-http(s) schemes (``file:``, ``gopher:``, ``data:``, ...)
* credentials in the URL (``https://user:pass@host``)
* private / loopback / link-local / CGNAT / multicast / reserved addresses,
  including the ``169.254.169.254`` cloud-metadata endpoint
* alternate numeric encodings (``http://2130706433/``, ``http://0x7f000001/``,
  ``http://127.1/``) — these are validated *after* DNS resolution, so the
  string form does not matter
* wildcard DNS that resolves a public-looking name to a private IP
* IPv6 forms that embed an IPv4 address: ``::ffff:127.0.0.1`` (mapped),
  ``2002:7f00:1::`` (6to4), ``2001::/32`` (Teredo), ``64:ff9b::/96`` (NAT64)

Two stdlib traps this avoids: ``is_global`` is *True* for multicast and for
NAT64-wrapped loopback, and ``is_private`` is *False* for CGNAT
(``100.64.0.0/10``). Neither flag alone is sufficient, so explicit ranges are
used as well.

Known limitation
----------------
The check runs before connecting, so a DNS record that changes between our
lookup and httpx's own resolution could still point at a private address (DNS
rebinding). httpx request hooks run *before* DNS, so they cannot pin the dialed
IP either. Fully closing this needs a custom transport that pins the validated
address; that is deliberately out of scope here, and
``WEBSEARCH_ALLOW_PRIVATE`` exists for the legitimate intranet use case.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse

from .types import UnsafeURLError

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}

BLOCK_V4 = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",        # "this network"
    "10.0.0.0/8",       # RFC 1918
    "100.64.0.0/10",    # carrier-grade NAT
    "127.0.0.0/8",      # loopback
    "169.254.0.0/16",   # link-local, incl. cloud metadata
    "172.16.0.0/12",    # RFC 1918
    "192.0.0.0/24",     # IETF protocol assignments
    "192.0.2.0/24",     # TEST-NET-1
    "192.88.99.0/24",   # 6to4 relay anycast
    "192.168.0.0/16",   # RFC 1918
    "198.18.0.0/15",    # benchmarking
    "198.51.100.0/24",  # TEST-NET-2
    "203.0.113.0/24",   # TEST-NET-3
    "224.0.0.0/4",      # multicast
    "240.0.0.0/4",      # reserved + broadcast
)]

BLOCK_V6 = [ipaddress.ip_network(n) for n in (
    "::/128",           # unspecified
    "::1/128",          # loopback
    "::ffff:0:0/96",    # IPv4-mapped
    "64:ff9b::/96",     # NAT64 well-known prefix
    "64:ff9b:1::/48",   # NAT64 local-use
    "100::/64",         # discard-only
    "2001:db8::/32",    # documentation
    "2002::/16",        # 6to4
    "fc00::/7",         # unique local
    "fe80::/10",        # link-local
    "ff00::/8",         # multicast
)]


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Extract an IPv4 address tunnelled inside an IPv6 address."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    return None


def _blocked(ip: ipaddress._BaseAddress) -> str | None:
    """Return a reason string if the address must not be fetched."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_v4(ip)
        if embedded is not None and _blocked(embedded) is not None:
            return f"tunnelled IPv4 {embedded} in {ip}"
    ranges = BLOCK_V4 if ip.version == 4 else BLOCK_V6
    if any(ip in net for net in ranges):
        return f"{ip} is in a reserved range"
    # Belt and braces: these flags catch anything the explicit lists missed,
    # but note is_global alone is NOT sufficient (multicast and NAT64 are
    # "global"), which is why the range lists come first.
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return f"{ip} is not a public address"
    return None


def validate_url(url: str, *, allow_private: bool = False) -> str:
    """Validate scheme/host/port and resolved addresses. Returns the URL."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("empty URL")
    url = url.strip()
    try:
        parts = urllib.parse.urlsplit(url)
        # Accessing .port raises ValueError for malformed values.
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError(f"malformed URL: {exc}") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(
            f"scheme '{scheme or '(none)'}' is not allowed; use http or https"
        )
    if not parts.hostname:
        raise UnsafeURLError("URL has no host")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URLs are not allowed")
    if port is not None and port not in ALLOWED_PORTS:
        raise UnsafeURLError(f"port {port} is not allowed")

    if allow_private:
        return url

    host = parts.hostname.rstrip(".")
    if not host:
        raise UnsafeURLError("URL has no host")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise UnsafeURLError("localhost is not allowed")

    try:
        infos = socket.getaddrinfo(
            host, port or (443 if scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeURLError(f"cannot resolve host '{host}': {exc}") from exc

    if not infos:
        raise UnsafeURLError(f"host '{host}' did not resolve")

    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])  # strip IPv6 zone id
        except ValueError:
            # Unparseable address: fail closed.
            raise UnsafeURLError(f"unparsable address {raw!r} for host '{host}'")
        reason = _blocked(ip)
        if reason:
            raise UnsafeURLError(f"host '{host}' resolves to a blocked address: {reason}")
    return url


def is_safe(url: str) -> bool:
    try:
        validate_url(url)
        return True
    except UnsafeURLError:
        return False
