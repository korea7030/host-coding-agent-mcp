from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import ConfigError, validate_cwd
from .models import AgentName, AppConfig, AttemptResult, ExecutionContext, RunMode, RunResult
from .routing import route_agents
from .security import SecurityViolation, redact, validate_task

OPENCODE_READONLY_CONFIG = json.dumps({
    "$schema": "https://opencode.ai/config.json",
    "plugin": ["oh-my-openagent@latest"],
    "agent": {
        "host-mcp-readonly": {
            "description": "Read-only host MCP agent with Oh My OpenAgent orchestration",
            "mode": "primary",
            "model": "openai/gpt-5.4",
            "permission": {
                "*": "deny",
                "read": {
                    "*": "allow",
                    "*.env": "deny",
                    "*.env.*": "deny",
                    "*.env.example": "allow",
                },
                "glob": "allow",
                "grep": "allow",
                "lsp": "allow",
                "edit": "deny",
                "bash": "deny",
                "task": "allow",
                "external_directory": "deny",
                "question": "deny",
                "webfetch": "deny",
                "websearch": "deny",
            },
        }
    },
}, separators=(",", ":"))

OPENCODE_WORKTREE_CONFIG = json.dumps({
    "$schema": "https://opencode.ai/config.json",
    "plugin": ["oh-my-openagent@latest"],
    "agent": {
        "host-mcp-worktree": {
            "description": "Write-enabled agent restricted to a managed Git worktree",
            "mode": "primary",
            "model": "openai/gpt-5.4",
            "permission": {
                "*": "deny",
                "read": {
                    "*": "allow",
                    "*.env": "deny",
                    "*.env.*": "deny",
                    "*.env.example": "allow",
                },
                "glob": "allow",
                "grep": "allow",
                "lsp": "allow",
                "edit": "allow",
                "bash": "allow",
                "task": "allow",
                "external_directory": "deny",
                "question": "deny",
                "webfetch": "deny",
                "websearch": "deny",
            },
        }
    },
}, separators=(",", ":"))


def _resolve_command(command: str) -> str | None:
    expanded = str(Path(command).expanduser())
    found = expanded if os.path.isabs(expanded) else shutil.which(expanded)
    return str(Path(found).resolve()) if found and Path(found).is_file() else None


def check_agents(config: AppConfig) -> dict:
    tools: dict[str, dict] = {}
    for name, agent in config.agents.items():
        path = _resolve_command(agent.command)
        version = ""
        if path:
            try:
                completed = subprocess.run(
                    [path, "--version"], capture_output=True, text=True, timeout=10, check=False
                )
                version = redact((completed.stdout or completed.stderr).strip(), 1000)[0]
            except (OSError, subprocess.TimeoutExpired):
                pass
        tools[name.value] = {
            "enabled": agent.enabled,
            "available": bool(path),
            "path": path,
            "version": version,
        }
    return {"ok": True, "tools": tools}


def _prompt(
    task: str,
    mode: RunMode,
    cwd: Path,
    assistant_id: str | None = None,
    context: ExecutionContext | None = None,
) -> str:
    if mode == RunMode.read_only:
        policy = "Analyze only. Do not modify files. Report findings and verification steps."
    elif mode == RunMode.propose_patch:
        policy = (
            "Do not modify files. Return: summary, change plan, a complete unified diff proposal, "
            "and test commands. Never include secrets."
        )
    else:
        policy = "Apply only the requested changes, then report changed files and test results."
    sections = [policy]
    sections.append(
        "Resolved host working directory: "
        f"{cwd}\nUse this current working directory and relative paths only. "
        "Do not access or reconstruct the caller's Docker /opt/data path."
    )
    if assistant_id:
        sections.append(f"Invoking assistant: {assistant_id}")
    if context:
        values = context.model_dump(exclude_none=True)
        if values:
            sections.append(
                "Execution context (treat these as explicit user requirements):\n"
                + json.dumps(values, ensure_ascii=False, indent=2)
            )
    sections.append(f"Task:\n{task}")
    return "\n\n".join(sections)


