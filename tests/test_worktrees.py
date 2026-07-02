from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from host_coding_agent.models import DeliveryMode, WorktreeStatus
from host_coding_agent.worktrees import WorktreeError, WorktreeManager


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
    subprocess.run(["git", "-C", str(repository), "add", "app.py"], check=True)
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


def test_creates_managed_branch_and_worktree_without_changing_original(tmp_path):
    repository = _repository(tmp_path)
    manager = _manager(tmp_path)

    job = manager.create(
        repository=repository,
        profile="dev-bot",
        task="change app",
        delivery_mode=DeliveryMode.manual,
    )

    assert job.status == WorktreeStatus.created
    assert job.repository == repository
    assert job.worktree.parent == manager.root
    assert job.branch.startswith("hca/dev-bot/")
    assert (job.worktree / "app.py").read_text() == "original\n"
    (job.worktree / "app.py").write_text("agent change\n")
    assert (repository / "app.py").read_text() == "original\n"
    branch = subprocess.run(
        ["git", "-C", str(job.worktree), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch == job.branch
    assert oct(manager.root.stat().st_mode & 0o777) == "0o700"
    assert oct(manager.state_path.stat().st_mode & 0o777) == "0o600"


def test_profile_isolation_for_get_and_list(tmp_path):
    repository = _repository(tmp_path)
    manager = _manager(tmp_path)
    job = manager.create(
        repository=repository,
        profile="dev-bot",
        task="change app",
    )

    assert manager.get(job.job_id, profile="dev-bot") == job
    assert manager.list(profile="dev-bot") == [job]
    assert manager.list(profile="research-bot") == []
    with pytest.raises(WorktreeError, match="not found"):
        manager.get(job.job_id, profile="research-bot")


def test_rejects_non_git_directory(tmp_path):
    directory = tmp_path / "not-git"
    directory.mkdir()
    manager = _manager(tmp_path)

    with pytest.raises(WorktreeError, match="git command failed"):
        manager.create(
            repository=directory,
            profile="dev-bot",
            task="change app",
        )


def test_database_rejects_identity_mutation_and_delete(tmp_path):
    repository = _repository(tmp_path)
    manager = _manager(tmp_path)
    job = manager.create(
        repository=repository,
        profile="dev-bot",
        task="change app",
    )

    with sqlite3.connect(manager.state_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE worktree_jobs SET branch = 'tampered' WHERE job_id = ?",
                (job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM worktree_jobs WHERE job_id = ?",
                (job.job_id,),
            )


@pytest.mark.parametrize("filename", ["tracked.txt", "untracked.txt"])
def test_rejects_dirty_repository(tmp_path, filename):
    repository = _repository(tmp_path)
    if filename == "tracked.txt":
        target = repository / "app.py"
    else:
        target = repository / filename
    target.write_text("dirty\n")
    manager = _manager(tmp_path)

    with pytest.raises(WorktreeError, match="uncommitted or untracked"):
        manager.create(
            repository=repository,
            profile="dev-bot",
            task="change app",
        )


def test_rejects_repository_operation_in_progress(tmp_path):
    repository = _repository(tmp_path)
    git_dir = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repository / git_dir / "MERGE_HEAD").write_text("0" * 40 + "\n")
    manager = _manager(tmp_path)

    with pytest.raises(WorktreeError, match="MERGE_HEAD"):
        manager.create(
            repository=repository,
            profile="dev-bot",
            task="change app",
        )


def test_repository_lock_blocks_concurrent_job_until_terminal_status(tmp_path):
    repository = _repository(tmp_path)
    manager = _manager(tmp_path)
    first = manager.create(
        repository=repository,
        profile="dev-bot",
        task="first task",
    )

    with pytest.raises(WorktreeError, match="active worktree job"):
        manager.create(
            repository=repository,
            profile="dev-bot",
            task="second task",
        )

    active = manager.transition(
        first.job_id,
        profile="dev-bot",
        status=WorktreeStatus.active,
    )
    failed = manager.transition(
        active.job_id,
        profile="dev-bot",
        status=WorktreeStatus.failed,
    )
    assert failed.status == WorktreeStatus.failed

    second = manager.create(
        repository=repository,
        profile="dev-bot",
        task="second task",
    )
    assert second.job_id != first.job_id


def test_rejects_invalid_status_transition(tmp_path):
    repository = _repository(tmp_path)
    manager = _manager(tmp_path)
    job = manager.create(
        repository=repository,
        profile="dev-bot",
        task="change app",
    )

    with pytest.raises(WorktreeError, match="invalid worktree status"):
        manager.transition(
            job.job_id,
            profile="dev-bot",
            status=WorktreeStatus.tested,
        )
