from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml
from fastmcp.server.auth import AccessToken

import server
from host_coding_agent.models import (
    AgentName,
    DeliveryMode,
    IsolationMode,
    ProfileConfig,
    RunMode,
    RunResult,
)


def _access_token(profile: str = "dev-bot") -> AccessToken:
    return AccessToken(
        token="x" * 32,
        client_id=profile,
        subject=profile,
        scopes=["host-coding-agent"],
        claims={"profile": profile},
    )


@pytest.mark.asyncio
async def test_async_development_task_returns_immediately_and_can_be_polled(
    config, monkeypatch, tmp_path: Path
):
    workspace = config.security.allowed_roots[0]
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_TOKEN",
        allowed_roots=[workspace],
        allowed_agents=[AgentName.codex],
        allowed_modes=[RunMode.propose_patch],
        allowed_isolation_modes=[IsolationMode.direct],
        default_isolation_mode=IsolationMode.direct,
        default_cwd=workspace,
        default_agent=AgentName.codex,
    )
    monkeypatch.setenv("TEST_DEV_TOKEN", "d" * 32)
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.root = tmp_path / "worktrees"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False))
    monkeypatch.setattr(server, "get_access_token", lambda: _access_token())

    def fake_agent(**kwargs):
        time.sleep(0.05)
        return RunResult(
            ok=True,
            selected_agent=AgentName.codex,
            cwd=Path(kwargs["cwd"]),
            mode=RunMode.apply_patch,
            summary="done",
        )

    monkeypatch.setattr(server, "execute_agent", fake_agent)
    mcp, _ = server.create_server(config_path)

    started = await mcp.call_tool(
        "start_development_task",
        {"task": "change app", "agent": "codex", "isolation_mode": "direct"},
    )
    start_data = started.structured_content
    assert start_data["ok"]
    assert start_data["job_id"]
    assert start_data["poll_with"] == "get_async_job"

    deadline = time.monotonic() + 2
    while True:
        polled = await mcp.call_tool("get_async_job", {"job_id": start_data["job_id"]})
        job = polled.structured_content["job"]
        if job["status"] in {"succeeded", "failed"}:
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert job["status"] == "succeeded"
    assert job["result"]["ok"] is True
    assert job["result"]["selected_agent"] == "codex"
    events = await mcp.call_tool(
        "get_async_job_events",
        {"job_id": start_data["job_id"], "after": 0},
    )
    event_data = events.structured_content
    assert event_data["ok"]
    assert [item["stage"] for item in event_data["events"]] == [
        "queued",
        "running",
        "workflow",
        "completed",
        "succeeded",
    ]


@pytest.mark.asyncio
async def test_async_development_task_rejects_invalid_direct_delivery_mode(
    config,
    monkeypatch,
    tmp_path: Path,
):
    workspace = config.security.allowed_roots[0]
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_TOKEN",
        allowed_roots=[workspace],
        allowed_agents=[AgentName.codex],
        allowed_modes=[RunMode.propose_patch],
        allowed_delivery_modes=[DeliveryMode.manual, DeliveryMode.commit],
        allowed_isolation_modes=[IsolationMode.direct],
        default_isolation_mode=IsolationMode.direct,
        default_cwd=workspace,
        default_agent=AgentName.codex,
    )
    monkeypatch.setenv("TEST_DEV_TOKEN", "d" * 32)
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.root = tmp_path / "worktrees"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False))
    monkeypatch.setattr(server, "get_access_token", lambda: _access_token())

    mcp, _ = server.create_server(config_path)
    result = await mcp.call_tool(
        "start_development_task",
        {
            "task": "change app",
            "agent": "codex",
            "isolation_mode": "direct",
            "delivery_mode": "commit",
        },
    )
    data = result.structured_content

    assert data["ok"] is False
    assert data["stage"] == "validation"
    assert data["error_code"] == "invalid_isolation_delivery_combination"
    assert data["requested"] == {
        "isolation_mode": "direct",
        "delivery_mode": "commit",
    }
    assert "valid_combinations" in data


