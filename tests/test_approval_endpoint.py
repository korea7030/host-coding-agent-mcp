from __future__ import annotations

import subprocess
from pathlib import Path

import yaml
from starlette.testclient import TestClient

import server
from host_coding_agent.approvals import ApprovalStore
from host_coding_agent.artifacts import ProposalStore
from host_coding_agent.models import AgentName, ProfileConfig, WorktreeStatus
from host_coding_agent.proposals import create_managed_worktree_proposal
from host_coding_agent.worktrees import WorktreeManager


def test_telegram_endpoint_requires_profile_token_and_allowed_identity(
    config,
    monkeypatch,
    tmp_path: Path,
):
    workspace = config.security.allowed_roots[0]
    (workspace / "app.py").write_text("old\n")
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_TOKEN",
        allowed_roots=[workspace],
        approval_identities=["telegram:123"],
        default_cwd=workspace,
    )
    token = "t" * 32
    monkeypatch.setenv("TEST_DEV_TOKEN", token)
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    proposals = ProposalStore(
        config.artifacts.path,
        ttl_sec=3600,
        max_diff_chars=100_000,
    )
    proposal = proposals.create(
        profile="dev-bot",
        cwd=workspace,
        agent=AgentName.codex,
        task="change app",
        diff_text=(
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
    )
    ApprovalStore(config.artifacts.path).create_pending(proposal)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    mcp, _ = server.create_server(config_path)

    with TestClient(mcp.http_app(path="/mcp")) as client:
        unauthorized = client.post(
            "/approval/telegram",
            json={
                "action": "show",
                "proposal_id": proposal["proposal_id"],
                "telegram_user_id": "999",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        accepted = client.post(
            "/approval/telegram",
            json={
                "action": "show",
                "proposal_id": proposal["proposal_id"],
                "telegram_user_id": "123",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert unauthorized.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["proposal"]["proposal_id"] == proposal["proposal_id"]


def test_telegram_approval_delivers_and_cleans_managed_worktree(
    config,
    monkeypatch,
    tmp_path: Path,
):
    workspace = config.security.allowed_roots[0]
    repository = workspace / "repository"
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
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "base"],
        check=True,
    )
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_TOKEN",
        allowed_roots=[workspace],
        approval_identities=["telegram:123"],
        default_cwd=workspace,
    )
    token = "t" * 32
    monkeypatch.setenv("TEST_DEV_TOKEN", token)
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.root = tmp_path / "worktrees"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    proposals = ProposalStore(
        config.artifacts.path,
        ttl_sec=3600,
        max_diff_chars=100_000,
    )
    approvals = ApprovalStore(config.artifacts.path)
    manager = WorktreeManager(
        root=config.worktrees.root,
        state_path=config.worktrees.state_path,
        ttl_sec=3600,
    )
    job = manager.create(
        repository=repository,
        profile="dev-bot",
        task="change app",
    )
    manager.transition(
        job.job_id,
        profile="dev-bot",
        status=WorktreeStatus.active,
    )
    (job.worktree / "app.py").write_text("delivered\n")
    manager.transition(
        job.job_id,
        profile="dev-bot",
        status=WorktreeStatus.tested,
    )
    proposal_result = create_managed_worktree_proposal(
        manager=manager,
        proposals=proposals,
        approvals=approvals,
        job_id=job.job_id,
        profile="dev-bot",
        agent=AgentName.codex,
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    mcp, _ = server.create_server(config_path)

    with TestClient(mcp.http_app(path="/mcp")) as client:
        response = client.post(
            "/approval/telegram",
            json={
                "action": "approve",
                "proposal_id": proposal_result.proposal_id,
                "proposal_sha256": proposal_result.proposal_sha256,
                "telegram_user_id": "123",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        repeated = client.post(
            "/approval/telegram",
            json={
                "action": "approve",
                "proposal_id": proposal_result.proposal_id,
                "proposal_sha256": proposal_result.proposal_sha256,
                "telegram_user_id": "123",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["delivery_status"] == "delivered"
    assert response.json()["cleanup"]["ok"]
    assert (repository / "app.py").read_text() == "delivered\n"
    assert not job.worktree.exists()
    assert repeated.status_code == 200
    assert repeated.json()["already_delivered"]
