"""Domain-guarded, non-user-bound Playwright MCP for LibreChat.

Two problems this solves that plain @playwright/mcp doesn't, without adding
a second system (no proxy container, no firewall config to keep in sync):

1. Playwright MCP's own --allowed-origins/--block-service-workers are
   enforced but INVISIBLE to the calling model (its own README calls them
   "not a security boundary" -- known gap: service workers/WebSockets
   bypass context.route(), --block-service-workers closes one of those).
   That's still the real enforcement here -- this module does not
   reimplement it.
2. The allowlist never appears in any tool description or the initialize
   `instructions`, so a model has no way to know upfront which domains
   are reachable -- it just fails, repeatedly, one blocked domain at a
   time, instead of planning around a known-reachable set.

This module spawns the actual `@playwright/mcp` CLI (upstream, unmodified,
brings its own browser/context handling) as a child process listening on
a loopback-only internal port, connects to it as an MCP *client*, and
re-exposes it as an MCP *server* on the container's public port: tools/list
is passed through with the domain list appended to any tool description
whose inputSchema takes a `url` argument, initialize's `instructions` gets
the same list, and tools/call is forwarded verbatim. ALLOWED_ORIGINS is the
single source of truth -- read once, handed to both the upstream process
(as --allowed-origins, real enforcement) and the two response rewrites
(visibility only, not enforcement) -- so extending it later is a one-line
env var change, not two configs to keep in sync.

No MCP_API_KEY, no auth gate: a 401 from any gate makes LibreChat's
non-OAuth MCP client try and get stuck on OAuth. Docker network isolation
is the boundary. No credentials of any kind flow through here -- this only
ever reads public pages.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
from typing import Any

import anyio
import httpx
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route, Router

logger = logging.getLogger(__name__)

INTERNAL_PORT = 8081
PUBLIC_PORT = int(os.environ.get("PORT", "8080"))
_ALLOWED_ORIGINS_RAW = os.environ.get("ALLOWED_ORIGINS", "").strip()
_ALLOWED_ORIGINS = [o.strip() for o in _ALLOWED_ORIGINS_RAW.split(";") if o.strip()]

# "localhost", not "127.0.0.1": @playwright/mcp checks the Host header
# (DNS-rebinding protection) against what it printed at startup
# ("Listening on http://localhost:<port>") and 403s a literal-IP Host.
_UPSTREAM_URL = f"http://localhost:{INTERNAL_PORT}/mcp"

_DOMAIN_LIST_HUMAN = ", ".join(_ALLOWED_ORIGINS) if _ALLOWED_ORIGINS else "(keine -- ALLOWED_ORIGINS ist leer, jede Domain wird geblockt)"

# Free-text, deployment-specific operating knowledge (e.g. "this domain is
# a self-hosted SearXNG, use ?categories=science for Google Scholar
# results") -- kept out of this module on purpose. The domain allowlist is
# generic and enforced; how to actually use what's behind a given domain
# is deployment knowledge this wrapper has no business hardcoding. Appended
# verbatim, so the deployer's wording is what the model sees.
_EXTRA_INSTRUCTIONS = os.environ.get("EXTRA_INSTRUCTIONS", "").strip()

_INSTRUCTIONS = (
    "Dieser Browser-MCP kann ausschliesslich folgende Domains erreichen "
    f"(serverseitig erzwungen, alles andere schlaegt fehl): {_DOMAIN_LIST_HUMAN}. "
    "Plane Aufrufe entsprechend, statt andere Domains zu versuchen."
    + (f"\n\n{_EXTRA_INSTRUCTIONS}" if _EXTRA_INSTRUCTIONS else "")
)

_TOOL_NOTE = f"\n\nErlaubte Domains (serverseitig erzwungen): {_DOMAIN_LIST_HUMAN}."


def _spawn_upstream() -> subprocess.Popen:
    args = [
        "node", "/app/cli.js",
        "--headless", "--browser", "chromium", "--no-sandbox",
        "--host", "127.0.0.1", "--port", str(INTERNAL_PORT),
        "--block-service-workers",
    ]
    if _ALLOWED_ORIGINS_RAW:
        args += ["--allowed-origins", _ALLOWED_ORIGINS_RAW]
    logger.info("spawning upstream playwright-mcp: %s", " ".join(args))
    return subprocess.Popen(args)


async def _wait_for_upstream(timeout_seconds: float = 30.0) -> None:
    async def _probe() -> bool:
        try:
            async with httpx.AsyncClient() as client:
                # Any response (including 4xx/406 from a GET on a
                # streamable-http endpoint without a session) means the
                # port is accepting connections -- that's all we need.
                await client.get(_UPSTREAM_URL, timeout=1.0)
            return True
        except httpx.TransportError:
            return False

    with anyio.fail_after(timeout_seconds):
        while not await _probe():
            await anyio.sleep(0.3)


def _annotate_tool(tool: types.Tool) -> types.Tool:
    props = (tool.inputSchema or {}).get("properties", {})
    if "url" not in props:
        return tool
    return tool.model_copy(update={"description": (tool.description or "") + _TOOL_NOTE})


def build_server(upstream: ClientSession) -> Server:
    server: Server = Server("playwright-mcp-guarded", instructions=_INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        result = await upstream.list_tools()
        return [_annotate_tool(t) for t in result.tools]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return await upstream.call_tool(name, arguments or {})

    return server


class _MCPEndpoint:
    """Class instance, not a function -- Starlette only treats instances as
    raw ASGI (see Route.__init__); a function gets wrapped as
    func(request)->Response and, if mounted by prefix, 307-redirects a bare
    path. That redirect broke a companion project (time-mcp-http) live:
    LibreChat's MCP client blocks redirects to private/reserved addresses
    as SSRF hardening, and any Docker-internal MCP address qualifies.
    Route with a class-instance endpoint never redirects.

    The session manager isn't available until the lifespan has finished
    connecting to the spawned upstream process (an async step) -- but the
    Router's route list has to exist synchronously when build_app() returns,
    before any event loop is running. So this holds a settable reference
    instead of the app mutating the route table later: same object identity
    for the route's whole lifetime, only its `_session_manager` attribute
    changes, once, when lifespan reaches `yield`.
    """

    def __init__(self) -> None:
        self._session_manager: StreamableHTTPSessionManager | None = None

    async def __call__(self, scope, receive, send) -> None:
        if self._session_manager is None:
            # Only reachable if a request lands mid-startup, before
            # lifespan's startup event has completed -- uvicorn normally
            # doesn't dispatch requests that early, this is a safety net.
            response = PlainTextResponse("starting up", status_code=503)
            await response(scope, receive, send)
            return
        await self._session_manager.handle_request(scope, receive, send)


def build_app() -> Router:
    endpoint = _MCPEndpoint()
    upstream_proc: subprocess.Popen | None = None

    async def livez(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def readyz(_request: Request) -> PlainTextResponse:
        if endpoint._session_manager is None:
            return PlainTextResponse("not ready", status_code=503)
        return PlainTextResponse("ok")

    @contextlib.asynccontextmanager
    async def lifespan(_app: Router):
        nonlocal upstream_proc
        if not _ALLOWED_ORIGINS:
            logger.warning("ALLOWED_ORIGINS is empty -- every navigation will be blocked")

        upstream_proc = _spawn_upstream()
        try:
            await _wait_for_upstream()
            async with streamablehttp_client(_UPSTREAM_URL) as (read, write, _get_session_id):
                async with ClientSession(read, write) as upstream:
                    await upstream.initialize()
                    server = build_server(upstream)
                    session_manager = StreamableHTTPSessionManager(
                        app=server, json_response=False, stateless=True,
                    )
                    async with session_manager.run():
                        endpoint._session_manager = session_manager
                        logger.info(
                            "playwright-mcp-guarded up, allowed origins=%s",
                            _DOMAIN_LIST_HUMAN,
                        )
                        yield
        finally:
            endpoint._session_manager = None
            if upstream_proc is not None:
                upstream_proc.terminate()
                try:
                    upstream_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    upstream_proc.kill()

    return Router(
        routes=[
            Route("/livez", livez),
            Route("/readyz", readyz),
            Route("/mcp", endpoint, methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )
