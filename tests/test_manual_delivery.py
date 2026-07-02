from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from host_coding_agent.applier import PatchApplier, PatchApplyError
from host_coding_agent.approvals import ApprovalStore
from host_coding_agent.artifacts import ProposalStore
from host_coding_agent.delivery import ManualDelivery
from host_coding_agent.models import AgentName, ProfileConfig, WorktreeStatus
from host_coding_agent.proposals import create_managed_worktree_proposal
from host_coding_agent.worktrees import WorktreeManager


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
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "base"],
        check=True,
    )
    return repository


def _workflow(config, tmp_path: Path):
    repository = _repository(config.security.allowed_roots[0])
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_TOKEN",
        allowed_roots=[repository],
        default_cwd=repository,
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
    proposal = proposals.get(
        proposal_result.proposal_id,
        profile="dev-bot",
    )
    delivery = ManualDelivery(
        manager=manager,
        applier=PatchApplier(
            config=config,
            proposals=proposals,
            approvals=approvals,
        ),
    )
    return repository, manager, proposals, approvals, job, proposal, delivery


def _approve(approvals: ApprovalStore, proposal: dict) -> None:
    approvals.decide(
        proposal_id=proposal["proposal_id"],
        profile="dev-bot",
        proposal_sha256=proposal["diff_sha256"],
        approved=True,
        decided_by="telegram:123",
        decision_channel="telegram",
    )


def test_manual_delivery_applies_and_cleans_worktree(config, tmp_path):
    repository, manager, _, approvals, job, proposal, delivery = _workflow(
        config,
        tmp_path,
    )
    _approve(approvals, proposal)

    result = delivery.deliver(
        proposal_id=proposal["proposal_id"],
        profile="dev-bot",
        proposal_sha256=proposal["diff_sha256"],
    )

    assert result["ok"]
    assert result["delivery_status"] == "delivered"
    assert result["cleanup"]["ok"]
    assert (repository / "app.py").read_text() == "delivered\n"
    assert not job.worktree.exists()
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
    assert manager.get(
        job.job_id,
        profile="dev-bot",
    ).status == WorktreeStatus.delivered
    assert approvals.get_for_proposal(
        proposal["proposal_id"],
        profile="dev-bot",
    )["status"] == "applied"
    with sqlite3.connect(manager.state_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM repository_locks WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0] == 0
        cleanup_id = connection.execute(
            """
            SELECT cleanup_id FROM worktree_cleanup_runs
            WHERE job_id = ? AND ok = 1
            """,
            (job.job_id,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE worktree_cleanup_runs SET ok = 0
                WHERE cleanup_id = ?
                """,
                (cleanup_id,),
            )


def test_manual_delivery_is_idempotent_after_success(config, tmp_path):
    _, _, _, approvals, _, proposal, delivery = _workflow(config, tmp_path)
    _approve(approvals, proposal)
    delivery.deliver(
        proposal_id=proposal["proposal_id"],
        profile="dev-bot",
        proposal_sha256=proposal["diff_sha256"],
    )

    repeated = delivery.deliver(
        proposal_id=proposal["proposal_id"],
        profile="dev-bot",
        proposal_sha256=proposal["diff_sha256"],
    )

    assert repeated["ok"]
    assert repeated["already_delivered"]
    assert repeated["cleanup"]["ok"]


def test_apply_failure_marks_job_failed_without_cleanup(config, tmp_path):
    repository, manager, _, approvals, job, proposal, delivery = _workflow(
        config,
        tmp_path,
    )
    _approve(approvals, proposal)
    (repository / "app.py").write_text("changed elsewhere\n")

    with pytest.raises(PatchApplyError, match="base file changed"):
        delivery.deliver(
            proposal_id=proposal["proposal_id"],
            profile="dev-bot",
            proposal_sha256=proposal["diff_sha256"],
        )

    assert manager.get(
        job.job_id,
        profile="dev-bot",
    ).status == WorktreeStatus.failed
    assert job.worktree.exists()
    with sqlite3.connect(manager.state_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM repository_locks WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0] == 0


def test_cleanup_rejects_non_terminal_job(config, tmp_path):
    _, manager, _, _, job, _, _ = _workflow(config, tmp_path)

    with pytest.raises(ValueError, match="terminal"):
        manager.cleanup(job.job_id, profile="dev-bot")
