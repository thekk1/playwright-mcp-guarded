# playwright-mcp-guarded

A domain-guarded, non-user-bound wrapper around Microsoft's
[playwright-mcp](https://github.com/microsoft/playwright-mcp) — a general
web-fetch/browse replacement for LibreChat assistants (GitHub, Wikipedia,
Google, Google Scholar, ...), scoped to an allowlist of domains, extensible
by editing one environment variable. Deployed as a plain streamable-HTTP
Docker service (no `ports:` exposed, no per-user OAuth) alongside sibling
projects [`time-mcp-http`](https://github.com/thekk1/time-mcp-http) and
`ews-mcp`'s multi-user mode, which follow the same shape.

## Why not just run `@playwright/mcp` directly

It already has `--allowed-origins`/`--blocked-origins` for exactly this,
and this project **does not reimplement that enforcement** — it's still
what actually blocks a disallowed navigation. Two things are missing from
running it bare:

1. Its own docs call `--allowed-origins` "not a security boundary" — a
   known gap is that Service Worker and WebSocket traffic bypass the
   `context.route()` interception it's built on. `--block-service-workers`
   (also set here, unconditionally) closes one of those gaps; there isn't
   a flag for the other.
2. The allowlist never appears anywhere the calling model can see it —
   not in `initialize`'s `instructions`, not in any tool's `description`.
   A model has no way to know upfront which domains are reachable, so it
   finds out by trying, once per blocked domain, every time.

## How it works

One container, one process tree, one config value (`ALLOWED_ORIGINS`):

```
LibreChat --(streamable-http, /mcp)--> guardian (Python, PID 1)
                                          |
                                          | spawns as child process,
                                          | on loopback only
                                          v
                                    @playwright/mcp (Node, --allowed-origins)
```

`guardian/app.py` spawns the real `@playwright/mcp` CLI, unmodified, as a
child process bound to `localhost` only, connects to it as an MCP
*client*, and re-exposes it as an MCP *server* on the container's public
port: `tools/list` and `initialize` get the domain list appended for
visibility, `tools/call` is forwarded verbatim — no tool logic of its own,
no changed behavior beyond those two text edits. `ALLOWED_ORIGINS` is read
once, at startup, and handed to both: the spawned process (as
`--allowed-origins`, the actual enforcement) and the two response
rewrites (visibility only). Extending the list later is a one-line change
to that single env var, not two configs to keep in sync.

## Run

```
docker build -t playwright-mcp-guarded .
docker run --rm -p 8080:8080 \
  -e ALLOWED_ORIGINS="github.com;raw.githubusercontent.com;wikipedia.org;en.wikipedia.org;google.com;scholar.google.com" \
  playwright-mcp-guarded
```

`ALLOWED_ORIGINS` is semicolon-separated — same format `@playwright/mcp`
itself uses for `PLAYWRIGHT_MCP_ALLOWED_ORIGINS`. An empty value blocks
every domain (logged as a warning at startup, not a silent bypass).

`EXTRA_INSTRUCTIONS` (optional, free text) is appended verbatim to the
`initialize` `instructions` the model sees. The allowlist only says *which*
domains are reachable, not *how* to use what's behind them — e.g. a
self-hosted search engine's query syntax. Deliberately not hardcoded here:
this wrapper stays domain-agnostic, and the deployer supplies whatever
operating knowledge their specific allowlist needs, in their own words:

```
-e EXTRA_INSTRUCTIONS="Search: http://searxng:8080/search?q=<term> (general), add &categories=science for Google Scholar results."
```

No `MCP_API_KEY`, no auth gate on the MCP connection itself — deliberately,
same reasoning as `ews-mcp`/`time-mcp-http`: a 401 from any gate makes
LibreChat's non-OAuth MCP client try (and get stuck on) OAuth. Docker
network isolation is the boundary. No credentials of any kind pass through
this server; it only ever reads public pages.

**Accepted trade-off, not an oversight:** this keeps enforcement at the
Playwright application layer (now with `--block-service-workers`), not an
independent network-level boundary (e.g. an egress proxy). That's a
deliberate simplicity choice — one deployable unit, one config value — not
a claim that this is airtight. A network-level backstop can be layered on
later without touching any of this logic.

## Verify

```
curl -s http://127.0.0.1:8080/readyz   # "ok" once the upstream browser is up
```

Logs should show the upstream's own startup line
(`Listening on http://localhost:<internal-port>`) followed by
`playwright-mcp-guarded up, allowed origins=...` — that second line is the
actual list currently enforced, not just what was configured.

## Test

```
pip install -e '.[dev]'
ALLOWED_ORIGINS="github.com;wikipedia.org" pytest
```

Unit tests only (description/instructions rewriting, arg passthrough) —
they don't spin up Chromium or Docker. The Docker image was verified
end-to-end manually: built, run, a real MCP client connected and listed
tools (allowlist visible in both `initialize.instructions` and
`browser_navigate`'s description), navigated successfully to an allowed
domain (`en.wikipedia.org`), and got `net::ERR_BLOCKED_BY_CLIENT` on a
disallowed one (`example.com`) — enforcement and visibility both
confirmed live, not just asserted.
