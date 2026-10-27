# playwright-mcp-guarded

A domain-guarded wrapper around Microsoft's
[playwright-mcp](https://github.com/microsoft/playwright-mcp) — a general
web-fetch/browse replacement for LibreChat assistants (GitHub, Wikipedia,
Google, Google Scholar, ...), scoped to an allowlist of domains, extensible
by editing one environment variable. Deployed as a plain streamable-HTTP
Docker service (no `ports:` exposed, no per-user OAuth), with one isolated
browser context per user. See also
[`time-mcp-http`](https://github.com/thekk1/time-mcp-http), a sibling
project with the same deployment shape.

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
visibility, `tools/call` is forwarded with file arguments removed (see below). One tool
of its own, `browser_read`, is built from upstream tools. `ALLOWED_ORIGINS` is read
once, at startup, and handed to both: the spawned process (as
`--allowed-origins`, the actual enforcement) and the two response
rewrites (visibility only). Extending the list later is a one-line change
to that single env var, not two configs to keep in sync.

### One browser context per user

The guardian opens one upstream MCP session per user, keyed by a request
header (`SESSION_HEADER`, default `X-LibreChat-User`). Upstream runs with
`--isolated`, so each session gets its own in-memory browser context in one
shared Chromium: own tabs, own cookies, own page state. With a single shared
session, every user would see every other user's open tabs in each tool
response, and two concurrent users would steer the same page.

LibreChat sets the header per user and, because of the placeholder, opens
one MCP connection per user instead of one shared app connection:

```yaml
mcpServers:
  browse:
    type: streamable-http
    url: http://playwright-mcp:8080/mcp
    headers:
      X-LibreChat-User: "{{LIBRECHAT_USER_ID}}"
```

LibreChat never passes `initialize` `instructions` of such a per-user
server to the model (it only reads them in its startup inspection, which
skips per-user servers). The guardian therefore appends the complete
instructions — domain list, `browser_snapshot` hint, `EXTRA_INSTRUCTIONS`
— to every tool that takes a `url` (`browser_navigate`, `browser_tabs`);
tool descriptions arrive per connection.

Requests without the header share one fallback session (logged as a
warning). A session idle for `SESSION_IDLE_SECONDS` (default 900) is
closed, and with it that user's browser context; `browser_close` closes it
right away. The next tool call opens a fresh one.

### Results always inline

The calling model can't read files inside this container, so any result
that upstream writes to a file is a dead end:

- `--snapshot-mode none`: navigate, click, type etc. return no snapshot
  (upstream would write it to a file and return only a link). The
  instructions tell the model to read with `browser_read` and to use
  `browser_snapshot` (accessibility tree, inline) only for element
  references.
- The `filename` argument is removed from every tool schema and every
  `tools/call` (`browser_snapshot`, `browser_evaluate`,
  `browser_console_messages`, ...), so results come back in the response.
- The console-log file link upstream adds to every response is replaced
  by `browser_console_messages`.
- `--output-dir /tmp/playwright-mcp-output` (capped at 50 MB): screenshot
  files still land there, the working directory isn't writable.

### `browser_read`

Navigates (optional `url`) and returns the rendered text of the main
content in one call, via upstream `browser_navigate` and
`browser_evaluate` in the user's own session — the allowlist applies
unchanged. Navigation, forms, selects and similar chrome are hidden while
reading; links to other hosts carry their URL as `<url>`, same-host links
don't. Long pages come in sections of 30000 characters, `offset` continues.

Why: with navigate + snapshot every page costs two calls, each a full model
turn, and the accessibility tree is several times the size of the text
(German Wikipedia article on Nike: 125849 characters as snapshot, 27597 via
`browser_read`; a SearXNG result page: 32721 vs. 9959). Models then pick
fragments with `browser_find` instead of reading the page.

### Blocked tools

`browser_run_code_unsafe` is hidden from `tools/list` and refused in
`tools/call`. It runs model-supplied code in the upstream Node process
(`node:vm`, no sandbox; upstream's own description calls it
RCE-equivalent), so a prompt injection from any visited page could execute
code in the container and reach the network past the domain allowlist.
Upstream counts it as a `core` tool and has no option to drop it.

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
`initialize` `instructions` the model sees, and with them to the
description of every tool that takes a `url`. The allowlist only says *which*
domains are reachable, not *how* to use what's behind them — e.g. a
self-hosted search engine's query syntax. Deliberately not hardcoded here:
this wrapper stays domain-agnostic, and the deployer supplies whatever
operating knowledge their specific allowlist needs, in their own words:

```
-e EXTRA_INSTRUCTIONS="Search: http://searxng:8080/search?q=<term> (general), add &categories=science for Google Scholar results."
```

No `MCP_API_KEY`, no auth gate on the MCP connection itself — deliberately:
a 401 from any such gate makes LibreChat's non-OAuth MCP client try (and
get stuck on) OAuth. Docker network isolation is the boundary. No
credentials of any kind pass through this server; it only ever reads
public pages.

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

Unit tests only (description/instructions/schema rewriting, arg
passthrough, per-user session pool) — they don't spin up Chromium or
Docker. The Docker image was verified
end-to-end manually: built, run, a real MCP client connected and listed
tools (allowlist visible in both `initialize.instructions` and
`browser_navigate`'s description), navigated successfully to an allowed
domain (`en.wikipedia.org`), and got `net::ERR_BLOCKED_BY_CLIENT` on a
disallowed one (`example.com`) — enforcement and visibility both
confirmed live, not just asserted.
