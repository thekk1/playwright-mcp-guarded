import importlib
import os

import pytest
from mcp import types


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def app_module(monkeypatch):
    monkeypatch.setenv(
        "ALLOWED_ORIGINS", "github.com;raw.githubusercontent.com;wikipedia.org"
    )
    from guardian import app as module

    importlib.reload(module)
    yield module
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    importlib.reload(module)


def _tool(name: str, description: str, properties: dict) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": properties},
    )


def test_tool_with_url_param_gets_domain_note_appended(app_module):
    tool = _tool("browser_navigate", "Navigate to a URL", {"url": {"type": "string"}})
    annotated = app_module._annotate_tool(tool)
    assert "github.com" in annotated.description
    assert "raw.githubusercontent.com" in annotated.description
    assert "wikipedia.org" in annotated.description
    assert annotated.description.startswith("Navigate to a URL")
    assert "browser_snapshot" in annotated.description


def test_tool_without_url_param_is_untouched(app_module):
    tool = _tool("browser_snapshot", "Capture accessibility snapshot", {})
    annotated = app_module._annotate_tool(tool)
    assert annotated.description == "Capture accessibility snapshot"
    assert annotated is tool


def test_filename_param_removed_from_schema(app_module):
    tool = types.Tool(
        name="browser_snapshot",
        description="Capture accessibility snapshot",
        inputSchema={
            "type": "object",
            "properties": {"filename": {"type": "string"}, "depth": {"type": "number"}},
            "required": ["filename"],
        },
    )
    annotated = app_module._annotate_tool(tool)
    assert "filename" not in annotated.inputSchema["properties"]
    assert "depth" in annotated.inputSchema["properties"]
    assert annotated.inputSchema["required"] == []
    assert annotated.description == "Capture accessibility snapshot"
    assert "filename" in tool.inputSchema["properties"]


def test_filename_argument_stripped_from_calls(app_module):
    assert app_module._strip_file_args({"filename": "x.md", "depth": 3}) == {"depth": 3}
    assert app_module._strip_file_args(None) == {}


def test_console_log_link_replaced_by_tool_name(app_module):
    result = types.CallToolResult(content=[types.TextContent(
        type="text",
        text="### Events\n- New console entries: ../tmp/playwright-mcp-output/console-1.log#L1-L8",
    )])
    text = app_module._replace_file_links(result).content[0].text
    assert text == "### Events\n- New console entries: browser_console_messages"


def test_instructions_list_all_configured_domains(app_module):
    for domain in ("github.com", "raw.githubusercontent.com", "wikipedia.org"):
        assert domain in app_module._INSTRUCTIONS


def test_extra_instructions_appended_when_set(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "github.com")
    monkeypatch.setenv("EXTRA_INSTRUCTIONS", "Use ?foo=bar for the frobnicator.")
    from guardian import app as module

    importlib.reload(module)
    try:
        assert "Use ?foo=bar for the frobnicator." in module._INSTRUCTIONS
        assert "github.com" in module._INSTRUCTIONS
    finally:
        monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
        monkeypatch.delenv("EXTRA_INSTRUCTIONS", raising=False)
        importlib.reload(module)


def test_extra_instructions_absent_when_unset(app_module):
    assert app_module._INSTRUCTIONS.endswith(app_module._SNAPSHOT_NOTE)


def test_instructions_tell_model_to_fetch_content_with_snapshot(app_module):
    assert "browser_snapshot" in app_module._INSTRUCTIONS


def test_empty_allowlist_says_so_instead_of_silently_allowing(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "")
    from guardian import app as module

    importlib.reload(module)
    try:
        assert "keine" in module._DOMAIN_LIST_HUMAN
    finally:
        monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
        importlib.reload(module)


def test_spawn_args_pass_through_same_allowlist_used_for_visibility(app_module):
    args = None

    class _FakePopen:
        def __init__(self, cmd):
            nonlocal args
            args = cmd

    import subprocess as subprocess_module

    orig = subprocess_module.Popen
    subprocess_module.Popen = _FakePopen
    try:
        app_module._spawn_upstream()
    finally:
        subprocess_module.Popen = orig

    assert "--allowed-origins" in args
    idx = args.index("--allowed-origins")
    assert args[idx + 1] == "github.com;raw.githubusercontent.com;wikipedia.org"
    assert "--block-service-workers" in args
    assert "--isolated" in args
    assert args[args.index("--snapshot-mode") + 1] == "none"
    assert args[args.index("--output-dir") + 1] == app_module._OUTPUT_DIR


class _FakeSession:
    def __init__(self) -> None:
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return name


@pytest.mark.anyio
async def test_pool_keeps_one_session_per_user_and_closes_idle(app_module, monkeypatch):
    import anyio

    opened = []

    async def fake_run(self, key, slot):
        slot.session = _FakeSession()
        opened.append((key, slot))
        slot.ready.set()
        await slot.closed.wait()
        if self._slots.get(key) is slot:
            del self._slots[key]

    monkeypatch.setattr(app_module._UpstreamPool, "_run", fake_run)
    monkeypatch.setattr(app_module, "_SESSION_IDLE_SECONDS", 0.05)
    async with anyio.create_task_group() as tg:
        pool = app_module._UpstreamPool(tg)
        await pool.call_tool("a", "browser_navigate", {})
        await pool.call_tool("a", "browser_snapshot", {})
        await pool.call_tool("b", "browser_navigate", {})
        assert [k for k, _ in opened] == ["a", "b"]
        assert len(opened[0][1].session.calls) == 2

        await pool.call_tool("a", "browser_close", {})
        await pool.call_tool("a", "browser_navigate", {})
        assert [k for k, _ in opened] == ["a", "b", "a"]

        tg.start_soon(pool.reap_idle)
        await anyio.sleep(0.2)
        assert pool._slots == {}
        tg.cancel_scope.cancel()


def test_unsafe_code_tool_hidden_and_refused(app_module):
    import anyio

    tools = [
        _tool("browser_run_code_unsafe", "Run code", {"code": {"type": "string"}}),
        _tool("browser_snapshot", "Snapshot", {}),
    ]

    class _NoPool:
        async def call_tool(self, *args):
            raise AssertionError("blocked tool reached upstream")

    server = app_module.build_server(tools, _NoPool())

    async def run():
        listed = await server.request_handlers[types.ListToolsRequest](
            types.ListToolsRequest(method="tools/list")
        )
        names = [t.name for t in listed.root.tools]
        called = await server.request_handlers[types.CallToolRequest](
            types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(name="browser_run_code_unsafe", arguments={"code": "1"}),
            )
        )
        return names, called.root

    names, called = anyio.run(run)
    assert names == ["browser_snapshot"]
    assert called.isError
