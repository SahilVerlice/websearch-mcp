# websearch-mcp

A self-hosted web-search MCP server that uses **search engines requiring no API
key**. No third-party search API, no billing, no Claude web-search dependency.

## Tools exposed to Claude Code

| Tool | Purpose |
|------|---------|
| `web_search(query, limit, engines)` | Query several engines at once, de-duplicated, round-robin merged |
| `search_engine(engine, query, limit)` | Query one specific engine |
| `fetch_url(url, max_chars)` | Fetch a page and return its readable text |
| `list_engines()` | Show enabled and blocked engines |

## Engines (16, all keyless)

**HTML search engines:** `brave`, `bing`, `yahoo`, `wiby` (indie/old-web index)

**APIs:** `wikipedia`, `mwmbl`, `github`, `stackexchange`, `hn` (Hacker News),
`lemmy` (federated forums), `openverse` (CC media), `arxiv`, `crossref`,
`openlibrary`, `pubmed`, `wikidata`

Use `engines: "all"` to query every engine, or pass a comma-separated list.

### Intentionally disabled

These block datacenter IPs with CAPTCHA or serve JS-only pages, so they return
nothing useful here: `google`, `duckduckgo`, `mojeek`, `startpage`, `ecosia`,
`searx` instances, `yandex`, `baidu`. They are listed in `list_engines()` output
so callers get a clear reason instead of a silent failure.

## CLI

```bash
python3 server.py                # run as an MCP stdio server
python3 server.py --selftest     # test every engine and print results
python3 server.py --list         # list engines
python3 server.py --search "query" [--engines brave,bing] [--limit 10]
python3 server.py --fetch "https://example.com"
```

## MCP registration

Registered in `/root/Downloads/.mcp.json` as `websearch` and enabled in
`/root/Downloads/.claude/settings.local.json`.

## Dependencies

`python3` with `httpx`, `beautifulsoup4`, `lxml`, and `mcp` (FastMCP).

## Notes

- Result URLs are de-duplicated by normalized host+path.
- Bing and Yahoo result links are wrapped in redirect URLs; the server decodes
  them back to the real target.
- Requests rotate through three browser User-Agents.
