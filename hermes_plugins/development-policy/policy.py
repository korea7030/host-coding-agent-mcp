from __future__ import annotations

from typing import Any

ALLOWED_DEVELOPMENT_MCP_TOOLS = frozenset(
    {
        "mcp_host_coding_agent_run_coding_agent",
        "mcp_host_coding_agent_run_antigravity",
        "mcp_host_coding_agent_run_codex",
        "mcp_host_coding_agent_run_opencode",
    }
)

BLOCKED_NATIVE_TOOLS = frozenset(
    {
        "terminal",
        "execute_code",
        "write_file",
        "patch",
        "delegate_task",
    }
)

ROUTING_CONTEXT = """Development execution policy:
- All code analysis, generation, modification, testing, refactoring, and deployment preparation for host projects MUST use the host-coding-agent MCP tools.
- Default to mcp_host_coding_agent_run_coding_agent with agent="auto" and mode="propose_patch".
- Never use terminal, execute_code, write_file, patch, delegate_task, or a directly launched coding-agent CLI for development.
- If host-coding-agent MCP fails, report the failure. Do not fall back to a native development tool.
- Coding agents are read-only and may only return findings or a proposed diff until a separate human approval is verified."""


def normalize_tool_name(tool_name: str) -> str:
    return tool_name.strip().casefold().replace("-", "_")


def block_message(tool_name: str, args: Any = None) -> str | None:
    del args
    normalized = normalize_tool_name(tool_name)
    if normalized in ALLOWED_DEVELOPMENT_MCP_TOOLS:
        return None
    if normalized in BLOCKED_NATIVE_TOOLS:
        return (
            f"Development policy blocked native tool '{tool_name}'. "
            "Use the host-coding-agent MCP server. If MCP fails, report the "
            "failure without falling back to native execution."
        )
    return None


def on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **_: Any,
) -> dict[str, str] | None:
    message = block_message(tool_name, args)
    if message is None:
        return None
    return {"action": "block", "message": message}


def on_pre_llm_call(**_: Any) -> dict[str, str]:
    return {"context": ROUTING_CONTEXT}
