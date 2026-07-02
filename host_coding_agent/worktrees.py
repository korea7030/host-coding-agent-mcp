from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import (
    DeliveryMode,
    WorktreeCleanupResult,
    WorktreeJob,
    WorktreeStatus,
)


class WorktreeError(ValueError):
    pass


_TERMINAL_STATUSES = {
    WorktreeStatus.delivered,
    WorktreeStatus.failed,
    WorktreeStatus.abandoned,
}

_STATUS_TRANSITIONS = {
    WorktreeStatus.created: {
        WorktreeStatus.active,
        WorktreeStatus.failed,
        WorktreeStatus.abandoned,
    },
    WorktreeStatus.active: {
        WorktreeStatus.tested,
        WorktreeStatus.failed,
        WorktreeStatus.abandoned,
    },
    WorktreeStatus.tested: {
        WorktreeStatus.proposed,
        WorktreeStatus.failed,
        WorktreeStatus.abandoned,
    },
    WorktreeStatus.proposed: {
        WorktreeStatus.delivered,
        WorktreeStatus.failed,
        WorktreeStatus.abandoned,
    },
}


def _run_git(arguments: list[str], *, timeout: int = 30) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git command failed to run: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise WorktreeError(f"git command failed: {detail[:1000]}")
    return completed.stdout.strip()


