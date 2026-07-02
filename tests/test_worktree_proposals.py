from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from host_coding_agent.approvals import ApprovalStore
from host_coding_agent.artifacts import ProposalStore
from host_coding_agent.models import AgentName, WorktreeStatus
from host_coding_agent.proposals import create_managed_worktree_proposal
from host_coding_agent.worktrees import WorktreeManager


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
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
    (repository / "remove.txt").write_text("remove me\n")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "base"],
        check=True,
    )
    return repository


def _manager(tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(
        root=tmp_path / "managed-worktrees",
        state_path=tmp_path / "state" / "worktrees.db",
        ttl_sec=3600,
    )


def _store(tmp_path: Path) -> ProposalStore:
    return ProposalStore(
        tmp_path / "artifacts" / "proposals.db",
        ttl_sec=3600,
        max_diff_chars=1_000_000,
    )


def _approvals(store: ProposalStore) -> ApprovalStore:
    return ApprovalStore(store.path)


def _tested_job(manager: WorktreeManager, repository: Path):
    job = manager.create(
        repository=repository,
        profile="dev-bot",
        task="modify, add, and remove files",
    )
    manager.transition(
        job.job_id,
        profile="dev-bot",
        status=WorktreeStatus.active,
    )
    return manager.transition(
        job.job_id,
        profile="dev-bot",
        status=WorktreeStatus.tested,
    )


def test_creates_immutable_proposal_from_tested_worktree(tmp_path):
    repository = _repository(tmp_path)
    manager = _manager(tmp_path)
    store = _store(tmp_path)
    job = _tested_job(manager, repository)
    (job.worktree / "app.py").write_text("changed\n")
    (job.worktree / "new.py").write_text("print('new')\n")
    (job.worktree / "remove.txt").unlink()

    result = create_managed_worktree_proposal(
        manager=manager,
        proposals=store,
        approvals=_approvals(store),
        job_id=job.job_id,
        profile="dev-bot",
        agent=AgentName.opencode,
    )

    assert result.ok
    assert result.changed_files == ["app.py", "new.py", "remove.txt"]
    assert result.proposal_id
    assert result.proposal_sha256
    assert manager.get(
        job.job_id, profile="dev-bot"
    ).status == WorktreeStatus.proposed
    proposal = store.get(result.proposal_id, profile="dev-bot")
    assert proposal["cwd"] == str(repository.resolve())
    assert proposal["task_hash"] == job.task_hash
    assert proposal["git_head"] == job.base_commit
    assert proposal["base_files"]["new.py"] is None
    assert _approvals(store).get_for_proposal(
        result.proposal_id,
        profile="dev-bot",
    )["status"] == "pending"
    assert (repository / "app.py").read_text() == "original\n"
    assert not (repository / "new.py").exists()

    with sqlite3.connect(manager.state_path) as connection:
        link = connection.execute(
            """
            SELECT proposal_id, proposal_sha256
            FROM worktree_proposals WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
        assert link == (result.proposal_id, result.proposal_sha256)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE worktree_proposals SET proposal_sha256 = 'tampered'
                WHERE job_id = ?
                """,
                (job.job_id,),
            )


def test_includes_untracked_binary_file(tmp_path):
    repository = _repository(tmp_path)
    manager = _manager(tmp_path)
    store = _store(tmp_path)
    job = _tested_job(manager, repository)
    (job.worktree / "asset.bin").write_bytes(bytes(range(256)))

    result = create_managed_worktree_proposal(
        manager=manager,
        proposals=store,
        approvals=_approvals(store),
        job_id=job.job_id,
        profile="dev-bot",
        agent=AgentName.codex,
    )

    assert result.ok
    proposal = store.get(result.proposal_id, profile="dev-bot")
    assert "GIT binary patch" in proposal["diff_text"]
    assert result.changed_files == ["asset.bin"]


def test_empty_worktree_fails_job_and_releases_lock(tmp_path):
    repository = _repository(tmp_path)
    manager = _manager(tmp_path)
    store = _store(tmp_path)
    job = _tested_job(manager, repository)

    result = create_managed_worktree_proposal(
        manager=manager,
        proposals=store,
        approvals=_approvals(store),
        job_id=job.job_id,
        profile="dev-bot",
        agent=AgentName.codex,
    )

    assert not result.ok
    assert "no file paths" in result.error
    assert manager.get(
        job.job_id, profile="dev-bot"
    ).status == WorktreeStatus.failed
    replacement = manager.create(
        repository=repository,
        profile="dev-bot",
        task="retry",
    )
    assert replacement.job_id != job.job_id


def test_changed_delivery_target_fails_closed(tmp_path):
    repository = _repository(tmp_path)
    manager = _manager(tmp_path)
    store = _store(tmp_path)
    job = _tested_job(manager, repository)
    (job.worktree / "app.py").write_text("changed\n")
    (repository / "local.txt").write_text("unexpected\n")

    result = create_managed_worktree_proposal(
        manager=manager,
        proposals=store,
        approvals=_approvals(store),
        job_id=job.job_id,
        profile="dev-bot",
        agent=AgentName.codex,
    )

    assert not result.ok
    assert "uncommitted or untracked" in result.error
    assert manager.get(
        job.job_id, profile="dev-bot"
    ).status == WorktreeStatus.failed
