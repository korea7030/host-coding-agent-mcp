from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import (
    ProjectPolicy,
    TestCommandResult,
    WorktreeConfig,
    WorktreeStatus,
    WorktreeTestResult,
)
from .security import redact
from .worktrees import WorktreeManager


class WorktreeTestError(ValueError):
    pass


def _load_policy(job, config: WorktreeConfig) -> ProjectPolicy | None:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(job.repository),
                "show",
                f"{job.base_commit}:{config.policy_file}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeTestError(f"could not read project test policy: {exc}") from exc
    if completed.returncode != 0:
        if config.require_tests:
            raise WorktreeTestError(
                f"required test policy is missing at base commit: {config.policy_file}"
            )
        return None
    try:
        data = yaml.safe_load(completed.stdout)
        policy = ProjectPolicy.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise WorktreeTestError(f"invalid project test policy: {exc}") from exc
    for command in policy.tests.commands:
        _validate_command(command)
    return policy


def _validate_command(command: list[str]) -> None:
    if not command or any(not isinstance(arg, str) or "\0" in arg for arg in command):
        raise WorktreeTestError("test commands must be non-empty argv string arrays")
    executable = command[0]
    if not executable.strip():
        raise WorktreeTestError("test command executable must not be empty")
    if os.path.isabs(executable):
        raise WorktreeTestError("absolute test command executables are not allowed")
    if "/" in executable:
        candidate = Path(executable)
        if not executable.startswith("./") or ".." in candidate.parts:
            raise WorktreeTestError(
                "relative test command executables must stay inside the worktree"
            )


def _run_command(
    command: list[str],
    *,
    worktree: Path,
    timeout_sec: int,
    max_output_chars: int,
    command_index: int,
) -> TestCommandResult:
    started = time.monotonic()
    timed_out = False
    returncode: int | None = None
    stdout = ""
    stderr = ""
    env = {
        key: value
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
        if (value := os.environ.get(key))
    }
    env.update({"CI": "1", "HOME": str(worktree)})
    try:
        process = subprocess.Popen(
            command,
            cwd=worktree,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        returncode = process.returncode
    except OSError as exc:
        stderr = f"test command could not start: {exc}"
    stdout, stdout_changed = redact(stdout, max_output_chars)
    stderr, stderr_changed = redact(stderr, max_output_chars)
    return TestCommandResult(
        run_id=uuid.uuid4().hex,
        command_index=command_index,
        command=command,
        ok=returncode == 0 and not timed_out,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_sec=time.monotonic() - started,
        timed_out=timed_out,
        redacted=stdout_changed or stderr_changed,
    )


def _record_result(
    manager: WorktreeManager,
    job_id: str,
    result: TestCommandResult,
) -> None:
    try:
        with sqlite3.connect(manager.state_path) as connection:
            connection.execute(
                """
                INSERT INTO worktree_test_runs (
                    run_id, job_id, command_index, command_json, ok, returncode,
                    stdout, stderr, duration_sec, timed_out, redacted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    job_id,
                    result.command_index,
                    json.dumps(result.command),
                    result.ok,
                    result.returncode,
                    result.stdout,
                    result.stderr,
                    result.duration_sec,
                    result.timed_out,
                    result.redacted,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except sqlite3.Error as exc:
        raise WorktreeTestError("could not persist test result") from exc


def run_managed_worktree_tests(
    *,
    manager: WorktreeManager,
    job_id: str,
    profile: str,
    config: WorktreeConfig,
) -> WorktreeTestResult:
    job = manager.get(job_id, profile=profile)
    if job.status != WorktreeStatus.active:
        raise WorktreeTestError("worktree job is not ready for test execution")
    worktree = manager.validate_checkout(job_id, profile=profile)
    results: list[TestCommandResult] = []
    try:
        policy = _load_policy(job, config)
        if policy is None:
            manager.transition(
                job_id,
                profile=profile,
                status=WorktreeStatus.tested,
            )
            return WorktreeTestResult(
                job_id=job_id,
                ok=True,
                policy_commit=job.base_commit,
                policy_file=config.policy_file,
            )
        timeout_sec = min(policy.tests.timeout_sec, config.max_test_timeout_sec)
        for index, command in enumerate(policy.tests.commands):
            result = _run_command(
                command,
                worktree=worktree,
                timeout_sec=timeout_sec,
                max_output_chars=config.max_test_output_chars,
                command_index=index,
            )
            _record_result(manager, job_id, result)
            results.append(result)
            if not result.ok:
                raise WorktreeTestError(f"test command {index} failed")
    except WorktreeTestError as exc:
        manager.transition(
            job_id,
            profile=profile,
            status=WorktreeStatus.failed,
        )
        return WorktreeTestResult(
            job_id=job_id,
            ok=False,
            policy_commit=job.base_commit,
            policy_file=config.policy_file,
            results=results,
            error=str(exc),
        )
    manager.transition(
        job_id,
        profile=profile,
        status=WorktreeStatus.tested,
    )
    return WorktreeTestResult(
        job_id=job_id,
        ok=True,
        policy_commit=job.base_commit,
        policy_file=config.policy_file,
        results=results,
    )
