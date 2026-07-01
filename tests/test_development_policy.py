from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

PLUGIN_DIR = (
    Path(__file__).resolve().parents[1]
    / "hermes_plugins"
    / "development-policy"
)


def _load_module(filename: str, name: str, *, package: bool = False):
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_DIR / filename,
        submodule_search_locations=[str(PLUGIN_DIR)] if package else None,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy = _load_module("policy.py", "development_policy_policy")


def test_blocks_native_development_tools():
    for tool_name in (
        "terminal",
        "execute_code",
        "write_file",
        "patch",
        "delegate_task",
    ):
        result = policy.on_pre_tool_call(tool_name=tool_name, args={})
        assert result is not None
        assert result["action"] == "block"
        assert "host-coding-agent" in result["message"]


def test_allows_host_coding_agent_mcp_tools():
    for tool_name in policy.ALLOWED_DEVELOPMENT_MCP_TOOLS:
        assert policy.on_pre_tool_call(tool_name=tool_name, args={}) is None


def test_allows_read_only_and_non_development_tools():
    for tool_name in ("read_file", "search_files", "web_search", "memory"):
        assert policy.on_pre_tool_call(tool_name=tool_name, args={}) is None


def test_tool_name_normalization_prevents_hyphen_bypass():
    result = policy.on_pre_tool_call(tool_name="execute-code", args={})
    assert result is not None
    assert result["action"] == "block"


def test_routing_context_is_fail_closed():
    result = policy.on_pre_llm_call()
    context = result["context"]
    assert "MUST use" in context
    assert "Do not fall back" in context
    assert "mcp_host_coding_agent_run_coding_agent" in context
    assert "proposal_sha256" in context


def test_plugin_registers_hooks_and_approval_commands():
    plugin = _load_module(
        "__init__.py",
        "development_policy_plugin",
        package=True,
    )
    registered = {}
    commands = {}

    class FakeContext:
        def register_hook(self, name, callback):
            registered[name] = callback

        def register_command(self, name, handler, **kwargs):
            commands[name] = (handler, kwargs)

    plugin.register(FakeContext())

    assert registered["pre_llm_call"] is plugin.on_pre_llm_call
    assert registered["pre_tool_call"] is plugin.on_pre_tool_call
    assert registered["pre_gateway_dispatch"] is plugin.on_pre_gateway_dispatch
    assert set(commands) == {"proposal", "apply-proposal", "reject"}


def test_gateway_hook_captures_telegram_identity_for_approval(monkeypatch):
    event = SimpleNamespace(
        text="/apply-proposal proposal-id sha256:value",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="telegram"),
            user_id="123",
        ),
    )
    policy.on_pre_gateway_dispatch(event=event)
    monkeypatch.setenv("MCP_HOST_CODING_AGENT_API_KEY", "x" * 32)
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return b'{"ok": true, "approval": {"proposal_id": "proposal-id", "status": "applied"}, "changed_files": ["app.py"]}'

    def fake_urlopen(request, timeout):
        captured["body"] = request.data
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(policy.urllib.request, "urlopen", fake_urlopen)
    result = policy._approval_request(
        "approve",
        "proposal-id sha256:value",
    )

    assert result["ok"]
    assert b'"telegram_user_id": "123"' in captured["body"]
    assert captured["timeout"] == 60
