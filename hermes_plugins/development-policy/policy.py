from __future__ import annotations

import asyncio
import contextvars
import json
import os
import urllib.error
import urllib.request
import time
from pathlib import Path
from typing import Any

ALLOWED_DEVELOPMENT_MCP_TOOLS = frozenset(
    {
        "mcp_host_coding_agent_run_coding_agent",
        "mcp_host_coding_agent_run_development_task",
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
- Classify the request before routing. Host code development and project dependency changes belong to host-coding-agent. Runtime/profile operations do not.
- NEVER send OAuth, login, token refresh, account connection, Hermes skill installation, MCP registration/configuration, or Playwright/Chromium runtime installation to host-coding-agent.
- Route authentication to the target MCP or skill. Route skill and MCP lifecycle operations to Hermes profile management. Install runtime dependencies in the environment where that MCP actually executes.
- A host-coding-agent response with error_code="non_development_task" is final and non-retryable. Do not rephrase or split the same request into another coding-agent call.
- Standard development flow is check_host_coding_agents -> check_execution_health -> start_development_task(agent=<explicit selected agent>) -> get_async_job_events -> get_async_job.
- For interactive requests, present selectable_agents and pass the user's explicit choice such as opencode, codex, or antigravity. Do not silently default to auto; auto is only for existing automation compatibility.
- If check_execution_health returns ok=false, report recommended_next_action and do not start development.
- Direct mode does not require Git and modifies the authenticated workspace immediately. Do not inspect for .git or require a repository before calling it. Use direct_write_policy=fail_if_changed for read-only intent.
- Use isolation_mode="worktree" only when the user explicitly requests isolation, report-only review, approval, commit, or PR delivery.
- Pass the current container workspace path as cwd (normally /opt/data/profiles/<profile>/workspace or a child). The MCP maps that authenticated profile path to its host workspace. Do not pass /opt/data itself.
- The result cwd is the resolved macOS host path by design. Use requested_cwd and path_mapping_applied to verify the translation; do not treat a /Users/... result cwd as a mapping failure.
- Keep each request narrowly scoped. Split large project analysis into multiple calls to avoid oversized tool results.
- Never use terminal, execute_code, write_file, patch, delegate_task, or a directly launched coding-agent CLI for development.
- If host-coding-agent MCP fails, report the failure. Do not fall back to a native development tool. If the failure is ClosedResourceError or another HTTP stream/client error, check /healthz or /readyz; if ok=true, report a stream reconnect/client-state issue.
- Direct mode coding agents may modify the mapped workspace. When worktree manual mode returns a proposal_id, show the proposal_id and proposal_sha256 to the user.
- Worktree report delivery creates an immutable proposal for review but does not create approval, commit, PR, or modify the original workspace.
- Only the external Telegram /apply_proposal command may apply a manual proposal. Do not claim that a patch was applied unless that command returns status=applied."""

_telegram_command_context: contextvars.ContextVar[tuple[str, str] | None] = (
    contextvars.ContextVar("development_policy_telegram_command", default=None)
)
_last_runtime_registration = 0.0


def _read_env_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix):].strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        return value
    return ""


def host_coding_agent_token() -> str:
    token = os.environ.get("MCP_HOST_CODING_AGENT_API_KEY", "")
    if token:
        return token
    hermes_home = Path(
        os.environ.get("HERMES_HOME")
        or os.environ.get("HOME", "")
        or "/opt/data"
    )
    candidates = []
    for env_file in sorted((hermes_home / "profiles").glob("*/.env")):
        value = _read_env_value(env_file, "MCP_HOST_CODING_AGENT_API_KEY")
        if value:
            candidates.append(value)
    return candidates[0] if len(candidates) == 1 else ""


def register_runtime(*, force: bool = False) -> None:
    global _last_runtime_registration
    if not Path("/.dockerenv").exists():
        return
    now = time.monotonic()
    if not force and now - _last_runtime_registration < 20:
        return
    token = host_coding_agent_token()
    if not token:
        return
    try:
        container_id = Path("/etc/hostname").read_text().strip()
        request = urllib.request.Request(
            "http://host.docker.internal:8787/runtime/register",
            data=json.dumps(
                {"runtime": "docker", "container_id": container_id}
            ).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read())
        if result.get("ok"):
            _last_runtime_registration = now
    except Exception:
        # MCP execution will return an explicit registration error if this
        # best-effort gateway registration could not complete.
        return


def on_pre_gateway_dispatch(event: Any = None, **_: Any) -> None:
    register_runtime()
    text = str(getattr(event, "text", "") or "").strip()
    command = (
        text.split(maxsplit=1)[0]
        .split("@", 1)[0]
        .casefold()
        .replace("_", "-")
    )
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None)
    user_id = getattr(source, "user_id", None)
    if (
        command in {"/proposal", "/apply-proposal", "/reject"}
        and platform == "telegram"
        and user_id
    ):
        _telegram_command_context.set((str(user_id), command.lstrip("/")))


def _approval_request(action: str, raw_args: str) -> dict[str, Any]:
    identity = _telegram_command_context.get()
    if identity is None:
        raise ValueError("Telegram command identity is unavailable")
    user_id, captured_action = identity
    expected_command = {
        "show": "proposal",
        "approve": "apply-proposal",
    }.get(action, action)
    if captured_action != expected_command:
        raise ValueError("Telegram command context mismatch")
    parts = raw_args.strip().split()
    required = 1 if action == "show" else 2
    if len(parts) != required:
        suffix = " <proposal_sha256>" if required == 2 else ""
        telegram_command = expected_command.replace("-", "_")
        raise ValueError(f"Usage: /{telegram_command} <proposal_id>{suffix}")
    payload = {
        "action": action,
        "proposal_id": parts[0],
        "telegram_user_id": user_id,
    }
    if required == 2:
        payload["proposal_sha256"] = parts[1]
    token = host_coding_agent_token()
    if not token:
        raise ValueError("MCP approval credential is unavailable")
    request = urllib.request.Request(
        "http://host.docker.internal:8787/approval/telegram",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", str(exc))
        except Exception:
            detail = str(exc)
        raise ValueError(detail) from exc


def _format_approval_result(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"Approval failed: {result.get('error', 'unknown error')}"
    proposal = result.get("proposal")
    approval = result.get("approval", {})
    if isinstance(proposal, dict):
        diff = str(proposal.get("diff_text", ""))
        if len(diff) > 3000:
            diff = diff[:3000] + "\n... (truncated; verify by proposal hash)"
        return (
            f"Proposal: {proposal['proposal_id']}\n"
            f"SHA-256: {proposal['diff_sha256']}\n"
            f"Status: {approval['status']}\n"
            f"Workspace: {proposal['cwd']}\n\n{diff}"
        )
    if approval.get("status") == "applied":
        files = ", ".join(result.get("changed_files", []))
        return f"Patch applied: {approval['proposal_id']}\nChanged files: {files}"
    return (
        f"Proposal {approval.get('proposal_id', '')}: "
        f"{approval.get('status', 'updated')}"
    )


async def handle_proposal(raw_args: str) -> str:
    try:
        return _format_approval_result(
            await asyncio.to_thread(_approval_request, "show", raw_args)
        )
    except Exception as exc:
        return f"Proposal command failed: {exc}"


async def handle_approve(raw_args: str) -> str:
    try:
        return _format_approval_result(
            await asyncio.to_thread(_approval_request, "approve", raw_args)
        )
    except Exception as exc:
        return f"Apply proposal command failed: {exc}"


async def handle_reject(raw_args: str) -> str:
    try:
        return _format_approval_result(
            await asyncio.to_thread(_approval_request, "reject", raw_args)
        )
    except Exception as exc:
        return f"Reject proposal command failed: {exc}"


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
