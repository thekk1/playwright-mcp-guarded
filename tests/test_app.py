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
    assert "browser_read" in annotated.description


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
    assert app_module._INSTRUCTIONS.endswith(app_module._USAGE_NOTE)


def test_instructions_tell_model_to_read_with_browser_read(app_module):
    assert "browser_read" in app_module._INSTRUCTIONS


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
    assert names == ["browser_read", "browser_snapshot"]
    assert called.isError


def _result(text: str, is_error: bool = False):
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=is_error)


class _ScriptedPool:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call_tool(self, key, name, arguments):
        self.calls.append((key, name, arguments))
        return self.responses[name]


def test_browser_read_navigates_and_returns_text_in_sections(app_module, monkeypatch):
    import anyio
    import json

    monkeypatch.setattr(app_module, "_READ_CHUNK", 10)
    page = {"url": "https://de.wikipedia.org/wiki/X", "title": "X", "text": "abcdefghij\n\n\n\nklmno  \n"}
    pool = _ScriptedPool({
        "browser_navigate": _result("### Ran Playwright code\n```js\n```\n### Page\n- Page URL: u\n- HTTP status: 404\n### Events\n- x"),
        "browser_evaluate": _result("### Result\n" + json.dumps(page, indent=2) + "\n### Ran Playwright code\n```js\n```"),
    })
    out = anyio.run(app_module._read_page, pool, "k", {"url": "https://de.wikipedia.org/wiki/X"})
    text = out.content[0].text
    assert [c[1] for c in pool.calls] == ["browser_navigate", "browser_evaluate"]
    assert "- HTTP status: 404" in text
    assert "### Text (Zeichen 0-10 von 17)\nabcdefghij\n" in text
    assert "offset=10" in text

    out = anyio.run(app_module._read_page, pool, "k", {"offset": 10})
    text = out.content[0].text
    assert pool.calls[-1][1] == "browser_evaluate" and pool.calls[-2][1] == "browser_evaluate"
    assert text.endswith("### Text (Zeichen 10-17 von 17)\n\n\nklmno")
    assert "Weiterlesen" not in text

    out = anyio.run(app_module._read_page, pool, "k", {"offset": 17})
    assert out.content[0].text.endswith("Kein weiterer Text: die Seite hat 17 Zeichen.")


def test_browser_read_passes_navigation_errors_through(app_module):
    import anyio

    pool = _ScriptedPool({"browser_navigate": _result("### Error\nnet::ERR_BLOCKED_BY_CLIENT", is_error=True)})
    out = anyio.run(app_module._read_page, pool, "k", {"url": "https://example.com/"})
    assert out.isError
    assert [c[1] for c in pool.calls] == ["browser_navigate"]


def test_browser_read_listed_with_domain_note(app_module):
    server = app_module.build_server([], _ScriptedPool({}))
    import anyio

    async def run():
        listed = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest(method="tools/list"))
        return listed.root.tools

    tools = anyio.run(run)
    assert tools[0].name == "browser_read"
    assert "github.com" in tools[0].description
