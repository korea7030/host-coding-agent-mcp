from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


IGNORED_DIRECTORIES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
}
MAX_SNAPSHOT_FILES = 10_000
MAX_HASH_BYTES = 1_000_000


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int
    digest: str | None


DirectSnapshot = dict[str, FileFingerprint]


def snapshot_workspace(root: Path) -> DirectSnapshot:
    snapshot: DirectSnapshot = {}
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in IGNORED_DIRECTORIES
        ]
        for filename in filenames:
            path = Path(current) / filename
            try:
                stat = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            digest = _digest(path, stat.st_size) if stat.st_size <= MAX_HASH_BYTES else None
            snapshot[relative] = FileFingerprint(
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                digest=digest,
            )
            if len(snapshot) >= MAX_SNAPSHOT_FILES:
                return snapshot
    return snapshot


def changed_files(before: DirectSnapshot, after: DirectSnapshot) -> list[str]:
    paths = set(before) | set(after)
    return sorted(
        path
        for path in paths
        if before.get(path) != after.get(path)
    )


def _digest(path: Path, size: int) -> str:
    del size
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()
