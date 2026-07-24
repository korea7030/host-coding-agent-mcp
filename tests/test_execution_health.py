from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastmcp.server.auth import AccessToken

import server
from host_coding_agent.health import check_execution_health
from host_coding_agent.models import (
    AgentName,
    IsolationMode,
    ProfileConfig,
    RunMode,
)
from host_coding_agent.runtime import RuntimeRegistry


def _access_token(profile: str = "invest-bot") -> AccessToken:
    return AccessToken(
        token="x" * 32,
        client_id=profile,
        subject=profile,
        scopes=["host-coding-agent"],
        claims={"profile": profile},
    )


def _profile_config(workspace: Path | None = None) -> ProfileConfig:
    return ProfileConfig(
        token_env="TEST_TOKEN",
        allowed_roots=[workspace] if workspace is not None else [],
        allowed_container_roots=[
            Path("/opt/data/profiles/invest-bot/workspace")
        ],
        runtime_labels={"com.docker.compose.service": "hermes-invest"},
        allowed_agents=[AgentName.codex],
        allowed_modes=[RunMode.propose_patch],
        allowed_isolation_modes=[IsolationMode.direct, IsolationMode.worktree],
        default_isolation_mode=IsolationMode.direct,
        default_cwd=Path("/opt/data/profiles/invest-bot/workspace"),
        default_agent=AgentName.codex,
    )


def _register_runtime(config, tmp_path: Path, monkeypatch) -> RuntimeRegistry:
    mount_source = tmp_path / ".hermes-invest"
    workspace = mount_source / "profiles" / "invest-bot" / "workspace"
    workspace.mkdir(parents=True)
    config.auth.enabled = True
    config.profiles["invest-bot"] = _profile_config()
    container_id = "a" * 64
    inspect_data = [
        {
            "Id": container_id,
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "hermes-invest",
                }
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(mount_source),
                    "Destination": "/opt/data",
                    "RW": True,
                }
            ],
        }
    ]
    monkeypatch.setattr("host_coding_agent.runtime.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        "host_coding_agent.runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps(inspect_data),
            stderr="",
        ),
    )
    registry = RuntimeRegistry(config)
    registry.register_docker(
        profile_name="invest-bot",
        container_id=container_id,
    )
    return registry


def test_execution_health_reports_container_mapping(
    config,
    tmp_path,
    monkeypatch,
):
    registry = _register_runtime(config, tmp_path, monkeypatch)
    monkeypatch.setattr(
        "host_coding_agent.health.check_sandbox_exec",
        lambda cwd: {"ok": True, "command": "sandbox-exec"},
    )

    result = check_execution_health(
        config=config,
        profile_name="invest-bot",
        runtime_registry=registry,
    )

    assert result["profile"] == "invest-bot"
    assert result["requested_cwd"] == "/opt/data/profiles/invest-bot/workspace"
    assert result["resolved_cwd"].endswith(
        "/.hermes-invest/profiles/invest-bot/workspace"
    )
    assert result["path_mapping_applied"] is True
    assert result["checks"]["runtime_registration"]["ok"] is True
    assert result["checks"]["cwd_mapping"]["ok"] is True
    assert result["checks"]["allowed_roots"]["ok"] is True
    assert result["checks"]["agent_cli"]["ok"] is True


def test_execution_health_reports_unregistered_runtime(
    config,
):
    config.auth.enabled = True
    config.profiles["invest-bot"] = _profile_config()
    registry = RuntimeRegistry(config)

    result = check_execution_health(
        config=config,
        profile_name="invest-bot",
        runtime_registry=registry,
    )

    assert result["ok"] is False
    assert result["checks"]["runtime_registration"]["ok"] is False
    assert result["checks"]["runtime_registration"]["registered_profiles"] == []
    assert "Register the Hermes Docker runtime" in result["recommended_next_action"]


