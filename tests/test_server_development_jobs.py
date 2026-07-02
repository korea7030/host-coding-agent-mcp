from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from fastmcp.server.auth import AccessToken

import server
from host_coding_agent.models import (
    AgentName,
    DeliveryMode,
    ProfileConfig,
    RunMode,
)
from host_coding_agent.worktrees import WorktreeManager


def _access_token(profile: str = "dev-bot") -> AccessToken:
    return AccessToken(
        token="x" * 32,
        client_id=profile,
        subject=profile,
        scopes=["host-coding-agent"],
        claims={"profile": profile},
    )


def _repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"],
        check=True,
    )
    (repository / "app.py").write_text("original\n")
    (repository / ".host-coding-agent.yaml").write_text(
        "version: 1\n"
        "tests:\n"
        "  timeout_sec: 30\n"
        "  commands:\n"
        "    - [git, status, --porcelain]\n"
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "base"],
        check=True,
    )
    return repository


@pytest.mark.asyncio
async def test_external_mcp_commit_job_workflow_and_profile_isolation(
    config,
    monkeypatch,
    tmp_path: Path,
):
    repository = _repository(config.security.allowed_roots[0])
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_TOKEN",
        allowed_roots=[repository],
        allowed_agents=[AgentName.codex],
        allowed_modes=[RunMode.read_only, RunMode.propose_patch],
        allowed_delivery_modes=[DeliveryMode.manual, DeliveryMode.commit],
        default_cwd=repository,
        default_agent=AgentName.codex,
    )
    config.profiles["other-bot"] = ProfileConfig(
        token_env="TEST_OTHER_TOKEN",
        allowed_roots=[repository],
        allowed_agents=[AgentName.codex],
        allowed_modes=[RunMode.read_only, RunMode.propose_patch],
        default_cwd=repository,
        default_agent=AgentName.codex,
    )
    monkeypatch.setenv("TEST_DEV_TOKEN", "d" * 32)
    monkeypatch.setenv("TEST_OTHER_TOKEN", "o" * 32)
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.root = tmp_path / "worktrees"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    current_token = _access_token()
    monkeypatch.setattr(server, "get_access_token", lambda: current_token)
    mcp, _ = server.create_server(config_path)

    created = await mcp.call_tool(
        "create_development_job",
        {
            "task": "change app",
            "delivery_mode": "commit",
        },
    )
    created_data = created.structured_content
    assert created_data is not None and created_data["ok"]
    job_id = created_data["job"]["job_id"]
    worktree = Path(created_data["job"]["worktree"])

    executed = await mcp.call_tool(
        "run_development_job",
        {
            "job_id": job_id,
            "task": "change app",
            "agent": "codex",
        },
    )
    assert executed.structured_content["ok"]
    (worktree / "app.py").write_text("developed\n")

    tested = await mcp.call_tool(
        "test_development_job",
        {"job_id": job_id},
    )
    assert tested.structured_content["ok"]

    proposed = await mcp.call_tool(
        "propose_development_job",
        {"job_id": job_id},
    )
    proposed_data = proposed.structured_content
    assert proposed_data["ok"]
    assert proposed_data["proposal_sha256"].startswith("sha256:")

    delivered = await mcp.call_tool(
        "deliver_development_job",
        {"job_id": job_id},
    )
    delivered_data = delivered.structured_content
    assert delivered_data["ok"]
    assert delivered_data["resolved_mode"] == "commit"
    assert not worktree.exists()
    assert (repository / "app.py").read_text() == "original\n"
    assert subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "show",
            f"{delivered_data['branch']}:app.py",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout == "developed\n"

    fetched = await mcp.call_tool(
        "get_development_job",
        {"job_id": job_id},
    )
    assert fetched.structured_content["job"]["status"] == "delivered"
    listed = await mcp.call_tool("list_development_jobs", {"limit": 10})
    assert [item["job_id"] for item in listed.structured_content["jobs"]] == [
        job_id
    ]

    current_token = _access_token("other-bot")
    isolated = await mcp.call_tool(
        "get_development_job",
        {"job_id": job_id},
    )
    assert not isolated.structured_content["ok"]
    assert "not found" in isolated.structured_content["error"]


@pytest.mark.asyncio
async def test_run_job_rejects_task_different_from_creation(
    config,
    monkeypatch,
    tmp_path: Path,
):
    repository = _repository(config.security.allowed_roots[0])
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_TOKEN",
        allowed_roots=[repository],
        allowed_agents=[AgentName.codex],
        allowed_modes=[RunMode.propose_patch],
        default_cwd=repository,
        default_agent=AgentName.codex,
    )
    monkeypatch.setenv("TEST_DEV_TOKEN", "d" * 32)
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.root = tmp_path / "worktrees"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    monkeypatch.setattr(
        server,
        "get_access_token",
        lambda: _access_token(),
    )
    mcp, _ = server.create_server(config_path)
    created = await mcp.call_tool(
        "create_development_job",
        {"task": "original task"},
    )
    job_id = created.structured_content["job"]["job_id"]

    result = await mcp.call_tool(
        "run_development_job",
        {
            "job_id": job_id,
            "task": "different task",
            "agent": "codex",
        },
    )

    assert not result.structured_content["ok"]
    assert "does not match" in result.structured_content["error"]
    abandoned = await mcp.call_tool(
        "abandon_development_job",
        {"job_id": job_id},
    )
    assert abandoned.structured_content["ok"]
    assert abandoned.structured_content["job"]["status"] == "abandoned"
    manager = WorktreeManager(
        root=config.worktrees.root,
        state_path=config.worktrees.state_path,
    )
    job = manager.get(job_id, profile="dev-bot")
    assert job.status.value == "abandoned"
    assert not job.worktree.exists()