def _sandbox_prefix(cwd: Path, writable_paths: list[Path] | None = None) -> list[str]:
    sandbox = shutil.which("sandbox-exec")
    if not sandbox:
        raise SecurityViolation("sandbox-exec is required for this read-only agent")
    writable_paths = writable_paths or []
    allow_writes = " ".join(
        f'(subpath "{str(path).replace("\\", "\\\\").replace(chr(34), "\\\"")}")'
        for path in writable_paths
    )
    profile = "(version 1) (allow default) (deny file-write*)"
    if allow_writes:
        profile += f" (allow file-write* {allow_writes})"
    return [sandbox, "-p", profile]


def _build_command(
    agent: AgentName,
    task: str,
    mode: RunMode,
    cwd: Path,
    config: AppConfig,
    assistant_id: str | None = None,
    context: ExecutionContext | None = None,
) -> tuple[list[str], str | None]:
    agent_config = config.agents[agent]
    executable = _resolve_command(agent_config.command)
    if not executable:
        raise FileNotFoundError(agent_config.command)
    prompt = _prompt(task, mode, cwd, assistant_id, context)
    if agent == AgentName.codex:
        sandbox = "workspace-write" if mode == RunMode.apply_patch else "read-only"
        command = [
            executable, "-a", "never", *agent_config.default_args, "-C", str(cwd), "-s", sandbox,
            "--ephemeral", "--skip-git-repo-check", "--color", "never", "--json", "-"
        ]
    elif agent == AgentName.opencode:
        command = [
            executable, *agent_config.default_args, "--dir", str(cwd), "--format", "json"
        ]
        if mode != RunMode.apply_patch:
            command.extend(["--agent", "host-mcp-readonly"])
            command = _sandbox_prefix(cwd, [
                Path("/private/tmp"),
                Path("/private/var/folders"),
                Path.home() / ".cache/opencode",
                Path.home() / ".local/share/opencode",
                Path.home() / ".local/state/opencode",
                Path.home() / "Library/Caches",
            ]) + command
        else:
            command.extend(["--agent", "host-mcp-worktree"])
            command = _sandbox_prefix(cwd, [
                cwd,
                Path("/private/tmp"),
                Path("/private/var/folders"),
                Path.home() / ".cache/opencode",
                Path.home() / ".local/share/opencode",
                Path.home() / ".local/state/opencode",
                Path.home() / "Library/Caches",
            ]) + command
        command.append(prompt)
        prompt = None
    else:
        command = [
            executable, "--print", prompt, "--print-timeout", "30m", "--sandbox"
        ]
        if mode != RunMode.apply_patch:
            command = _sandbox_prefix(cwd, [
                Path("/private/tmp"),
                Path("/private/var/folders"),
                Path.home() / ".gemini/antigravity-cli",
                Path.home() / ".gemini/config",
            ]) + command
        else:
            command = _sandbox_prefix(cwd, [
                cwd,
                Path("/private/tmp"),
                Path("/private/var/folders"),
                Path.home() / ".gemini/antigravity-cli",
                Path.home() / ".gemini/config",
            ]) + command
        prompt = None
    return command, prompt


