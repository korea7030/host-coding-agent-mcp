from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import host_coding_agent.automated_delivery as delivery_module
from host_coding_agent.approvals import ApprovalError, ApprovalStore
from host_coding_agent.artifacts import ProposalStore
from host_coding_agent.automated_delivery import (
    AutomatedDelivery,
    AutomatedDeliveryError,
)
from host_coding_agent.models import (
    AgentName,
    DeliveryMode,
    ProfileConfig,
    WorktreeStatus,
)
from host_coding_agent.proposals import create_managed_worktree_proposal
from host_coding_agent.worktrees import WorktreeManager


def _repository(root: Path, *, remote_url: str | None = None) -> Path:
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
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "base"],
        check=True,
    )
    if remote_url:
        subprocess.run(
            ["git", "-C", str(repository), "remote", "add", "origin", remote_url],
            check=True,
        )
    return repository


def _workflow(
    config,
    tmp_path: Path,
    *,
    mode: DeliveryMode,
    remote_url: str | None = None,
    enable_pr: bool = False,
):
    repository = _repository(
        config.security.allowed_roots[0],
        remote_url=remote_url,
    )
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_TOKEN",
        allowed_roots=[repository],
        default_cwd=repository,
        allowed_delivery_modes=[
            DeliveryMode.manual,
            DeliveryMode.auto,
            DeliveryMode.commit,
            DeliveryMode.pr,
        ],
        allow_git_push=enable_pr,
        allow_pull_requests=enable_pr,
    )
    manager = WorktreeManager(
        root=tmp_path / "worktrees",
        state_path=tmp_path / "state" / "worktrees.db",
        ttl_sec=3600,
    )
    proposals = ProposalStore(
        tmp_path / "artifacts" / "proposals.db",
        ttl_sec=3600,
        max_diff_chars=100_000,
    )
    approvals = ApprovalStore(proposals.path)
    job = manager.create(
        repository=repository,
        profile="dev-bot",
        task="change app",
        delivery_mode=mode,
    )
    target = manager.get_delivery_target(job.job_id, profile="dev-bot")
    manager.transition(
        job.job_id,
        profile="dev-bot",
        status=WorktreeStatus.active,
    )
    (job.worktree / "app.py").write_text("automated\n")
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
    assert proposal_result.ok
    with pytest.raises(ApprovalError, match="not found"):
        approvals.get_for_proposal(
            proposal_result.proposal_id,
            profile="dev-bot",
        )
    delivery = AutomatedDelivery(
        manager=manager,
        proposals=proposals,
        config=config,
    )
    return repository, manager, job, target, delivery


def test_commit_delivery_preserves_local_branch(config, tmp_path):
    repository, manager, job, target, delivery = _workflow(
        config,
        tmp_path,
        mode=DeliveryMode.commit,
    )

    result = delivery.deliver(job_id=job.job_id, profile="dev-bot")

    assert result["resolved_mode"] == "commit"
    assert result["remote"] is None
    assert result["cleanup"]["ok"]
    assert result["cleanup"]["branch_removed"] is False
    assert not job.worktree.exists()
    assert (repository / "app.py").read_text() == "original\n"
    content = subprocess.run(
        ["git", "-C", str(repository), "show", f"{job.branch}:app.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert content == "automated\n"
    assert target["remote_name"] is None
    with sqlite3.connect(manager.state_path) as connection:
        row = connection.execute(
            """
            SELECT resolved_mode, commit_sha, remote_name
            FROM worktree_deliveries WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE worktree_delivery_targets SET remote_name = 'evil'
                WHERE job_id = ?
                """,
                (job.job_id,),
            )
    assert row == ("commit", result["commit_sha"], None)


def test_auto_without_remote_resolves_to_commit(config, tmp_path):
    _, _, job, _, delivery = _workflow(
        config,
        tmp_path,
        mode=DeliveryMode.auto,
        enable_pr=True,
    )

    result = delivery.deliver(job_id=job.job_id, profile="dev-bot")

    assert result["requested_mode"] == "auto"
    assert result["resolved_mode"] == "commit"


def test_pr_pushes_fixed_remote_and_removes_local_branch(
    config,
    tmp_path,
    monkeypatch,
):
    remote_url = "git@github.com:example/project.git"
    repository, _, job, target, delivery = _workflow(
        config,
        tmp_path,
        mode=DeliveryMode.pr,
        remote_url=remote_url,
        enable_pr=True,
    )
    real_run = delivery_module._run
    calls: list[list[str]] = []

    def fake_external(command, *, cwd, timeout=120):
        calls.append(command)
        if command[:2] == ["git", "push"]:
            return ""
        if command[:3] == ["gh", "pr", "create"]:
            return "https://github.com/example/project/pull/42"
        return real_run(command, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(delivery_module, "_run", fake_external)

    result = delivery.deliver(job_id=job.job_id, profile="dev-bot")

    assert result["resolved_mode"] == "pr"
    assert result["remote"] == "origin"
    assert result["pr_url"].endswith("/pull/42")
    assert result["cleanup"]["branch_removed"]
    assert target == {
        "base_branch": "master",
        "remote_name": "origin",
        "remote_url": remote_url,
        "remote_push_url": remote_url,
    }
    assert [
        "git",
        "push",
        "--no-verify",
        "--set-upstream",
        "origin",
        job.branch,
    ] in calls
    branches = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert job.branch not in branches


def test_pr_without_remote_is_rejected_at_job_creation(config, tmp_path):
    repository = _repository(config.security.allowed_roots[0])
    manager = WorktreeManager(
        root=tmp_path / "worktrees",
        state_path=tmp_path / "state" / "worktrees.db",
        ttl_sec=3600,
    )

    with pytest.raises(ValueError, match="requires an unambiguous remote"):
        manager.create(
            repository=repository,
            profile="dev-bot",
            task="change app",
            delivery_mode=DeliveryMode.pr,
        )

    assert manager.list(profile="dev-bot") == []


def test_worktree_change_after_proposal_is_rejected(config, tmp_path):
    _, manager, job, _, delivery = _workflow(
        config,
        tmp_path,
        mode=DeliveryMode.commit,
    )
    (job.worktree / "late.txt").write_text("tampered\n")

    with pytest.raises(AutomatedDeliveryError, match="changed after"):
        delivery.deliver(job_id=job.job_id, profile="dev-bot")

    assert manager.get(
        job.job_id,
        profile="dev-bot",
    ).status == WorktreeStatus.proposed
