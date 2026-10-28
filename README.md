# websearch-mcp

A self-hosted, **keyless** web-search MCP server. It searches through engines
that require no API key and no billing, fuses their results, and reads pages as
clean markdown. Runs as a stdio MCP server that Claude Code launches.

```bash
python3 server.py            # stdio MCP server (what MCP clients run)
python3 server.py --selftest # health-check every engine
```

## Tools exposed to Claude Code

| Tool | Purpose |
|------|---------|
| `web_search` | Query several engines in parallel, fuse with reciprocal rank fusion, de-duplicate |
| `search_engine` | Run one named engine and return its raw ordering |
| `fetch_url` | Fetch a page → markdown / text / html, with SSRF protection |
| `multi_fetch` | Fetch up to 12 pages concurrently (read the top search hits in one call) |
| `list_engines` | Enabled engines, groups, and why others are disabled |
| `check_engines` | Live health-check of every engine with timings |
| `suggest` | Query autocomplete suggestions |
| `expand_query` | Show routing and generate query variants (no network) |
| `server_info` | Version, cache stats, effective limits |

### `web_search` highlights

* **Parallel fan-out** — engines run concurrently, so wall-clock is the slowest
  engine, not the sum.
* **Reciprocal rank fusion** — a hit found by three engines at ranks 2–5 beats a
  single engine's outlier at rank 1. Each result carries
  `meta.agreement` and `meta.also_in`. Agreement is counted per *index family*,
  not per engine name: DuckDuckGo, Yahoo and Bing all serve the Bing index, so
  three hits there count as one opinion (`meta.independent`).
* **Off-topic filtering** — keyless engines sometimes answer with a SERP for a
  completely different query (measured: Bing answering "how to use git rebase"
  with eight Gmail login pages). Results sharing no significant term with the
  query are dropped before fusion and reported in `filtered_off_topic`. If
  *every* engine does this, the call returns an error rather than passing the
  junk through.
* **Engine groups** — `engines="academic"`, `"code"`, `"social"`, `"media"`,
  `"news"`, `"reference"`, `"indie"`, `"web"`, or `"all"` / `"fast"` / `"api"`.
* **Filters** — `site=` (pushed down into engines that understand `site:`),
  `exclude=`, `max_per_domain=` for diversity.
* **Caching** — 5-minute TTL cache; `no_cache=True` bypasses it. The cache key
  covers every result-affecting parameter (query, engines, limit, per-engine
  count, site, strategy, per-domain cap, exclude list and timeout), so a
  short-timeout call can never serve its degraded result set to a patient one.
  A response that is empty *because engines failed* is not cached — only a
  genuine "no results".

### `fetch_url` highlights

* **Query-focused reading** — pass `query="..."` and only the passages that
  answer it come back, in document order, within `max_chars`. A 54,000-character
  docs page typically returns ~2,000 relevant characters. The response reports
  `passages` (blocks kept), `passage_matched` (whether the query matched
  anything at all), `total_chars` (the size of the **whole** document, not the
  kept slice, so the caller can judge what it is missing) and `truncated`
  (whether content was actually dropped — a fully returned page is never
  flagged just because rejoining paragraphs normalised whitespace).
* **Formats** — `markdown` (default), `text`, or `html` (raw, for parser
  debugging).

## Engines (27, all keyless)

**Web (HTML):** `duckduckgo` (Lite), `bing`, `yahoo`, `brave`, `wiby`,
`marginalia`

**Web (API):** `wikipedia`, `wikinews`, `wikidata`, `mwmbl`

**News (RSS):** `bingnews`, `gnews` — structured XML, so a site redesign cannot
break the parser. Bing News links are decoded from their `apiclick.aspx`
wrapper to the real article; Google News hands out JS-only wrapper URLs, so
those results point at the publisher (the wrapper is kept in
`meta.google_news_url`).

**Code:** `github`, `gitlab`, `stackexchange`, `npm`, `crates`

**Academic:** `arxiv`, `crossref`, `openalex`, `pubmed`

**Other:** `hn`, `lemmy` (social), `openlibrary` (books), `openverse`,
`commons`, `archive` (media/archives)

Default set: `brave,bing,yahoo,duckduckgo` — the four broad-web engines verified
to work from a datacenter IP. Brave answers best but rate-limits under sustained
use; DuckDuckGo starts serving anomaly pages; the circuit breaker cools each one
down and the others carry the call.

### Intentionally disabled

These block datacenter IPs with captchas or require credentials, so they return
nothing useful here. They are reported in `list_engines()` with the reason, and
requesting one returns an explicit error rather than an empty result set:
`google`, `google-scholar`, `mojeek`, `startpage`, `ecosia`, `searx`, `yandex`,
`baidu`, `reddit`, `lobsters`, `dblp`, `bluesky`, `semanticscholar`,
`googlebooks`.

