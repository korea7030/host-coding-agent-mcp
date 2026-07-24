from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import ConfigError, validate_profile_cwd
from .models import AppConfig, IsolationMode
from .runner import check_agents


def _check(ok: bool, **details: Any) -> dict[str, Any]:
    return {"ok": ok, **details}


def check_sandbox_exec(cwd: Path) -> dict[str, Any]:
    sandbox = shutil.which("sandbox-exec")
    if sandbox is None:
        return _check(
            False,
            category="sandbox_unavailable",
            error="sandbox-exec is not available on this host",
            bypass_available=False,
        )
    true_cmd = shutil.which("true") or "/usr/bin/true"
    try:
        completed = subprocess.run(
            [
                sandbox,
                "-p",
                "(version 1) (allow default)",
                true_cmd,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check(
            False,
            category="sandbox_probe_failed",
            error=str(exc),
            command=sandbox,
            bypass_available=False,
        )
    if completed.returncode != 0:
        return _check(
            False,
            category="sandbox_apply_failed",
            error=(completed.stderr or completed.stdout).strip(),
            command=sandbox,
            exit_code=completed.returncode,
            bypass_available=False,
        )
    return _check(True, command=sandbox)


def check_worktree_available(cwd: Path) -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        return _check(False, reason="git is not available")
    try:
        completed = subprocess.run(
            [git, "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check(False, reason=str(exc))
    if completed.returncode != 0 or completed.stdout.strip() != "true":
        return _check(False, reason="cwd is not inside a Git worktree")
    return _check(True)


def check_execution_health(
    *,
    config: AppConfig,
    profile_name: str,
    runtime_registry,
    cwd: str | None = None,
    isolation_mode: IsolationMode | None = None,
) -> dict[str, Any]:
    profile = config.profiles.get(profile_name)
    if profile is None:
        raise ConfigError("unknown authenticated profile")

    requested_cwd = cwd or (
        str(profile.default_cwd)
        if profile.default_cwd is not None
        else None
    )
    checks: dict[str, dict[str, Any]] = {
        "auth": _check(True, profile=profile_name),
    }
    if requested_cwd is None:
        checks["cwd_mapping"] = _check(
            False,
            error="cwd is required because this profile has no default_cwd",
        )
        return _health_response(
            profile_name=profile_name,
            requested_cwd=None,
            resolved_cwd=None,
            path_mapping_applied=False,
            checks=checks,
        )

    raw_cwd = Path(requested_cwd).expanduser()
    normalized_cwd = Path(os.path.normpath(raw_cwd)) if raw_cwd.is_absolute() else raw_cwd
    is_container_cwd = raw_cwd.is_absolute() and any(
        normalized_cwd == root or normalized_cwd.is_relative_to(root)
        for root in profile.allowed_container_roots
    )
    runtime_status = runtime_registry.status(profile_name=profile_name)
    checks["runtime_registration"] = _check(
        (not is_container_cwd) or bool(runtime_status["registered"]),
        **runtime_status,
        required=is_container_cwd,
    )

    resolved_cwd: Path | None = None
    try:
        resolved_cwd = validate_profile_cwd(
            requested_cwd,
            profile_name,
            config,
            runtime_registry=runtime_registry,
        )
        checks["cwd_mapping"] = _check(True)
        checks["allowed_roots"] = _check(True)
    except ConfigError as exc:
        checks["cwd_mapping"] = _check(False, error=str(exc))
        checks["allowed_roots"] = _check(False, error=str(exc))

    agent_details = check_agents(
        config,
        allowed_agents=set(profile.allowed_agents),
    )
    checks["agent_cli"] = _check(
        bool(agent_details["selectable_agents"]),
        selectable_agents=agent_details["selectable_agents"],
        tools=agent_details["tools"],
    )

    requested_isolation = isolation_mode or profile.default_isolation_mode
    checks["isolation_mode"] = _check(
        requested_isolation in profile.allowed_isolation_modes,
        requested=requested_isolation.value,
        allowed=[mode.value for mode in profile.allowed_isolation_modes],
    )

    if resolved_cwd is not None:
        checks["sandbox"] = check_sandbox_exec(resolved_cwd)
        checks["direct_smoke"] = _check(
            IsolationMode.direct in profile.allowed_isolation_modes,
            reason=(
                None
                if IsolationMode.direct in profile.allowed_isolation_modes
                else "direct isolation is not allowed for this profile"
            ),
        )
        checks["worktree_available"] = (
            check_worktree_available(resolved_cwd)
            if IsolationMode.worktree in profile.allowed_isolation_modes
            else _check(False, reason="worktree isolation is not allowed for this profile")
        )
    else:
        checks["sandbox"] = _check(False, error="cwd could not be resolved")
        checks["direct_smoke"] = _check(False, error="cwd could not be resolved")
        checks["worktree_available"] = _check(False, error="cwd could not be resolved")

    return _health_response(
        profile_name=profile_name,
        requested_cwd=requested_cwd,
        resolved_cwd=str(resolved_cwd) if resolved_cwd is not None else None,
        path_mapping_applied=(
            resolved_cwd is not None
            and Path(requested_cwd) != resolved_cwd
        ),
        checks=checks,
    )


def _health_response(
    *,
    profile_name: str,
    requested_cwd: str | None,
    resolved_cwd: str | None,
    path_mapping_applied: bool,
    checks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    failed = {
        name: details
        for name, details in checks.items()
        if not details.get("ok")
    }
    return {
        "ok": not failed,
        "profile": profile_name,
        "requested_cwd": requested_cwd,
        "resolved_cwd": resolved_cwd,
        "path_mapping_applied": path_mapping_applied,
        "path_mapping_note": (
            "Resolved host cwd is expected when the caller passes a Docker container path."
            if path_mapping_applied
            else None
        ),
        "checks": checks,
        "recommended_next_action": _recommended_next_action(failed),
    }


def compact_execution_health(health: dict[str, Any]) -> dict[str, Any]:
    checks = health.get("checks", {})
    selected = {
        name: checks.get(name, {"ok": False, "error": "check missing"})
        for name in (
            "runtime_registration",
            "cwd_mapping",
            "allowed_roots",
            "sandbox",
            "direct_smoke",
            "worktree_available",
        )
    }
    failed = [
        name
        for name, details in selected.items()
        if isinstance(details, dict) and not details.get("ok")
    ]
    return {
        "ok": health.get("ok", False),
        "requested_cwd": health.get("requested_cwd"),
        "resolved_cwd": health.get("resolved_cwd"),
        "path_mapping_applied": health.get("path_mapping_applied", False),
        "checks": selected,
        "failed_checks": failed,
        "recommended_next_action": health.get("recommended_next_action"),
    }


def _recommended_next_action(failed: dict[str, dict[str, Any]]) -> str:
    if not failed:
        return "Execution health checks passed."
    if "runtime_registration" in failed:
        return (
            "Register the Hermes Docker runtime through /runtime/register, then retry. "
            "Ensure the development-policy plugin can read MCP_HOST_CODING_AGENT_API_KEY."
        )
    if "cwd_mapping" in failed or "allowed_roots" in failed:
        return "Check the requested cwd, profile allowed roots, and Docker path mapping."
    if "sandbox" in failed:
        return (
            "sandbox-exec is unavailable or failed on this host. Fix host sandbox "
            "permissions or use an explicitly approved bypass policy."
        )
    if "agent_cli" in failed:
        return "Install or enable at least one allowed coding agent CLI for this profile."
    if "worktree_available" in failed:
        return "Use direct isolation or run worktree mode from a Git repository."
    return "Inspect failed checks for the next action."
