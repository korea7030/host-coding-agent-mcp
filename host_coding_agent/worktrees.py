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

from .models import DeliveryMode, WorktreeJob, WorktreeStatus


class WorktreeError(ValueError):
    pass


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
        except sqlite3.Error as exc:
            _run_git(
                [
                    "-C",
                    str(repository_root),
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                ]
            )
            raise WorktreeError("could not persist worktree job") from exc
        return WorktreeJob.model_validate(record)

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
