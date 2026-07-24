from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from host_coding_agent.jobs import JobError, JobStore


def _wait_for_status(
    store: JobStore,
    job_id: str,
    profile: str,
    expected: str,
    *,
    timeout: float = 2.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get(job_id, profile)
        if job["status"] == expected:
            return job
        time.sleep(0.01)
    pytest.fail(f"job {job_id} did not reach {expected!r} within {timeout}s")


def test_submit_returns_immediately_and_records_events_with_pagination(tmp_path: Path):
    store = JobStore(tmp_path / "state" / "jobs.db", max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def worker(emit):
        started.set()
        assert release.wait(1)
        emit("prepare", "Preparing", {"count": 2})
        emit("execute", "Executing")
        return {"ok": True, "items": [1, 2]}

    try:
        submitted = store.submit("dev-bot", "delivery", {"request_id": "r1"}, worker)
        assert submitted["status"] == "queued"
        assert submitted["stage"] == "queued"
        assert submitted["started_at"] is None
        assert started.wait(1)

        running = store.get(submitted["job_id"], "dev-bot")
        assert running["status"] == "running"
        assert running["started_at"] is not None
        release.set()
        completed = _wait_for_status(
            store, submitted["job_id"], "dev-bot", "succeeded"
        )

        assert completed["stage"] == "succeeded"
        assert completed["metadata"] == {"request_id": "r1"}
        assert completed["result"] == {"items": [1, 2], "ok": True}
        assert completed["error"] is None
        assert completed["finished_at"] is not None
        assert completed["created_at"] <= completed["started_at"]
        assert completed["started_at"] <= completed["finished_at"]

        first_page = store.events(
            submitted["job_id"], "dev-bot", after=0, limit=2
        )
        assert [event["sequence"] for event in first_page["events"]] == [1, 2]
        assert [event["stage"] for event in first_page["events"]] == [
            "queued",
            "running",
        ]
        assert first_page["next_after"] == 2
        assert first_page["has_more"] is True

        second_page = store.events(
            submitted["job_id"],
            "dev-bot",
            after=first_page["next_after"],
            limit=10,
        )
        assert [event["sequence"] for event in second_page["events"]] == [3, 4, 5]
        assert [event["stage"] for event in second_page["events"]] == [
            "prepare",
            "execute",
            "succeeded",
        ]
        assert second_page["events"][0]["details"] == {"count": 2}
        assert second_page["next_after"] == 5
        assert second_page["has_more"] is False

        empty_page = store.events(submitted["job_id"], "dev-bot", after=5)
        assert empty_page == {"events": [], "next_after": 5, "has_more": False}
    finally:
        release.set()
        store.shutdown()


def test_jobs_are_profile_scoped_and_listed_newest_first(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db", max_workers=2)
    gate = threading.Event()

    def worker(_emit):
        assert gate.wait(1)
        return {"done": True}

    try:
        first = store.submit("alpha", "one", {}, worker)
        second = store.submit("alpha", "two", {}, worker)
        other = store.submit("beta", "three", {}, worker)

        assert [job["job_id"] for job in store.list("alpha")] == [
            second["job_id"],
            first["job_id"],
        ]
        assert [job["job_id"] for job in store.list("beta")] == [other["job_id"]]
        assert len(store.list("alpha", limit=1)) == 1
        with pytest.raises(JobError, match="not found"):
            store.get(first["job_id"], "beta")
        with pytest.raises(JobError, match="not found"):
            store.events(first["job_id"], "beta")
    finally:
        gate.set()
        store.shutdown()


def test_worker_failure_is_redacted_and_invalid_results_fail(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db", max_workers=2)

    def secret_failure(_emit):
        raise RuntimeError("password=highly-secret-value")

    def invalid_result(_emit):
        return {"value": object()}

    try:
        failed = store.submit("dev", "secret", {}, secret_failure)
        invalid = store.submit("dev", "invalid", {}, invalid_result)
        failed_job = _wait_for_status(store, failed["job_id"], "dev", "failed")
        invalid_job = _wait_for_status(store, invalid["job_id"], "dev", "failed")

        assert "highly-secret-value" not in failed_job["error"]
        assert "[REDACTED]" in failed_job["error"]
        assert failed_job["result"] is None
        assert "JSON-serializable dict" in invalid_job["error"]
        assert store.events(failed["job_id"], "dev")["events"][-1]["stage"] == "failed"
    finally:
        store.shutdown()


def test_cancel_marks_running_job_terminal_and_preserves_cancel_event(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db", max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def worker(emit):
        started.set()
        assert release.wait(1)
        emit("late", "Late event")
        return {"ok": True}

    try:
        submitted = store.submit("dev", "long", {}, worker)
        assert started.wait(1)

        cancelled = store.cancel(
            submitted["job_id"],
            "dev",
            reason="user requested cancellation",
        )

        assert cancelled["status"] == "failed"
        assert cancelled["stage"] == "cancelled"
        assert cancelled["cancelled"] is True
        assert cancelled["error"] == "user requested cancellation"
        release.set()
        time.sleep(0.05)
        final = store.get(submitted["job_id"], "dev")
        assert final["status"] == "failed"
        assert final["stage"] == "cancelled"
        events = store.events(submitted["job_id"], "dev")["events"]
        assert [event["stage"] for event in events] == [
            "queued",
            "running",
            "cancelled",
        ]
        assert events[-1]["details"]["process_kill_guaranteed"] is False
    finally:
        release.set()
        store.shutdown()


@pytest.mark.parametrize(
    "metadata",
    [object(), {"bad": object()}, {"number": float("nan")}],
)
def test_submit_rejects_non_json_metadata(tmp_path: Path, metadata):
    store = JobStore(tmp_path / "jobs.db")
    try:
        with pytest.raises(JobError, match="metadata must be a JSON-serializable dict"):
            store.submit("dev", "kind", metadata, lambda _emit: {})
        assert store.list("dev") == []
    finally:
        store.shutdown()


def test_initialization_recovers_interrupted_jobs(tmp_path: Path):
    path = tmp_path / "private" / "jobs.db"
    first_store = JobStore(path)
    first_store.shutdown()
    with sqlite3.connect(path) as connection:
        for job_id, status in (("queued-job", "queued"), ("running-job", "running")):
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, profile, kind, metadata_json, status, stage,
                    created_at, started_at, finished_at, updated_at,
                    result_json, error
                ) VALUES (?, 'dev', 'test', '{}', ?, ?, ?, NULL, NULL, ?, NULL, NULL)
                """,
                (job_id, status, status, "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
            )

    recovered_store = JobStore(path)
    try:
        for job_id in ("queued-job", "running-job"):
            job = recovered_store.get(job_id, "dev")
            assert job["status"] == "failed"
            assert job["stage"] == "interrupted"
            assert job["finished_at"] is not None
            assert job["error"] == "job interrupted by process restart"
            events = recovered_store.events(job_id, "dev")["events"]
            assert [(event["sequence"], event["stage"]) for event in events] == [
                (1, "interrupted")
            ]
    finally:
        recovered_store.shutdown()


def test_database_and_parent_permissions_are_restricted(tmp_path: Path):
    path = tmp_path / "state" / "jobs.db"
    store = JobStore(path)
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        store.shutdown()
