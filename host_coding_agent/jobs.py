from __future__ import annotations

import json
import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from host_coding_agent.security import redact


class JobError(ValueError):
    pass


Emit = Callable[[str, str, dict[str, Any] | None], None]
Worker = Callable[[Emit], dict[str, Any]]


class JobStore:
    """Profile-scoped SQLite store for jobs run by a background thread pool."""

    def __init__(self, state_path: Path, *, max_workers: int = 4):
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_path.parent, 0o700)
        self._initialize()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="host-coding-agent-job",
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.state_path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 10000")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                    stage TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_profile_created
                    ON jobs(profile, created_at DESC, job_id DESC);
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    sequence INTEGER NOT NULL CHECK (sequence > 0),
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS job_events_job_sequence
                    ON job_events(job_id, sequence);
                """
            )
            interrupted = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE status IN ('queued', 'running')
                ORDER BY job_id
                """
            ).fetchall()
            for row in interrupted:
                now = self._now()
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', stage = 'interrupted', finished_at = ?,
                        updated_at = ?, error = ?
                    WHERE job_id = ? AND status IN ('queued', 'running')
                    """,
                    (now, now, "job interrupted by process restart", row["job_id"]),
                )
                self._insert_event(
                    connection,
                    job_id=row["job_id"],
                    stage="interrupted",
                    message="Job interrupted by process restart",
                    details=None,
                    created_at=now,
                )
        os.chmod(self.state_path, 0o600)

    def submit(
        self,
        profile: str,
        kind: str,
        metadata: dict[str, Any],
        worker: Worker,
    ) -> dict[str, Any]:
        if not isinstance(profile, str) or not profile:
            raise JobError("profile must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise JobError("kind must be a non-empty string")
        if not callable(worker):
            raise JobError("worker must be callable")
        metadata_value, metadata_json = self._validated_dict(metadata, "metadata")
        now = self._now()
        record = {
            "job_id": uuid.uuid4().hex,
            "profile": profile,
            "kind": kind,
            "metadata": metadata_value,
            "status": "queued",
            "stage": "queued",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
            "result": None,
            "error": None,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, profile, kind, metadata_json, status, stage,
                    created_at, started_at, finished_at, updated_at,
                    result_json, error
                ) VALUES (?, ?, ?, ?, 'queued', 'queued', ?, NULL, NULL, ?, NULL, NULL)
                """,
                (
                    record["job_id"],
                    profile,
                    kind,
                    metadata_json,
                    now,
                    now,
                ),
            )
            self._insert_event(
                connection,
                job_id=record["job_id"],
                stage="queued",
                message="Job queued",
                details=None,
                created_at=now,
            )
        self._executor.submit(self._run, record["job_id"], worker)
        return record

    def get(self, job_id: str, profile: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND profile = ?",
                (job_id, profile),
            ).fetchone()
        if row is None:
            raise JobError("job not found")
        return self._deserialize_job(row)

    def list(self, profile: str, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE profile = ?
                ORDER BY created_at DESC, job_id DESC
                LIMIT ?
                """,
                (profile, safe_limit),
            ).fetchall()
        return [self._deserialize_job(row) for row in rows]

    def events(
        self,
        job_id: str,
        profile: str,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        if after < 0:
            raise JobError("after must not be negative")
        safe_limit = max(1, min(limit, 1000))
        with self._connect() as connection:
            owned = connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ? AND profile = ?",
                (job_id, profile),
            ).fetchone()
            if owned is None:
                raise JobError("job not found")
            rows = connection.execute(
                """
                SELECT sequence, stage, message, details_json, created_at
                FROM job_events
                WHERE job_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (job_id, after, safe_limit + 1),
            ).fetchall()
        has_more = len(rows) > safe_limit
        selected = rows[:safe_limit]
        records = []
        for row in selected:
            event = dict(row)
            raw_details = event.pop("details_json")
            event["details"] = json.loads(raw_details) if raw_details is not None else None
            records.append(event)
        next_after = records[-1]["sequence"] if records else after
        return {"events": records, "next_after": next_after, "has_more": has_more}

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    def _run(self, job_id: str, worker: Worker) -> None:
        started_at = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'running', stage = 'running', started_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (started_at, started_at, job_id),
            )
            if cursor.rowcount != 1:
                return
            self._insert_event(
                connection,
                job_id=job_id,
                stage="running",
                message="Job started",
                details=None,
                created_at=started_at,
            )

        def emit(stage: str, message: str, details: dict[str, Any] | None = None) -> None:
            if not isinstance(stage, str) or not stage:
                raise JobError("event stage must be a non-empty string")
            if not isinstance(message, str):
                raise JobError("event message must be a string")
            details_json = None
            if details is not None:
                _, details_json = self._validated_dict(details, "event details")
            created_at = self._now()
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE jobs SET stage = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (stage, created_at, job_id),
                )
                if cursor.rowcount != 1:
                    raise JobError("job is no longer running")
                self._insert_event(
                    connection,
                    job_id=job_id,
                    stage=stage,
                    message=message,
                    details_json=details_json,
                    created_at=created_at,
                )

        try:
            result = worker(emit)
            _, result_json = self._validated_dict(result, "worker result")
        except Exception as exc:
            self._finish_failed(job_id, exc)
            return

        finished_at = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', stage = 'succeeded', finished_at = ?,
                    updated_at = ?, result_json = ?, error = NULL
                WHERE job_id = ? AND status = 'running'
                """,
                (finished_at, finished_at, result_json, job_id),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                stage="succeeded",
                message="Job succeeded",
                details=None,
                created_at=finished_at,
            )

    def _finish_failed(self, job_id: str, exc: Exception) -> None:
        error, _ = redact(f"{type(exc).__name__}: {exc}")
        finished_at = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', stage = 'failed', finished_at = ?,
                    updated_at = ?, result_json = NULL, error = ?
                WHERE job_id = ? AND status = 'running'
                """,
                (finished_at, finished_at, error, job_id),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                stage="failed",
                message="Job failed",
                details=None,
                created_at=finished_at,
            )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        stage: str,
        message: str,
        details: dict[str, Any] | None = None,
        details_json: str | None = None,
        created_at: str,
    ) -> None:
        if details_json is None and details is not None:
            details_json = json.dumps(details, sort_keys=True, allow_nan=False)
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, sequence, stage, message, details_json, created_at
            )
            SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?, ?, ?, ?
            FROM job_events WHERE job_id = ?
            """,
            (job_id, stage, message, details_json, created_at, job_id),
        )

    @staticmethod
    def _validated_dict(value: Any, name: str) -> tuple[dict[str, Any], str]:
        if not isinstance(value, dict):
            raise JobError(f"{name} must be a JSON-serializable dict")
        try:
            encoded = json.dumps(value, sort_keys=True, allow_nan=False)
            decoded = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise JobError(f"{name} must be a JSON-serializable dict") from exc
        return decoded, encoded

    @staticmethod
    def _deserialize_job(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["metadata"] = json.loads(record.pop("metadata_json"))
        raw_result = record.pop("result_json")
        record["result"] = json.loads(raw_result) if raw_result is not None else None
        return record

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
