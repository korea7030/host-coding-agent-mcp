from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from host_coding_agent.artifacts import (
    ArtifactError,
    ProposalStore,
    extract_diff_paths,
    normalize_diff_text,
)
from host_coding_agent.models import AgentName


def _store(tmp_path: Path, *, max_diff_chars: int = 100_000) -> ProposalStore:
    return ProposalStore(
        tmp_path / "artifacts" / "proposals.db",
        ttl_sec=3600,
        max_diff_chars=max_diff_chars,
    )


def _diff(path: str = "app.py", old: str = "old", new: str = "new") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


def test_extracts_and_normalizes_unified_diff_paths():
    assert extract_diff_paths(_diff("src/app.py")) == ["src/app.py"]


def test_normalizes_transport_newline_and_whitespace_only_added_lines():
    raw = (
        "--- /dev/null\n"
        "+++ blank.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+first\n"
        "+    \n"
        "+last"
    )

    assert normalize_diff_text(raw) == (
        "--- /dev/null\n"
        "+++ b/blank.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+first\n"
        "+\n"
        "+last\n"
    )


def test_recounts_incorrect_hunk_line_count_before_storing(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = _store(tmp_path)
    diff = (
        "--- /dev/null\n"
        "+++ count.py\n"
        "@@ -0,0 +1,99 @@\n"
        "+print('ok')"
    )

    proposal = store.create(
        profile="dev-bot",
        cwd=workspace,
        agent=AgentName.codex,
        task="add script",
        diff_text=diff,
    )

    assert proposal["diff_text"].endswith("\n")


def test_stores_immutable_proposal_with_base_hash(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("old\n")
    store = _store(tmp_path)

    proposal = store.create(
        profile="dev-bot",
        cwd=workspace,
        agent=AgentName.codex,
        task="change app",
        diff_text=_diff(),
    )

    expected_base = "sha256:" + hashlib.sha256(b"old\n").hexdigest()
    assert proposal["profile"] == "dev-bot"
    assert proposal["diff_sha256"].startswith("sha256:")
    assert proposal["base_files"] == {"app.py": expected_base}
    assert proposal["task_hash"].startswith("sha256:")
    assert proposal["diff_text"] == _diff()
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"
    assert oct(store.path.parent.stat().st_mode & 0o777) == "0o700"

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE proposals SET diff_text = 'tampered' WHERE proposal_id = ?",
                (proposal["proposal_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM proposals WHERE proposal_id = ?",
                (proposal["proposal_id"],),
            )


def test_stores_proposal_with_existing_task_hash(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("old\n")
    store = _store(tmp_path)
    task_hash = "sha256:" + "a" * 64

    proposal = store.create_with_task_hash(
        profile="dev-bot",
        cwd=workspace,
        agent=AgentName.codex,
        task_hash=task_hash,
        diff_text=_diff(),
    )

    assert proposal["task_hash"] == task_hash

    with pytest.raises(ArtifactError, match="invalid task hash"):
        store.create_with_task_hash(
            profile="dev-bot",
            cwd=workspace,
            agent=AgentName.codex,
            task_hash="invalid",
            diff_text=_diff(),
        )


def test_new_file_snapshot_is_none(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = _store(tmp_path)

    diff = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+print('new')\n"
    )
    proposal = store.create(
        profile="dev-bot",
        cwd=workspace,
        agent=AgentName.opencode,
        task="add file",
        diff_text=diff,
    )

    assert proposal["base_files"] == {"new.py": None}


def test_profile_isolation_and_list_omits_diff_text(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("old\n")
    store = _store(tmp_path)
    proposal = store.create(
        profile="dev-bot",
        cwd=workspace,
        agent=AgentName.codex,
        task="change app",
        diff_text=_diff(),
    )

    with pytest.raises(ArtifactError, match="not found"):
        store.get(proposal["proposal_id"], profile="research-bot")

    records = store.list(profile="dev-bot")
    assert len(records) == 1
    assert "diff_text" not in records[0]
    assert store.list(profile="research-bot") == []


def test_rejects_path_traversal_and_absolute_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = _store(tmp_path)

    for unsafe in ("../escape.py", "/tmp/escape.py"):
        with pytest.raises(ArtifactError, match="unsafe diff path"):
            store.create(
                profile="dev-bot",
                cwd=workspace,
                agent=AgentName.codex,
                task="escape",
                diff_text=_diff(unsafe),
            )


def test_accepts_absolute_diff_path_inside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("old\n")
    store = _store(tmp_path)

    proposal = store.create(
        profile="dev-bot",
        cwd=workspace,
        agent=AgentName.codex,
        task="change app",
        diff_text=_diff(str(target)),
    )

    assert proposal["base_files"] == {
        "app.py": "sha256:" + hashlib.sha256(b"old\n").hexdigest()
    }


def test_rejects_absolute_diff_path_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    store = _store(tmp_path)

    with pytest.raises(ArtifactError, match="unsafe diff path"):
        store.create(
            profile="dev-bot",
            cwd=workspace,
            agent=AgentName.codex,
            task="escape",
            diff_text=_diff(str(outside)),
        )


def test_rejects_symlink_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.py").write_text("old\n")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    store = _store(tmp_path)

    with pytest.raises(ArtifactError, match="symlink"):
        store.create(
            profile="dev-bot",
            cwd=workspace,
            agent=AgentName.codex,
            task="change linked file",
            diff_text=_diff("linked/app.py"),
        )


def test_rejects_empty_and_oversized_diffs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = _store(tmp_path, max_diff_chars=1_000)

    with pytest.raises(ArtifactError, match="empty"):
        store.create(
            profile="dev-bot",
            cwd=workspace,
            agent=AgentName.codex,
            task="empty",
            diff_text="",
        )
    with pytest.raises(ArtifactError, match="size limit"):
        store.create(
            profile="dev-bot",
            cwd=workspace,
            agent=AgentName.codex,
            task="large",
            diff_text=_diff() + ("x" * 1_000),
        )
