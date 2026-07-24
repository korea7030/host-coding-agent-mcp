from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from fastmcp.server.auth import AccessToken

import server
from host_coding_agent.approvals import ApprovalStore
from host_coding_agent.models import (
    AgentName,
    DeliveryMode,
    DirectWritePolicy,
    IsolationMode,
    ProfileConfig,
    RunMode,
    RunResult,
    WorktreeStatus,
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
async def test_single_call_direct_mode_modifies_non_git_workspace(
    config,
    monkeypatch,
    tmp_path: Path,
):
    workspace = config.security.allowed_roots[0] / "non-git-workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("original\n")
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_TOKEN",
        allowed_roots=[workspace],
        allowed_agents=[AgentName.opencode],
        allowed_modes=[RunMode.propose_patch],
        allowed_isolation_modes=[IsolationMode.direct, IsolationMode.worktree],
        default_isolation_mode=IsolationMode.direct,
        default_cwd=workspace,
        default_agent=AgentName.opencode,
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

    def fake_direct_agent(**kwargs):
        assert kwargs["mode"] == RunMode.apply_patch
        assert kwargs["allow_apply_patch_override"] is True
        target = Path(kwargs["cwd"])
        (target / "app.py").write_text("directly modified\n")
        return RunResult(
            ok=True,
            selected_agent=AgentName.opencode,
            assistant_id="dev-bot",
            cwd=target,
            mode=RunMode.apply_patch,
            summary="modified app.py",
        )

    monkeypatch.setattr(server, "execute_agent", fake_direct_agent)
    mcp, _ = server.create_server(config_path)

    result = await mcp.call_tool(
        "run_development_task",
        {
            "task": "change app",
            "agent": "opencode",
        },
    )
    data = result.structured_content

    assert data["ok"]
    assert data["isolation_mode"] == "direct"
    assert data["applied_immediately"]
    assert data["selected_agent"] == "opencode"
    assert data["direct_write_policy"] == "allow"
    assert data["changed_files"] == ["app.py"]
    assert data["changed_file_count"] == 1
    assert (workspace / "app.py").read_text() == "directly modified\n"
    (workspace / "app.py").write_text("original again\n")
    compatibility = await mcp.call_tool(
        "run_opencode",
        {"task": "change app"},
    )
    assert compatibility.structured_content["ok"]
    assert compatibility.structured_content["isolation_mode"] == "direct"
    assert compatibility.structured_content["changed_files"] == ["app.py"]
    assert (workspace / "app.py").read_text() == "directly modified\n"
    manager = WorktreeManager(
        root=config.worktrees.root,
        state_path=config.worktrees.state_path,
    )
    assert manager.list(profile="dev-bot") == []


@pytest.mark.asyncio
async def test_direct_mode_can_fail_if_files_changed(
    config,
    monkeypatch,
    tmp_path: Path,
):
    workspace = config.security.allowed_roots[0] / "non-git-workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("original\n")
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
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    monkeypatch.setattr(server, "get_access_token", lambda: _access_token())

    def fake_direct_agent(**kwargs):
        target = Path(kwargs["cwd"])
        (target / "app.py").write_text("modified despite read-only intent\n")
        return RunResult(
            ok=True,
            selected_agent=AgentName.codex,
            assistant_id="dev-bot",
            cwd=target,
            mode=RunMode.apply_patch,
            summary="modified app.py",
        )

    monkeypatch.setattr(server, "execute_agent", fake_direct_agent)
    mcp, _ = server.create_server(config_path)

    result = await mcp.call_tool(
        "run_development_task",
        {
            "task": "inspect only",
            "agent": "codex",
            "isolation_mode": "direct",
            "direct_write_policy": "fail_if_changed",
        },
    )
    data = result.structured_content

    assert data["ok"] is False
    assert data["stage"] == "direct"
    assert data["direct_write_policy"] == DirectWritePolicy.fail_if_changed.value
    assert data["write_policy_violated"] is True
    assert data["error_code"] == "direct_write_policy_violation"
    assert data["changed_files"] == ["app.py"]
    assert (workspace / "app.py").read_text() == "modified despite read-only intent\n"


@pytest.mark.asyncio
async def test_direct_mode_requires_profile_permission(
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
        default_cwd=workspace,
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

    result = await mcp.call_tool(
        "run_development_task",
        {
            "task": "change app",
            "isolation_mode": "direct",
        },
    )

    assert not result.structured_content["ok"]
    assert "isolation mode is not allowed" in result.structured_content["error"]


@pytest.mark.asyncio
async def test_direct_mode_rejects_worktree_delivery_modes_early(
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
        allowed_isolation_modes=[IsolationMode.direct, IsolationMode.worktree],
        default_isolation_mode=IsolationMode.direct,
        default_cwd=workspace,
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

    result = await mcp.call_tool(
        "run_development_task",
        {
            "task": "change app",
            "isolation_mode": "direct",
            "delivery_mode": "commit",
        },
    )
    data = result.structured_content

    assert data["ok"] is False
    assert data["stage"] == "validation"
    assert data["error_code"] == "invalid_isolation_delivery_combination"
    assert data["error"] == "delivery_mode applies only to worktree isolation"
    assert data["requested"] == {
        "isolation_mode": "direct",
        "delivery_mode": "commit",
    }
    assert data["valid_combinations"][0]["isolation_mode"] == "direct"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_mode", "expected_stage"),
    [
        ("manual", "awaiting_approval"),
        ("commit", "delivered"),
    ],
)
async def test_single_call_development_workflow(
    config,
    monkeypatch,
    tmp_path: Path,
    delivery_mode: str,
    expected_stage: str,
):
    repository = _repository(config.security.allowed_roots[0])
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_TOKEN",
        allowed_roots=[repository],
        allowed_agents=[AgentName.codex],
        allowed_modes=[RunMode.propose_patch],
        allowed_delivery_modes=[DeliveryMode.manual, DeliveryMode.commit],
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

    def fake_agent_run(**kwargs):
        manager = kwargs["manager"]
        job = manager.get(kwargs["job_id"], profile=kwargs["profile"])
        manager.transition(
            job.job_id,
            profile=job.profile,
            status=WorktreeStatus.active,
        )
        (job.worktree / "app.py").write_text("single call\n")
        manager.record_selected_agent(
            job.job_id,
            profile=job.profile,
            agent=AgentName.codex,
        )
        return RunResult(
            ok=True,
            selected_agent=AgentName.codex,
            assistant_id=job.profile,
            cwd=job.worktree,
            requested_cwd=str(job.repository),
            path_mapping_applied=True,
            mode=RunMode.apply_patch,
        )

    monkeypatch.setattr(server, "run_managed_worktree_agent", fake_agent_run)
    mcp, _ = server.create_server(config_path)

    result = await mcp.call_tool(
        "run_development_task",
        {
            "task": "change app",
            "agent": "codex",
            "delivery_mode": delivery_mode,
        },
    )
    data = result.structured_content

    assert data["ok"]
    assert data["stage"] == expected_stage
    assert data["selected_agent"] == "codex"
    assert data["proposal_sha256"].startswith("sha256:")
    assert (repository / "app.py").read_text() == "original\n"
    manager = WorktreeManager(
        root=config.worktrees.root,
        state_path=config.worktrees.state_path,
    )
    job = manager.get(data["job_id"], profile="dev-bot")
    if delivery_mode == "manual":
        assert data["apply_command"] == (
            f"/apply_proposal {data['proposal_id']} "
            f"{data['proposal_sha256']}"
        )
        assert job.status == WorktreeStatus.proposed
        assert job.worktree.exists()
        approval = ApprovalStore(config.artifacts.path).get_for_proposal(
            data["proposal_id"],
            profile="dev-bot",
        )
        assert approval["status"] == "pending"
    else:
        assert job.status == WorktreeStatus.delivered
        assert not job.worktree.exists()
        assert subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "show",
                f"{data['branch']}:app.py",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout == "single call\n"


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
