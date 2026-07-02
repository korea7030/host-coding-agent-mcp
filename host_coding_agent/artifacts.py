from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .models import AgentName


class ArtifactError(ValueError):
    pass


def _format_diff_path(path: str) -> str:
    return json.dumps(path) if any(char.isspace() for char in path) else path


def normalize_diff_text(diff_text: str, cwd: Path | None = None) -> str:
    """Normalize transport damage without changing non-blank source lines."""
    normalized_lines = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            fields = shlex.split(line)
            if len(fields) != 4:
                raise ArtifactError(f"invalid diff header: {line}")
            old_path = _normalize_diff_path(fields[2], cwd)
            new_path = _normalize_diff_path(fields[3], cwd)
            if old_path is None or new_path is None:
                raise ArtifactError(f"invalid diff header: {line}")
            normalized_lines.append(
                "diff --git "
                f"{_format_diff_path('a/' + old_path)} "
                f"{_format_diff_path('b/' + new_path)}"
            )
        elif line.startswith(("--- ", "+++ ")):
            prefix = line[:3]
            value = line[4:].split("\t", 1)[0]
            path = _normalize_diff_path(value, cwd)
            if path is None:
                normalized_lines.append(f"{prefix} /dev/null")
            else:
                side = "a/" if prefix == "---" else "b/"
                normalized_lines.append(
                    f"{prefix} {_format_diff_path(side + path)}"
                )
        elif (
            line.startswith("+")
            and not line.startswith("+++")
            and not line[1:].strip()
        ):
            normalized_lines.append("+")
        else:
            normalized_lines.append(line)
    return "\n".join(normalized_lines) + "\n" if normalized_lines else ""


def validate_patch_preflight(cwd: Path, diff_text: str) -> None:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(cwd),
                "apply",
                "--check",
                "--recount",
                "--whitespace=error-all",
                "-",
            ],
            input=diff_text,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactError(f"proposal preflight could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ArtifactError(f"proposal failed git apply preflight: {detail[:1000]}")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _normalize_diff_path(value: str, cwd: Path | None = None) -> str | None:
    candidate = value.strip()
    if not candidate or candidate == "/dev/null":
        return None
    if candidate.startswith(("a/", "b/")):
        candidate = candidate[2:]
    path = PurePosixPath(candidate)
    if path.is_absolute():
        if cwd is None:
            raise ArtifactError(f"unsafe diff path: {value}")
        root = Path(os.path.realpath(cwd))
        absolute = Path(os.path.realpath(candidate))
        if absolute == root or not absolute.is_relative_to(root):
            raise ArtifactError(f"unsafe diff path: {value}")
        path = PurePosixPath(absolute.relative_to(root).as_posix())
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactError(f"unsafe diff path: {value}")
    return path.as_posix()


def extract_diff_paths(diff_text: str, cwd: Path | None = None) -> list[str]:
    paths: set[str] = set()
    for line in diff_text.splitlines():
        values: list[str] = []
        if line.startswith("diff --git "):
            try:
                fields = shlex.split(line)
            except ValueError as exc:
                raise ArtifactError(f"invalid diff header: {line}") from exc
            if len(fields) != 4:
                raise ArtifactError(f"invalid diff header: {line}")
            values.extend(fields[2:4])
        elif line.startswith(("--- ", "+++ ")):
            # Unified diff timestamps, when present, are tab-separated.
            values.append(line[4:].split("\t", 1)[0])
        for value in values:
            normalized = _normalize_diff_path(value, cwd)
            if normalized is not None:
                paths.add(normalized)
    if not paths:
        raise ArtifactError("diff contains no file paths")
    return sorted(paths)


def _reject_symlink_components(root: Path, relative_path: str) -> None:
    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ArtifactError(f"diff path contains a symlink: {relative_path}")


def snapshot_base_files(cwd: Path, diff_text: str) -> dict[str, str | None]:
    root = Path(os.path.realpath(cwd))
    snapshots: dict[str, str | None] = {}
    for relative_path in extract_diff_paths(diff_text, root):
        _reject_symlink_components(root, relative_path)
        target = Path(os.path.realpath(root / relative_path))
        if target != root and not target.is_relative_to(root):
            raise ArtifactError(f"diff path escapes workspace: {relative_path}")
        if not target.exists():
            snapshots[relative_path] = None
            continue
        if not target.is_file():
            raise ArtifactError(f"diff path is not a regular file: {relative_path}")
        snapshots[relative_path] = _sha256_bytes(target.read_bytes())
    return snapshots


def _git_head(cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


class ProposalStore:
    def __init__(self, path: Path, *, ttl_sec: int, max_diff_chars: int):
        self.path = path
        self.ttl_sec = ttl_sec
        self.max_diff_chars = max_diff_chars
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    task_hash TEXT NOT NULL,
                    diff_sha256 TEXT NOT NULL,
                    diff_text TEXT NOT NULL,
                    base_files_json TEXT NOT NULL,
                    git_head TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS proposals_profile_created
                    ON proposals(profile, created_at DESC);
                CREATE TRIGGER IF NOT EXISTS proposals_immutable_update
                    BEFORE UPDATE ON proposals
                    BEGIN
                        SELECT RAISE(ABORT, 'proposal artifacts are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS proposals_immutable_delete
                    BEFORE DELETE ON proposals
                    BEGIN
                        SELECT RAISE(ABORT, 'proposal artifacts are immutable');
                    END;
                """
            )
        os.chmod(self.path, 0o600)

    def create(
        self,
        *,
        profile: str,
        cwd: Path,
        agent: AgentName,
        task: str,
        diff_text: str,
    ) -> dict[str, Any]:
        if not diff_text.strip():
            raise ArtifactError("cannot store an empty diff")
        canonical_cwd = Path(os.path.realpath(cwd))
        diff_text = normalize_diff_text(diff_text, canonical_cwd)
        if len(diff_text) > self.max_diff_chars:
            raise ArtifactError("diff exceeds configured artifact size limit")
        base_files = snapshot_base_files(canonical_cwd, diff_text)
        validate_patch_preflight(canonical_cwd, diff_text)
        diff_sha256 = _sha256_bytes(diff_text.encode())
        now = datetime.now(timezone.utc)
        record = {
            "proposal_id": uuid.uuid4().hex,
            "profile": profile,
            "cwd": str(canonical_cwd),
            "agent": agent.value,
            "task_hash": _sha256_bytes(task.encode()),
            "diff_sha256": diff_sha256,
            "diff_text": diff_text,
            "base_files_json": json.dumps(base_files, sort_keys=True),
            "git_head": _git_head(canonical_cwd),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.ttl_sec)).isoformat(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO proposals (
                    proposal_id, profile, cwd, agent, task_hash, diff_sha256,
                    diff_text, base_files_json, git_head, created_at, expires_at
                ) VALUES (
                    :proposal_id, :profile, :cwd, :agent, :task_hash, :diff_sha256,
                    :diff_text, :base_files_json, :git_head, :created_at, :expires_at
                )
                """,
                record,
            )
        return self.get(record["proposal_id"], profile=profile)

    def get(self, proposal_id: str, *, profile: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ? AND profile = ?",
                (proposal_id, profile),
            ).fetchone()
        if row is None:
            raise ArtifactError("proposal not found")
        return self._deserialize(row)

    def list(self, *, profile: str, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM proposals
                WHERE profile = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (profile, safe_limit),
            ).fetchall()
        records = [self._deserialize(row) for row in rows]
        for record in records:
            record.pop("diff_text", None)
        return records

    @staticmethod
    def _deserialize(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["base_files"] = json.loads(record.pop("base_files_json"))
        return record
