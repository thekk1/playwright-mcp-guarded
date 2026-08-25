import importlib
import os

import pytest
from mcp import types


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


def test_tool_without_url_param_is_untouched(app_module):
    tool = _tool("browser_snapshot", "Capture accessibility snapshot", {})
    annotated = app_module._annotate_tool(tool)
    assert annotated.description == "Capture accessibility snapshot"
    assert annotated is tool


def test_instructions_list_all_configured_domains(app_module):
    for domain in ("github.com", "raw.githubusercontent.com", "wikipedia.org"):
        assert domain in app_module._INSTRUCTIONS


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