@pytest.mark.asyncio
async def test_async_job_is_profile_scoped(config, monkeypatch, tmp_path: Path):
    workspace = config.security.allowed_roots[0]
    config.auth.enabled = True
    for name, token_env in (("dev-bot", "TEST_DEV_TOKEN"), ("other-bot", "TEST_OTHER_TOKEN")):
        config.profiles[name] = ProfileConfig(
            token_env=token_env,
            allowed_roots=[workspace],
            allowed_agents=[AgentName.codex],
            allowed_modes=[RunMode.propose_patch],
            allowed_isolation_modes=[IsolationMode.direct],
            default_isolation_mode=IsolationMode.direct,
            default_cwd=workspace,
            default_agent=AgentName.codex,
        )
        monkeypatch.setenv(token_env, name[0] * 32)
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.root = tmp_path / "worktrees"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False))
    current = {"profile": "dev-bot"}
    monkeypatch.setattr(server, "get_access_token", lambda: _access_token(current["profile"]))
    monkeypatch.setattr(
        server,
        "execute_agent",
        lambda **kwargs: RunResult(
            ok=True,
            selected_agent=AgentName.codex,
            cwd=Path(kwargs["cwd"]),
            mode=RunMode.apply_patch,
        ),
    )
    mcp, _ = server.create_server(config_path)
    started = await mcp.call_tool(
        "start_development_task",
        {"task": "change app", "agent": "codex", "isolation_mode": "direct"},
    )
    current["profile"] = "other-bot"
    hidden = await mcp.call_tool(
        "get_async_job",
        {"job_id": started.structured_content["job_id"]},
    )
    assert hidden.structured_content == {"ok": False, "error": "job not found"}


@pytest.mark.asyncio
async def test_cancel_async_job_is_profile_scoped_and_terminal(
    config,
    monkeypatch,
    tmp_path: Path,
):
    workspace = config.security.allowed_roots[0]
    config.auth.enabled = True
    for name, token_env in (
        ("dev-bot", "TEST_DEV_TOKEN"),
        ("other-bot", "TEST_OTHER_TOKEN"),
    ):
        config.profiles[name] = ProfileConfig(
            token_env=token_env,
            allowed_roots=[workspace],
            allowed_agents=[AgentName.codex],
            allowed_modes=[RunMode.propose_patch],
            allowed_isolation_modes=[IsolationMode.direct],
            default_isolation_mode=IsolationMode.direct,
            default_cwd=workspace,
            default_agent=AgentName.codex,
        )
        monkeypatch.setenv(token_env, name[0] * 32)
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.root = tmp_path / "worktrees"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    current = {"profile": "dev-bot"}
    monkeypatch.setattr(
        server,
        "get_access_token",
        lambda: _access_token(current["profile"]),
    )

    def slow_agent(**kwargs):
        time.sleep(0.2)
        return RunResult(
            ok=True,
            selected_agent=AgentName.codex,
            cwd=Path(kwargs["cwd"]),
            mode=RunMode.apply_patch,
        )

    monkeypatch.setattr(server, "execute_agent", slow_agent)
    mcp, _ = server.create_server(config_path)
    started = await mcp.call_tool(
        "start_development_task",
        {"task": "change app", "agent": "codex", "isolation_mode": "direct"},
    )
    job_id = started.structured_content["job_id"]

    current["profile"] = "other-bot"
    hidden = await mcp.call_tool("cancel_async_job", {"job_id": job_id})
    assert hidden.structured_content == {"ok": False, "error": "job not found"}

    current["profile"] = "dev-bot"
    cancelled = await mcp.call_tool(
        "cancel_async_job",
        {"job_id": job_id, "reason": "telegram stop request"},
    )
    data = cancelled.structured_content
    assert data["ok"]
    assert data["cancelled"] is True
    assert data["status"] == "failed"
    assert data["stage"] == "cancelled"
    assert data["process_kill_guaranteed"] is False
    assert data["job"]["error"] == "telegram stop request"
