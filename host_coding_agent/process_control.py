from __future__ import annotations

import contextvars
import os
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


_CURRENT_JOB_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "host_coding_agent_current_job_id",
    default=None,
)


@dataclass(frozen=True)
class ProcessKillResult:
    job_id: str
    process_count: int
    pids: list[int]
    process_killed: bool
    process_kill_guaranteed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "process_count": self.process_count,
            "pids": self.pids,
            "process_killed": self.process_killed,
            "process_kill_guaranteed": self.process_kill_guaranteed,
        }


class ProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pids_by_job: dict[str, set[int]] = {}

    @contextmanager
    def job_context(self, job_id: str) -> Iterator[None]:
        token = _CURRENT_JOB_ID.set(job_id)
        try:
            yield
        finally:
            _CURRENT_JOB_ID.reset(token)

    @contextmanager
    def register_current_process(self, pid: int) -> Iterator[None]:
        job_id = _CURRENT_JOB_ID.get()
        if job_id is None:
            yield
            return
        self.register(job_id, pid)
        try:
            yield
        finally:
            self.unregister(job_id, pid)

    def register(self, job_id: str, pid: int) -> None:
        with self._lock:
            self._pids_by_job.setdefault(job_id, set()).add(pid)

    def unregister(self, job_id: str, pid: int) -> None:
        with self._lock:
            pids = self._pids_by_job.get(job_id)
            if pids is None:
                return
            pids.discard(pid)
            if not pids:
                self._pids_by_job.pop(job_id, None)

    def terminate_job(self, job_id: str) -> ProcessKillResult:
        with self._lock:
            pids = sorted(self._pids_by_job.get(job_id, set()))
        for pid in pids:
            self._terminate_group(pid)
        return ProcessKillResult(
            job_id=job_id,
            process_count=len(pids),
            pids=pids,
            process_killed=bool(pids),
            process_kill_guaranteed=bool(pids),
        )

    @staticmethod
    def _terminate_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
            time.sleep(0.2)
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


process_registry = ProcessRegistry()
