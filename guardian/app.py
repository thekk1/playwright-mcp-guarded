"""Domain-guarded, per-user isolated Playwright MCP for LibreChat.

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
the same list, and tools/call is forwarded with file arguments removed (see
_FILE_ARGS). ALLOWED_ORIGINS is the
single source of truth -- read once, handed to both the upstream process
(as --allowed-origins, real enforcement) and the two response rewrites
(visibility only, not enforcement) -- so extending it later is a one-line
env var change, not two configs to keep in sync.

Each browser user gets their own upstream MCP session, and with it their
own browser context (upstream runs with --isolated: one Chromium, one
in-memory context per connected client). A single shared upstream session
would hand every user the same tabs, cookies and page state -- one user's
research shows up as an open tab in another user's tool output, and two
concurrent users steer the same page. The user is identified by a request
header (SESSION_HEADER) the MCP client sets per user; see _UpstreamPool.

No MCP_API_KEY, no auth gate: a 401 from any gate makes LibreChat's
non-OAuth MCP client try and get stuck on OAuth. Docker network isolation
is the boundary. No credentials of any kind flow through here -- this only
ever reads public pages.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
from typing import Any

import anyio
import anyio.abc
import httpx
from mcp import types
from mcp.shared.exceptions import McpError
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

# Header that identifies the browser user, set by the MCP client per user
# (LibreChat: `headers: {X-LibreChat-User: "{{LIBRECHAT_USER_ID}}"}`, which
# also makes LibreChat open one MCP connection per user instead of one
# shared app connection). Requests without it share one fallback session --
# the pre-isolation behavior, logged as a warning, not silently.
_SESSION_HEADER = os.environ.get("SESSION_HEADER", "X-LibreChat-User").strip().lower()
_FALLBACK_KEY = ""

# Idle upstream sessions are closed after this many seconds, which closes
# that user's browser context (tabs, cookies) upstream. Next tool call
# starts a fresh one.
_SESSION_IDLE_SECONDS = float(os.environ.get("SESSION_IDLE_SECONDS", "900"))

# Upstream writes output files into --output-dir; the cwd (/guardian) is not
# writable for the `node` user. Only screenshots still land there (see
# _FILE_ARGS), capped by --output-max-size.
_OUTPUT_DIR = "/tmp/playwright-mcp-output"
_OUTPUT_MAX_SIZE = 50 * 1024 * 1024

# Tool arguments that redirect a result into a file inside the container
# instead of the response. The calling model has no way to read container
# files -- a model that sets `filename` gets a dead link (or EACCES) instead
# of the content it asked for. Removed from every tool schema and from every
# tools/call, so results always come back inline.
_FILE_ARGS = ("filename",)

# Upstream writes new console messages to a log file and puts a link into
# every response ("- New console entries: ../tmp/.../console-*.log#L1-L8");
# there's no option to turn that off. Same dead end as _FILE_ARGS, so the
# link is replaced by the tool that returns those messages inline.
_CONSOLE_LINK = re.compile(r"(- New console entries: )\S+")

# --snapshot-mode none: page-changing tools (navigate, click, type, ...) no
# longer return a snapshot. Upstream would write it to a file and return
# only a link (see _FILE_ARGS for why that's useless here); with "none" the
# response is just the page header, and browser_snapshot returns the
# accessibility tree inline. The model needs to know that up front.
_SNAPSHOT_NOTE = (
    "\n\nSeitenwechsel und Aktionen (browser_navigate, browser_click, ...) liefern keinen "
    "Seiteninhalt. Den Inhalt und die Element-Referenzen danach mit browser_snapshot abrufen."
)

_INSTRUCTIONS = (
    "Dieser Browser-MCP kann ausschliesslich folgende Domains erreichen "
    f"(serverseitig erzwungen, alles andere schlaegt fehl): {_DOMAIN_LIST_HUMAN}. "
    "Plane Aufrufe entsprechend, statt andere Domains zu versuchen."
    + _SNAPSHOT_NOTE
    + (f"\n\n{_EXTRA_INSTRUCTIONS}" if _EXTRA_INSTRUCTIONS else "")
)

_TOOL_NOTE = f"\n\nErlaubte Domains (serverseitig erzwungen): {_DOMAIN_LIST_HUMAN}."


def _spawn_upstream() -> subprocess.Popen:
    args = [
        "node", "/app/cli.js",
        "--headless", "--browser", "chromium", "--no-sandbox",
        "--host", "127.0.0.1", "--port", str(INTERNAL_PORT),
        "--block-service-workers",
        "--isolated",
        "--snapshot-mode", "none",
        "--output-dir", _OUTPUT_DIR,
        "--output-max-size", str(_OUTPUT_MAX_SIZE),
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
    schema = tool.inputSchema or {}
    props = schema.get("properties", {})
    update: dict[str, Any] = {}
    if any(arg in props for arg in _FILE_ARGS):
        update["inputSchema"] = {
            **schema,
            "properties": {k: v for k, v in props.items() if k not in _FILE_ARGS},
            **({"required": [r for r in schema["required"] if r not in _FILE_ARGS]}
               if "required" in schema else {}),
        }
    if "url" in props:
        update["description"] = (tool.description or "") + _TOOL_NOTE
    return tool.model_copy(update=update) if update else tool


def _strip_file_args(arguments: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (arguments or {}).items() if k not in _FILE_ARGS}


def _replace_file_links(result: types.CallToolResult) -> types.CallToolResult:
    content = [
        c.model_copy(update={"text": _CONSOLE_LINK.sub(r"\1browser_console_messages", c.text)})
        if isinstance(c, types.TextContent) else c
        for c in result.content
    ]
    return result.model_copy(update={"content": content})


class _Slot:
    def __init__(self) -> None:
        self.session: ClientSession | None = None
        self.ready = anyio.Event()
        self.closed = anyio.Event()
        self.error: BaseException | None = None
        self.in_flight = 0
        self.last_used = anyio.current_time()


class _UpstreamPool:
    """One upstream MCP session per user key, opened on first use.

    Each session lives in its own task inside the pool's task group: the
    streamable-http client and ClientSession are async context managers that
    must be entered and exited in the same task, so a session can't simply
    be opened inside the request that first needs it (that request's task
    ends with the request). The task holds the session open until the slot
    is closed -- by idle timeout, by browser_close, or by a transport error.
    """

    def __init__(self, task_group: anyio.abc.TaskGroup) -> None:
        self._tg = task_group
        self._slots: dict[str, _Slot] = {}
        self._lock = anyio.Lock()

    async def _run(self, key: str, slot: _Slot) -> None:
        try:
            async with streamablehttp_client(_UPSTREAM_URL) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    slot.session = session
                    slot.ready.set()
                    await slot.closed.wait()
        except Exception as exc:  # noqa: BLE001 -- logged, surfaced to waiting callers
            slot.error = exc
            logger.exception("upstream session failed")
        finally:
            slot.session = None
            slot.ready.set()
            if self._slots.get(key) is slot:
                del self._slots[key]
            logger.info("upstream session closed, %d open", len(self._slots))

    async def _acquire(self, key: str) -> _Slot:
        async with self._lock:
            slot = self._slots.get(key)
            if slot is None or slot.closed.is_set():
                slot = _Slot()
                self._slots[key] = slot
                self._tg.start_soon(self._run, key, slot)
                logger.info("upstream session opened, %d open", len(self._slots))
            slot.in_flight += 1
        try:
            with anyio.fail_after(30):
                await slot.ready.wait()
        except BaseException:
            slot.in_flight -= 1
            raise
        if slot.session is None:
            slot.in_flight -= 1
            raise RuntimeError(f"upstream session unavailable: {slot.error!r}")
        return slot

    async def call_tool(self, key: str, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        slot = await self._acquire(key)
        try:
            result = await slot.session.call_tool(name, arguments)
        except McpError:
            raise
        except Exception:
            # Transport-level failure: drop the session, the next call opens a new one.
            slot.closed.set()
            raise
        finally:
            slot.in_flight -= 1
            slot.last_used = anyio.current_time()
        if name == "browser_close":
            # Upstream disposes the browser context on browser_close; a fresh
            # session gives the next call a fresh context instead of a dead one.
            slot.closed.set()
        return result

    async def reap_idle(self) -> None:
        while True:
            await anyio.sleep(min(60.0, _SESSION_IDLE_SECONDS))
            now = anyio.current_time()
            for slot in list(self._slots.values()):
                if slot.in_flight == 0 and now - slot.last_used > _SESSION_IDLE_SECONDS:
                    slot.closed.set()

    def close_all(self) -> None:
        for slot in list(self._slots.values()):
            slot.closed.set()


def _session_key(server: Server) -> str:
    request = server.request_context.request
    key = request.headers.get(_SESSION_HEADER, "") if request is not None else ""
    if not key:
        logger.warning("request without %s header -- using the shared fallback session", _SESSION_HEADER)
    return key or _FALLBACK_KEY


def build_server(tools: list[types.Tool], pool: _UpstreamPool) -> Server:
    server: Server = Server("playwright-mcp-guarded", instructions=_INSTRUCTIONS)
    annotated = [_annotate_tool(t) for t in tools]

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return annotated

    # validate_input=False: the SDK would validate against the advertised
    # schema, which no longer has `filename` -- a call that still sends it
    # must be stripped, not rejected. The SDK's validator also ignores zod
    # defaults and rejects calls upstream accepts (browser_take_screenshot
    # without `scale`). Upstream validates every call itself anyway.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result = await pool.call_tool(_session_key(server), name, _strip_file_args(arguments))
        return _replace_file_links(result)

    return server


async def _fetch_tools() -> list[types.Tool]:
    # The tool list is static for a given upstream configuration -- fetched
    # once at startup, so tools/list never opens a per-user session (and with
    # it a browser context) just to describe the tools.
    async with streamablehttp_client(_UPSTREAM_URL) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return (await session.list_tools()).tools


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
            tools = await _fetch_tools()
            async with anyio.create_task_group() as tg:
                pool = _UpstreamPool(tg)
                tg.start_soon(pool.reap_idle)
                server = build_server(tools, pool)
                session_manager = StreamableHTTPSessionManager(
                    app=server, json_response=False, stateless=True,
                )
                try:
                    async with session_manager.run():
                        endpoint._session_manager = session_manager
                        logger.info(
                            "playwright-mcp-guarded up, allowed origins=%s",
                            _DOMAIN_LIST_HUMAN,
                        )
                        yield
                finally:
                    endpoint._session_manager = None
                    pool.close_all()
                    tg.cancel_scope.cancel()
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
