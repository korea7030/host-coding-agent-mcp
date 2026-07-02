from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

from host_coding_agent.models import WorktreeConfig, WorktreeStatus
from host_coding_agent.testing import run_managed_worktree_tests
from host_coding_agent.worktrees import WorktreeManager


def _repository(tmp_path: Path, policy: str) -> Path:
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
    (repository / ".host-coding-agent.yaml").write_text(policy)
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


def _active_job(manager: WorktreeManager, repository: Path):
    job = manager.create(
        repository=repository,
        profile="dev-bot",
        task="change app",
    )
    return manager.transition(
        job.job_id,
        profile="dev-bot",
        status=WorktreeStatus.active,
    )


def _policy(command: list[str], *, timeout: int = 30) -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "tests": {"commands": [command], "timeout_sec": timeout},
        }
    )


def test_passing_base_commit_policy_marks_job_tested(tmp_path):
    repository = _repository(
        tmp_path,
        _policy(["git", "status", "--porcelain"]),
    )
    manager = _manager(tmp_path)
    job = _active_job(manager, repository)

    result = run_managed_worktree_tests(
        manager=manager,
        job_id=job.job_id,
        profile="dev-bot",
        config=WorktreeConfig(),
    )

    assert result.ok
    assert result.results[0].stdout == ""
    assert manager.get(
        job.job_id, profile="dev-bot"
    ).status == WorktreeStatus.tested


def test_failing_command_marks_job_failed_and_releases_repository_lock(tmp_path):
    repository = _repository(
        tmp_path,
        _policy(["git", "rev-parse", "--verify", "missing-test-ref"]),
    )
    manager = _manager(tmp_path)
    job = _active_job(manager, repository)

    result = run_managed_worktree_tests(
        manager=manager,
        job_id=job.job_id,
        profile="dev-bot",
        config=WorktreeConfig(),
    )

    assert not result.ok
    assert result.results[0].returncode != 0
    assert manager.get(
        job.job_id, profile="dev-bot"
    ).status == WorktreeStatus.failed
    replacement = manager.create(
        repository=repository,
        profile="dev-bot",
        task="retry",
    )
    assert replacement.job_id != job.job_id


def test_policy_is_loaded_from_base_commit_not_modified_worktree(tmp_path):
    repository = _repository(
        tmp_path,
        _policy(["git", "status", "--porcelain"]),
    )
    manager = _manager(tmp_path)
    job = _active_job(manager, repository)
    (job.worktree / ".host-coding-agent.yaml").write_text(
        _policy(["git", "rev-parse", "--verify", "missing-test-ref"])
    )

    result = run_managed_worktree_tests(
        manager=manager,
        job_id=job.job_id,
        profile="dev-bot",
        config=WorktreeConfig(),
    )

    assert result.ok
    assert result.results[0].stdout == " M .host-coding-agent.yaml\n"
    assert result.policy_commit == job.base_commit


@pytest.mark.parametrize(
    "policy",
    [
        "version: 1\ntests:\n  commands:\n    - pytest -q\n",
        "version: 1\ntests:\n  commands:\n    - [/bin/sh, -c, echo bad]\n",
        "version: 1\ntests:\n  commands:\n    - [../outside]\n",
    ],
)
def test_invalid_or_unsafe_policy_fails_closed(tmp_path, policy):
    repository = _repository(tmp_path, policy)
    manager = _manager(tmp_path)
    job = _active_job(manager, repository)

    result = run_managed_worktree_tests(
        manager=manager,
        job_id=job.job_id,
        profile="dev-bot",
        config=WorktreeConfig(),
    )

    assert not result.ok
    assert manager.get(
        job.job_id, profile="dev-bot"
    ).status == WorktreeStatus.failed


def test_test_run_records_are_immutable(tmp_path):
    repository = _repository(
        tmp_path,
        _policy(["git", "status", "--porcelain"]),
    )
    manager = _manager(tmp_path)
    job = _active_job(manager, repository)
    result = run_managed_worktree_tests(
        manager=manager,
        job_id=job.job_id,
        profile="dev-bot",
        config=WorktreeConfig(),
    )

    with sqlite3.connect(manager.state_path) as connection:
        row = connection.execute(
            "SELECT command_json FROM worktree_test_runs WHERE run_id = ?",
            (result.results[0].run_id,),
        ).fetchone()
        assert row is not None
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE worktree_test_runs SET stdout = 'tampered' WHERE run_id = ?",
                (result.results[0].run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM worktree_test_runs WHERE run_id = ?",
                (result.results[0].run_id,),
            )