## CLI

```bash
python3 server.py --version
python3 server.py --list [--verbose]
python3 server.py --selftest [--query "..."] [--timeout 15]
python3 server.py --search "query" [--engines academic,code] [--limit 10] [--site docs.python.org]
python3 server.py --fetch URL [--max-chars 20000] [--fmt markdown|text|html]
python3 server.py --transport http --host 127.0.0.1 --port 8000
```

## MCP registration

Registered in the **user** scope, so it is available in every project:

```bash
claude mcp add websearch --scope user -- /usr/bin/python3 /root/Tools/websearch-mcp/server.py
claude mcp list        # should show: websearch - ✔ Connected
```

Or add it per-project with a `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "websearch": {
      "command": "/usr/bin/python3",
      "args": ["/root/Tools/websearch-mcp/server.py"]
    }
  }
}
```

## Safety

`fetch_url` and `multi_fetch` take URLs that a model produced, so they are
guarded against SSRF:

* only `http`/`https`; no credentials in the URL; ports restricted to 80/443/8080/8443
* the hostname is resolved and **every** address must be public — loopback,
  RFC1918, link-local (incl. `169.254.169.254`), CGNAT, multicast, reserved,
  IPv6 unique-local, IPv4-mapped, 6to4 and Teredo-embedded addresses are rejected
* redirects are followed **manually**, and the policy is re-applied to every
  hop, so a public page cannot 302 to loopback or the metadata endpoint
  (`WEBSEARCH_MAX_REDIRECTS`, default 5)
* responses are streamed and aborted past `WEBSEARCH_MAX_DOWNLOAD` (5 MB)

Set `WEBSEARCH_ALLOW_PRIVATE=1` to allow intranet fetching. Known limitation:
the check happens before connecting, so a DNS-rebinding attacker could still
point the connection at a private address; closing that fully needs an IP-pinned
transport.

## Configuration

Every setting is an environment variable:

| Variable | Default | Meaning |
|---|---|---|
| `WEBSEARCH_TIMEOUT` | 20 | per-request timeout (s) |
| `WEBSEARCH_FETCH_TIMEOUT` | 30 | page-fetch timeout (s) |
| `WEBSEARCH_RETRIES` | 2 | retries for transient failures / 429 / 5xx |
| `WEBSEARCH_CONCURRENCY` | 8 | parallel engine workers |
| `WEBSEARCH_CACHE` | 1 | enable the TTL cache |
| `WEBSEARCH_SEARCH_TTL` | 300 | search cache TTL (s) |
| `WEBSEARCH_FETCH_TTL` | 900 | fetch cache TTL (s) |
| `WEBSEARCH_MAX_RESULTS` | 50 | upper bound on `limit` |
| `WEBSEARCH_MAX_DOWNLOAD` | 5242880 | max bytes streamed per fetch |
| `WEBSEARCH_MAX_PDF` | same as above | tighter PDF-only size cap (cannot exceed `MAX_DOWNLOAD`) |
| `WEBSEARCH_ALLOW_PRIVATE` | 0 | allow private/loopback targets |
| `WEBSEARCH_RELEVANCE_FILTER` | 1 | drop results sharing no term with the query |
| `WEBSEARCH_PROXY` | — | proxy URL for all requests |

## Dependencies

Required: `mcp` (FastMCP), `httpx`, `beautifulsoup4`, `lxml`.

Optional: `charset_normalizer` (better non-UTF-8 decoding), `pypdf` (PDF text
extraction — without it PDFs return a notice instead of text), `html2text`
(fallback renderer if the built-in markdown converter throws).

## Tests

```bash
python3 -m pytest tests/ -q              # offline unit tests
python3 -m pytest tests/ -q -m live      # live network tests
python3 server.py --selftest             # every engine, with timings
```

## Notes

* Result URLs are de-duplicated on host+path+**query** (tracking parameters
  stripped), so distinct YouTube videos no longer collapse into one hit.
* Bing/Yahoo redirect wrappers and DuckDuckGo's `uddg=` wrapper are decoded back
  to the real destination; sponsored DDG rows are filtered out.
* Bing's market parameters (`cc`, `mkt`, `setlang`) are deliberately not sent.
  Measured from a datacenter IP: a bare `q` returns 20/20 on-topic results,
  while adding `cc=us` returns 0/20 — Bing serves a generic unrelated SERP.
  `Accept-Language` is enough to get English results.
* Requests rotate through four browser User-Agents; arXiv (3 s), NCBI, Openverse
  and the HTML engines have per-host rate limits.
* A non-2xx response or a captcha page raises a `BlockedError`/`HttpStatusError`
  instead of silently parsing the error page into zero results.
