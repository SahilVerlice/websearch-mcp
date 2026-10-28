#!/usr/bin/env python3
"""websearch-mcp — a self-hosted, keyless web-search MCP server.

The implementation lives in the ``websearch`` package; this file is the CLI
entry point and the process the MCP client launches.

    python3 server.py                      # stdio MCP server (what clients run)
    python3 server.py --transport http     # streamable-http on 127.0.0.1:8000
    python3 server.py --selftest           # health-check every engine
    python3 server.py --list               # list engines and groups
    python3 server.py --search "query"     # one-shot search, prints JSON
    python3 server.py --fetch URL          # one-shot page fetch, prints JSON
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

# httpx logs every request at INFO; that would corrupt nothing on stderr but is
# noise for MCP clients, so keep the log level at WARNING.
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
for _noisy in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_list(verbose: bool) -> int:
    from websearch.engines import BLOCKED, ENGINES, ENGINE_ORDER, GROUPS

    print(f"{'engine':16} {'kind':5} {'groups':28} note")
    print("-" * 96)
    for name in ENGINE_ORDER:
        eng = ENGINES.get(name)
        if not eng or not eng.enabled:
            continue
        print(f"{name:16} {eng.kind:5} {','.join(eng.groups):28} {eng.note}")
    print("\ngroups:")
    for g in GROUPS:
        members = [n for n in ENGINE_ORDER if n in ENGINES and g in ENGINES[n].groups]
        print(f"  {g:12} {', '.join(members)}")
    print("\ndisabled (blocked from datacenter IPs or key-gated):")
    for name, why in BLOCKED.items():
        print(f"  {name:16} {why}")
    if verbose:
        from websearch import config

        print("\nconfig:")
        for key in dir(config):
            if key.isupper():
                print(f"  {key} = {getattr(config, key)}")
    return 0


def cmd_selftest(query: str, timeout: float) -> int:
    from websearch.server import check_engines

    report = json.loads(check_engines(query, timeout))
    print(f"selftest query: {query!r}\n")
    for row in report["engines"]:
        if row["ok"]:
            print(f"[ OK ] {row['engine']:16} {row['count']:>2} in {row['seconds']:5.2f}s | {row['top'][:56]}")
        else:
            print(f"[FAIL] {row['engine']:16} {row['seconds']:5.2f}s | {row['error'][:70]}")
    print(f"\n{report['ok']}/{report['total']} engines returned results")
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Keyless web search + page fetch MCP server")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "http", "sse"],
                    help="MCP transport (default: stdio)")
    ap.add_argument("--host", default="127.0.0.1", help="bind host for http/sse")
    ap.add_argument("--port", type=int, default=8000, help="bind port for http/sse")
    ap.add_argument("--selftest", action="store_true", help="health-check every engine and exit")
    ap.add_argument("--query", default="python tutorial", help="query for --selftest")
    ap.add_argument("--timeout", type=float, default=15.0, help="per-engine timeout for --selftest")
    ap.add_argument("--list", action="store_true", help="list engines and exit")
    ap.add_argument("--verbose", action="store_true", help="show config with --list")
    ap.add_argument("--search", metavar="QUERY", help="one-shot search, prints JSON")
    ap.add_argument("--engines", default="", help="engine spec for --search")
    ap.add_argument("--limit", type=int, default=10, help="result limit for --search")
    ap.add_argument("--site", default="", help="restrict --search to a domain")
    ap.add_argument("--fetch", metavar="URL", help="one-shot page fetch, prints JSON")
    ap.add_argument("--max-chars", type=int, default=20000, help="char cap for --fetch")
    ap.add_argument("--fmt", default="markdown", choices=["markdown", "text", "html"],
                    help="output format for --fetch")
    ap.add_argument("--version", action="store_true", help="print version and exit")
    args = ap.parse_args(argv)

    if args.version:
        from websearch import __version__

        print(__version__)
        return 0
    if args.list:
        return cmd_list(args.verbose)
    if args.selftest:
        return cmd_selftest(args.query, args.timeout)
    if args.search:
        from websearch.aggregate import search

        resp = search(args.search, limit=args.limit, engines=args.engines,
                      site=args.site)
        _print(resp.as_dict())
        return 0
    if args.fetch:
        from websearch.fetch import fetch

        _print(fetch(args.fetch, max_chars=args.max_chars, fmt=args.fmt))
        return 0

    from websearch.server import run

    run(args.transport, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