def _safe_branch_segment(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized or "profile"


class WorktreeManager:
    def __init__(
        self,
        *,
        root: Path,
        state_path: Path,
        branch_prefix: str = "hca",
        ttl_sec: int = 86_400,
    ):
        self.root = Path(os.path.realpath(root.expanduser()))
        self.state_path = state_path.expanduser().resolve()
        self.branch_prefix = branch_prefix
        self.ttl_sec = ttl_sec
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worktree_jobs (
                    job_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    worktree TEXT NOT NULL UNIQUE,
                    branch TEXT NOT NULL UNIQUE,
                    base_commit TEXT NOT NULL,
                    task_hash TEXT NOT NULL,
                    delivery_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS worktree_jobs_profile_created
                    ON worktree_jobs(profile, created_at DESC);
                CREATE TABLE IF NOT EXISTS repository_locks (
                    repository TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    acquired_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worktree_test_runs (
                    run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    command_index INTEGER NOT NULL,
                    command_json TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    returncode INTEGER,
                    stdout TEXT NOT NULL,
                    stderr TEXT NOT NULL,
                    duration_sec REAL NOT NULL,
                    timed_out INTEGER NOT NULL,
                    redacted INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES worktree_jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS worktree_test_runs_job_command
                    ON worktree_test_runs(job_id, command_index);
                CREATE TABLE IF NOT EXISTS worktree_proposals (
                    job_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE,
                    proposal_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES worktree_jobs(job_id)
                );
                CREATE TRIGGER IF NOT EXISTS worktree_proposals_no_update
                    BEFORE UPDATE ON worktree_proposals
                    BEGIN
                        SELECT RAISE(ABORT, 'worktree proposal links are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS worktree_proposals_no_delete
                    BEFORE DELETE ON worktree_proposals
                    BEGIN
                        SELECT RAISE(ABORT, 'worktree proposal links cannot be deleted');
                    END;
                CREATE TABLE IF NOT EXISTS worktree_cleanup_runs (
                    cleanup_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    worktree_removed INTEGER NOT NULL,
                    branch_removed INTEGER NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES worktree_jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS worktree_cleanup_runs_job_created
                    ON worktree_cleanup_runs(job_id, created_at);
                CREATE TRIGGER IF NOT EXISTS worktree_cleanup_runs_no_update
                    BEFORE UPDATE ON worktree_cleanup_runs
                    BEGIN
                        SELECT RAISE(ABORT, 'worktree cleanup runs are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS worktree_cleanup_runs_no_delete
                    BEFORE DELETE ON worktree_cleanup_runs
                    BEGIN
                        SELECT RAISE(ABORT, 'worktree cleanup runs cannot be deleted');
                    END;
                CREATE TRIGGER IF NOT EXISTS worktree_test_runs_no_update
                    BEFORE UPDATE ON worktree_test_runs
                    BEGIN
                        SELECT RAISE(ABORT, 'worktree test runs are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS worktree_test_runs_no_delete
                    BEFORE DELETE ON worktree_test_runs
                    BEGIN
                        SELECT RAISE(ABORT, 'worktree test runs cannot be deleted');
                    END;
                CREATE TRIGGER IF NOT EXISTS worktree_jobs_identity_immutable
                    BEFORE UPDATE ON worktree_jobs
                    WHEN NEW.job_id IS NOT OLD.job_id
                      OR NEW.profile IS NOT OLD.profile
                      OR NEW.repository IS NOT OLD.repository
                      OR NEW.worktree IS NOT OLD.worktree
                      OR NEW.branch IS NOT OLD.branch
                      OR NEW.base_commit IS NOT OLD.base_commit
                      OR NEW.task_hash IS NOT OLD.task_hash
                      OR NEW.delivery_mode IS NOT OLD.delivery_mode
                      OR NEW.created_at IS NOT OLD.created_at
                      OR NEW.expires_at IS NOT OLD.expires_at
                    BEGIN
                        SELECT RAISE(ABORT, 'worktree job identity is immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS worktree_jobs_no_delete
                    BEFORE DELETE ON worktree_jobs
                    BEGIN
                        SELECT RAISE(ABORT, 'worktree job records cannot be deleted');
                    END;
                CREATE TRIGGER IF NOT EXISTS worktree_jobs_valid_transition
                    BEFORE UPDATE OF status ON worktree_jobs
                    WHEN NOT (
                        (OLD.status = 'created' AND NEW.status IN (
                            'active', 'failed', 'abandoned'
                        ))
                        OR (OLD.status = 'active' AND NEW.status IN (
                            'tested', 'failed', 'abandoned'
                        ))
                        OR (OLD.status = 'tested' AND NEW.status IN (
                            'proposed', 'failed', 'abandoned'
                        ))
                        OR (OLD.status = 'proposed' AND NEW.status IN (
                            'delivered', 'failed', 'abandoned'
                        ))
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid worktree status transition');
                    END;
                """
            )
        os.chmod(self.state_path, 0o600)

    def create(
        self,
        *,
        repository: Path,
        profile: str,
        task: str,
        delivery_mode: DeliveryMode = DeliveryMode.manual,
    ) -> WorktreeJob:
        requested = Path(os.path.realpath(repository))
        repository_root = Path(
            _run_git(["-C", str(requested), "rev-parse", "--show-toplevel"])
        )
        self._validate_repository_state(repository_root)
        if self.root == repository_root or self.root.is_relative_to(repository_root):
            raise WorktreeError("managed worktree root must be outside the repository")
        base_commit = _run_git(
            ["-C", str(repository_root), "rev-parse", "HEAD"]
        )
        job_id = uuid.uuid4().hex
        branch = (
            f"{self.branch_prefix}/{_safe_branch_segment(profile)}/{job_id}"
        )
        worktree = self.root / job_id
        if worktree.exists():
            raise WorktreeError("managed worktree path already exists")
        now = datetime.now(timezone.utc)
        record = {
            "job_id": job_id,
            "profile": profile,
            "repository": str(repository_root),
            "worktree": str(worktree),
            "branch": branch,
            "base_commit": base_commit,
            "task_hash": "sha256:" + hashlib.sha256(task.encode()).hexdigest(),
            "delivery_mode": delivery_mode.value,
            "status": WorktreeStatus.created.value,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.ttl_sec)).isoformat(),
        }
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO repository_locks (
                        repository, job_id, acquired_at
                    ) VALUES (?, ?, ?)
                    """,
                    (str(repository_root), job_id, now.isoformat()),
                )
        except sqlite3.IntegrityError as exc:
            raise WorktreeError(
                "repository already has an active worktree job"
            ) from exc
        try:
            _run_git(
                [
                    "-C",
                    str(repository_root),
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree),
                    base_commit,
                ]
            )
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO worktree_jobs (
                        job_id, profile, repository, worktree, branch,
                        base_commit, task_hash, delivery_mode, status,
                        created_at, expires_at
                    ) VALUES (
                        :job_id, :profile, :repository, :worktree, :branch,
                        :base_commit, :task_hash, :delivery_mode, :status,
                        :created_at, :expires_at
                    )
                    """,
                    record,
                )
        except (sqlite3.Error, WorktreeError) as exc:
            self._rollback_creation(
                repository=repository_root,
                worktree=worktree,
                branch=branch,
                job_id=job_id,
            )
            if isinstance(exc, WorktreeError):
                raise
            raise WorktreeError("could not persist worktree job") from exc
        return WorktreeJob.model_validate(record)

    def transition(
        self,
        job_id: str,
        *,
        profile: str,
        status: WorktreeStatus,
    ) -> WorktreeJob:
        current = self.get(job_id, profile=profile)
        if status not in _STATUS_TRANSITIONS.get(current.status, set()):
            raise WorktreeError(
                f"invalid worktree status transition: {current.status.value} -> {status.value}"
            )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE worktree_jobs
                    SET status = ?
                    WHERE job_id = ? AND profile = ? AND status = ?
                    """,
                    (status.value, job_id, profile, current.status.value),
                )
                if cursor.rowcount != 1:
                    raise WorktreeError("worktree status transition lost a race")
                if status in _TERMINAL_STATUSES:
                    connection.execute(
                        """
                        DELETE FROM repository_locks
                        WHERE repository = ? AND job_id = ?
                        """,
                        (str(current.repository), job_id),
                    )
        except sqlite3.IntegrityError as exc:
            raise WorktreeError("worktree status transition was rejected") from exc
        return self.get(job_id, profile=profile)

    def validate_checkout(self, job_id: str, *, profile: str) -> Path:
        job = self.get(job_id, profile=profile)
        worktree = Path(os.path.realpath(job.worktree))
        if worktree == self.root or not worktree.is_relative_to(self.root):
            raise WorktreeError("worktree path escaped the managed root")
        if not worktree.is_dir():
            raise WorktreeError("managed worktree directory is missing")
        actual_root = Path(
            _run_git(["-C", str(worktree), "rev-parse", "--show-toplevel"])
        )
        if Path(os.path.realpath(actual_root)) != worktree:
            raise WorktreeError("managed worktree Git root does not match")
        actual_head = _run_git(["-C", str(worktree), "rev-parse", "HEAD"])
        if actual_head != job.base_commit:
            raise WorktreeError("managed worktree base commit changed before execution")
        actual_branch = _run_git(
            ["-C", str(worktree), "branch", "--show-current"]
        )
        if actual_branch != job.branch:
            raise WorktreeError("managed worktree branch does not match")
        return worktree

    def mark_proposed(
        self,
        job_id: str,
        *,
        profile: str,
        proposal_id: str,
        proposal_sha256: str,
    ) -> WorktreeJob:
        current = self.get(job_id, profile=profile)
        if current.status != WorktreeStatus.tested:
            raise WorktreeError("worktree job is not ready for proposal creation")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO worktree_proposals (
                        job_id, proposal_id, proposal_sha256, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        proposal_id,
                        proposal_sha256,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                cursor = connection.execute(
                    """
                    UPDATE worktree_jobs
                    SET status = ?
                    WHERE job_id = ? AND profile = ? AND status = ?
                    """,
                    (
                        WorktreeStatus.proposed.value,
                        job_id,
                        profile,
                        WorktreeStatus.tested.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise WorktreeError("worktree proposal transition lost a race")
        except sqlite3.IntegrityError as exc:
            raise WorktreeError("worktree proposal link was rejected") from exc
        return self.get(job_id, profile=profile)

    def find_by_proposal(
        self,
        proposal_id: str,
        *,
        profile: str,
    ) -> tuple[WorktreeJob, dict[str, str]] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT j.*, p.proposal_id, p.proposal_sha256
                FROM worktree_jobs AS j
                JOIN worktree_proposals AS p ON p.job_id = j.job_id
                WHERE p.proposal_id = ? AND j.profile = ?
                """,
                (proposal_id, profile),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        link = {
            "proposal_id": record.pop("proposal_id"),
            "proposal_sha256": record.pop("proposal_sha256"),
        }
        return WorktreeJob.model_validate(record), link

    def cleanup(
        self,
        job_id: str,
        *,
        profile: str,
    ) -> WorktreeCleanupResult:
        job = self.get(job_id, profile=profile)
        if job.status not in _TERMINAL_STATUSES:
            raise WorktreeError("only terminal worktree jobs can be cleaned up")
        worktree = Path(os.path.realpath(job.worktree))
        if worktree == self.root or not worktree.is_relative_to(self.root):
            raise WorktreeError("worktree path escaped the managed root")
        worktree_removed = not worktree.exists()
        branch_removed = False
        error: str | None = None
        try:
            if not worktree_removed:
                _run_git(
                    [
                        "-C",
                        str(job.repository),
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                    ]
                )
                worktree_removed = not worktree.exists()
                if not worktree_removed:
                    raise WorktreeError("managed worktree directory still exists")
            else:
                _run_git(
                    ["-C", str(job.repository), "worktree", "prune"]
                )
            branches = set(
                _run_git(
                    [
                        "-C",
                        str(job.repository),
                        "for-each-ref",
                        "--format=%(refname:short)",
                        "refs/heads",
                    ]
                ).splitlines()
            )
            if job.branch in branches:
                _run_git(
                    ["-C", str(job.repository), "branch", "-D", job.branch]
                )
            branch_removed = job.branch not in set(
                _run_git(
                    [
                        "-C",
                        str(job.repository),
                        "for-each-ref",
                        "--format=%(refname:short)",
                        "refs/heads",
                    ]
                ).splitlines()
            )
            if not branch_removed:
                raise WorktreeError("managed worktree branch still exists")
        except WorktreeError as exc:
            error = str(exc)
        result = WorktreeCleanupResult(
            job_id=job_id,
            ok=worktree_removed and branch_removed and error is None,
            worktree_removed=worktree_removed,
            branch_removed=branch_removed,
            error=error,
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO worktree_cleanup_runs (
                        cleanup_id, job_id, ok, worktree_removed,
                        branch_removed, error, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        job_id,
                        result.ok,
                        result.worktree_removed,
                        result.branch_removed,
                        result.error,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        except sqlite3.Error as exc:
            raise WorktreeError("could not persist worktree cleanup result") from exc
        return result

    def get(self, job_id: str, *, profile: str) -> WorktreeJob:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM worktree_jobs
                WHERE job_id = ? AND profile = ?
                """,
                (job_id, profile),
            ).fetchone()
        if row is None:
            raise WorktreeError("worktree job not found")
        return WorktreeJob.model_validate(dict(row))

    def list(self, *, profile: str, limit: int = 20) -> list[WorktreeJob]:
        safe_limit = max(1, min(limit, 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM worktree_jobs
                WHERE profile = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (profile, safe_limit),
            ).fetchall()
        return [WorktreeJob.model_validate(dict(row)) for row in rows]

    def _validate_repository_state(self, repository: Path) -> None:
        status = _run_git(
            [
                "-C",
                str(repository),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ]
        )
        if status:
            raise WorktreeError("repository has uncommitted or untracked changes")
        in_progress_paths = (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "BISECT_LOG",
            "rebase-apply",
            "rebase-merge",
        )
        for marker in in_progress_paths:
            git_path = Path(
                _run_git(
                    ["-C", str(repository), "rev-parse", "--git-path", marker]
                )
            )
            if not git_path.is_absolute():
                git_path = repository / git_path
            if git_path.exists():
                raise WorktreeError(f"repository operation is in progress: {marker}")

    def _rollback_creation(
        self,
        *,
        repository: Path,
        worktree: Path,
        branch: str,
        job_id: str,
    ) -> None:
        if worktree.exists():
            try:
                _run_git(
                    [
                        "-C",
                        str(repository),
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                    ]
                )
            except WorktreeError:
                pass
        try:
            _run_git(["-C", str(repository), "branch", "-D", branch])
        except WorktreeError:
            pass
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM repository_locks WHERE job_id = ?",
                (job_id,),
            )