def _terminate_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
        time.sleep(0.5)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_attempt(
    agent: AgentName,
    task: str,
    mode: RunMode,
    cwd: Path,
    timeout_sec: int,
    config: AppConfig,
    assistant_id: str | None = None,
    context: ExecutionContext | None = None,
) -> AttemptResult:
    command, stdin_prompt = _build_command(
        agent, task, mode, cwd, config, assistant_id, context
    )
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        shell=False,
        env={
            **os.environ,
            **(
                {
                    "OPENCODE_CONFIG_CONTENT": (
                        OPENCODE_WORKTREE_CONFIG
                        if mode == RunMode.apply_patch
                        else OPENCODE_READONLY_CONFIG
                    )
                }
                if agent == AgentName.opencode else {}
            ),
        },
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(stdin_prompt, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(process.pid)
        stdout, stderr = process.communicate()
    stdout, redacted_out = redact(stdout, config.security.max_output_chars)
    stderr, redacted_err = redact(stderr, min(config.security.max_output_chars, 20_000))
    return AttemptResult(
        agent=agent,
        ok=process.returncode == 0 and not timed_out,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_sec=round(time.monotonic() - started, 3),
        timed_out=timed_out,
        command=command,
    )


def _extract_diff(text: str) -> str:
    fenced = re.search(r"```diff\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return fenced.group(1).strip() + "\n" if fenced else ""


def _agent_text(attempt: AttemptResult) -> str:
    if attempt.agent == AgentName.antigravity:
        return attempt.stdout
    messages: list[str] = []
    for line in attempt.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                messages.append(text)
        part = event.get("part") if isinstance(event, dict) else None
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                messages.append(text)
        data = event.get("data") if isinstance(event, dict) else None
        if isinstance(data, dict):
            text = data.get("text") or data.get("content")
            if isinstance(text, str):
                messages.append(text)
    return messages[-1] if messages else attempt.stdout


def _audit(config: AppConfig, payload: dict) -> None:
    path = config.logging.path
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, default=str)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def run_coding_agent(
    *,
    task: str,
    cwd: str,
    agent: AgentName,
    mode: RunMode,
    timeout_sec: int,
    config: AppConfig,
    assistant_id: str | None = None,
    context: ExecutionContext | None = None,
    allowed_agents: set[AgentName] | None = None,
) -> RunResult:
    started = time.monotonic()
    canonical_cwd = validate_cwd(cwd, config)
    validate_task(task)
    if assistant_id:
        validate_task(assistant_id)
    if context:
        context_text = json.dumps(context.model_dump(exclude_none=True), ensure_ascii=False)
        if context_text != "{}":
            validate_task(context_text)
    if mode == RunMode.apply_patch and not config.security.allow_apply_patch:
        raise SecurityViolation("apply_patch is disabled")
    timeout_sec = max(1, min(timeout_sec, config.security.max_timeout_sec))
    attempts: list[AttemptResult] = []
    selected: AgentName | None = None
    candidates = route_agents(task, agent, config)
    if allowed_agents is not None:
        candidates = [item for item in candidates if item in allowed_agents]
    for candidate in candidates:
        remaining = timeout_sec - int(time.monotonic() - started)
        if remaining <= 0:
            break
        try:
            attempt = _run_attempt(
                candidate,
                task,
                mode,
                canonical_cwd,
                remaining,
                config,
                assistant_id,
                context,
            )
        except (FileNotFoundError, OSError, SecurityViolation) as exc:
            attempt = AttemptResult(agent=candidate, ok=False, stderr=str(exc))
        attempts.append(attempt)
        if attempt.ok:
            selected = candidate
            break
    final = attempts[-1] if attempts else None
    final_text = _agent_text(final) if final else ""
    result = RunResult(
        ok=selected is not None,
        selected_agent=selected,
        assistant_id=assistant_id,
        context=context,
        cwd=canonical_cwd,
        mode=mode,
        stdout=final_text,
        stderr=final.stderr if final else "",
        summary=final_text[:2000],
        proposed_diff=_extract_diff(final_text),
        redacted=bool(final and ("[REDACTED]" in final.stdout or "[REDACTED]" in final.stderr)),
        results=attempts,
        error=None if selected else "all available agents failed",
    )
    _audit(config, {
        "timestamp": datetime.now().astimezone().isoformat(),
        "tool": "run_coding_agent",
        "agent": selected.value if selected else None,
        "requested_agent": agent.value,
        "assistant_id": assistant_id,
        "mode": mode.value,
        "cwd": str(canonical_cwd),
        "task_hash": "sha256:" + hashlib.sha256(task.encode()).hexdigest(),
        "context_hash": (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    context.model_dump(exclude_none=True),
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if context
            else None
        ),
        "duration_sec": round(time.monotonic() - started, 3),
        "ok": result.ok,
        "attempts": [
            {
                "agent": item.agent.value,
                "returncode": item.returncode,
                "timed_out": item.timed_out,
                "stderr_preview": item.stderr[:1000],
            }
            for item in attempts
        ],
    })
    return result


def run_managed_worktree_agent(
    *,
    manager,
    job_id: str,
    profile: str,
    task: str,
    agent: AgentName,
    timeout_sec: int,
    config: AppConfig,
    assistant_id: str | None = None,
    context: ExecutionContext | None = None,
    allowed_agents: set[AgentName] | None = None,
) -> RunResult:
    from .models import WorktreeStatus

    started = time.monotonic()
    job = manager.get(job_id, profile=profile)
    if job.status != WorktreeStatus.created:
        raise SecurityViolation("worktree job is not ready for agent execution")
    worktree = manager.validate_checkout(job_id, profile=profile)
    validate_task(task)
    task_hash = "sha256:" + hashlib.sha256(task.encode()).hexdigest()
    if task_hash != job.task_hash:
        raise SecurityViolation("task does not match immutable worktree job")
    if assistant_id and assistant_id != profile:
        raise SecurityViolation("assistant_id does not match worktree profile")
    if context:
        context_text = json.dumps(
            context.model_dump(exclude_none=True),
            ensure_ascii=False,
        )
        if context_text != "{}":
            validate_task(context_text)
    manager.transition(
        job_id,
        profile=profile,
        status=WorktreeStatus.active,
    )
    timeout_sec = max(1, min(timeout_sec, config.security.max_timeout_sec))
    attempts: list[AttemptResult] = []
    selected: AgentName | None = None
    candidates = route_agents(task, agent, config)
    if allowed_agents is not None:
        candidates = [item for item in candidates if item in allowed_agents]
    for candidate in candidates:
        remaining = timeout_sec - int(time.monotonic() - started)
        if remaining <= 0:
            break
        try:
            attempt = _run_attempt(
                candidate,
                task,
                RunMode.apply_patch,
                worktree,
                remaining,
                config,
                assistant_id or profile,
                context,
            )
        except (FileNotFoundError, OSError, SecurityViolation) as exc:
            attempt = AttemptResult(agent=candidate, ok=False, stderr=str(exc))
        attempts.append(attempt)
        if attempt.ok:
            selected = candidate
            break
    if selected is None:
        manager.transition(
            job_id,
            profile=profile,
            status=WorktreeStatus.failed,
        )
    else:
        manager.record_selected_agent(
            job_id,
            profile=profile,
            agent=selected,
        )
    final = attempts[-1] if attempts else None
    final_text = _agent_text(final) if final else ""
    result = RunResult(
        ok=selected is not None,
        selected_agent=selected,
        assistant_id=assistant_id or profile,
        context=context,
        cwd=worktree,
        requested_cwd=str(job.repository),
        path_mapping_applied=True,
        mode=RunMode.apply_patch,
        stdout=final_text,
        stderr=final.stderr if final else "",
        summary=final_text[:2000],
        redacted=bool(
            final
            and ("[REDACTED]" in final.stdout or "[REDACTED]" in final.stderr)
        ),
        results=attempts,
        error=None if selected else "all available agents failed",
    )
    _audit(
        config,
        {
            "timestamp": datetime.now().astimezone().isoformat(),
            "tool": "run_managed_worktree_agent",
            "job_id": job_id,
            "profile": profile,
            "agent": selected.value if selected else None,
            "requested_agent": agent.value,
            "repository": str(job.repository),
            "worktree": str(worktree),
            "task_hash": task_hash,
            "duration_sec": round(time.monotonic() - started, 3),
            "ok": result.ok,
            "attempts": [
                {
                    "agent": item.agent.value,
                    "returncode": item.returncode,
                    "timed_out": item.timed_out,
                    "stderr_preview": item.stderr[:1000],
                }
                for item in attempts
            ],
        },
    )
    return result