def test_execution_health_exposes_sandbox_failure(
    config,
    tmp_path,
    monkeypatch,
):
    registry = _register_runtime(config, tmp_path, monkeypatch)
    monkeypatch.setattr(
        "host_coding_agent.health.check_sandbox_exec",
        lambda cwd: {
            "ok": False,
            "category": "sandbox_apply_failed",
            "error": "sandbox-exec: sandbox_apply: Operation not permitted",
            "exit_code": 71,
        },
    )

    result = check_execution_health(
        config=config,
        profile_name="invest-bot",
        runtime_registry=registry,
    )

    assert result["ok"] is False
    assert result["checks"]["sandbox"]["category"] == "sandbox_apply_failed"
    assert result["checks"]["sandbox"]["exit_code"] == 71
    assert "sandbox-exec" in result["recommended_next_action"]


@pytest.mark.asyncio
async def test_check_execution_health_mcp_tool(config, tmp_path, monkeypatch):
    workspace = config.security.allowed_roots[0] / "workspace"
    workspace.mkdir()
    config.auth.enabled = True
    config.profiles["invest-bot"] = _profile_config(workspace)
    config.profiles["invest-bot"].allowed_container_roots = []
    config.profiles["invest-bot"].runtime_labels = {}
    config.profiles["invest-bot"].default_cwd = workspace
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.root = tmp_path / "worktrees"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    monkeypatch.setenv("TEST_TOKEN", "d" * 32)
    monkeypatch.setattr(server, "get_access_token", lambda: _access_token())
    monkeypatch.setattr(
        "host_coding_agent.health.check_sandbox_exec",
        lambda cwd: {"ok": True, "command": "sandbox-exec"},
    )

    mcp, _ = server.create_server(config_path)
    result = await mcp.call_tool(
        "check_execution_health",
        {"isolation_mode": "direct"},
    )
    data = result.structured_content

    assert data["profile"] == "invest-bot"
    assert data["requested_cwd"] == str(workspace)
    assert data["resolved_cwd"] == str(workspace)
    assert data["checks"]["auth"]["ok"] is True


@pytest.mark.asyncio
async def test_check_host_coding_agents_points_to_execution_health(
    config,
    tmp_path,
    monkeypatch,
):
    workspace = config.security.allowed_roots[0] / "workspace"
    workspace.mkdir()
    config.auth.enabled = True
    config.profiles["invest-bot"] = _profile_config(workspace)
    config.profiles["invest-bot"].allowed_container_roots = []
    config.profiles["invest-bot"].runtime_labels = {}
    config.profiles["invest-bot"].default_cwd = workspace
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.root = tmp_path / "worktrees"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    monkeypatch.setenv("TEST_TOKEN", "d" * 32)
    monkeypatch.setattr(server, "get_access_token", lambda: _access_token())
    monkeypatch.setattr(
        "host_coding_agent.health.check_sandbox_exec",
        lambda cwd: {
            "ok": False,
            "category": "sandbox_apply_failed",
            "error": "sandbox-exec: sandbox_apply: Operation not permitted",
            "exit_code": 71,
        },
    )

    mcp, _ = server.create_server(config_path)
    result = await mcp.call_tool("check_host_coding_agents", {})
    data = result.structured_content

    assert data["discovery_scope"] == "cli_availability"
    assert data["execution_health_tool"] == "check_execution_health"
    assert "does not guarantee" in data["warning"]
    assert data["execution_ready"] is False
    assert data["execution_health"]["requested_cwd"] == str(workspace)
    assert data["execution_health"]["checks"]["sandbox"]["category"] == "sandbox_apply_failed"
    assert "sandbox" in data["execution_health"]["failed_checks"]


@pytest.mark.asyncio
async def test_check_host_coding_agents_can_skip_execution_health(
    config,
    tmp_path,
    monkeypatch,
):
    workspace = config.security.allowed_roots[0] / "workspace"
    workspace.mkdir()
    config.auth.enabled = True
    config.profiles["invest-bot"] = _profile_config(workspace)
    config.profiles["invest-bot"].allowed_container_roots = []
    config.profiles["invest-bot"].runtime_labels = {}
    config.profiles["invest-bot"].default_cwd = workspace
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.root = tmp_path / "worktrees"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    monkeypatch.setenv("TEST_TOKEN", "d" * 32)
    monkeypatch.setattr(server, "get_access_token", lambda: _access_token())

    mcp, _ = server.create_server(config_path)
    result = await mcp.call_tool(
        "check_host_coding_agents",
        {"include_execution_health": False},
    )
    data = result.structured_content

    assert data["discovery_scope"] == "cli_availability"
    assert "execution_health" not in data
