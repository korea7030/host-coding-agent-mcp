from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .approvals import ApprovalError, ApprovalStore
from .artifacts import ArtifactError, ProposalStore, extract_diff_paths
from .models import AgentName, DeliveryMode, WorktreeProposalResult, WorktreeStatus
from .worktrees import WorktreeError, WorktreeManager


class WorktreeProposalError(ValueError):
    pass


def _run_git(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeProposalError(f"git diff generation could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise WorktreeProposalError(f"git diff generation failed: {detail[:1000]}")
    return completed.stdout


def _validate_delivery_target(repository: Path, base_commit: str) -> None:
    head = _run_git(["rev-parse", "HEAD"], cwd=repository).strip()
    if head != base_commit:
        raise WorktreeProposalError("repository HEAD changed since worktree creation")
    status = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository,
    )
    if status:
        raise WorktreeProposalError("repository has uncommitted or untracked changes")


def build_worktree_diff(worktree: Path, base_commit: str) -> str:
    index_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="hca-index-", delete=False) as handle:
            index_path = handle.name
        os.unlink(index_path)
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = index_path
        _run_git(["read-tree", base_commit], cwd=worktree, env=env)
        _run_git(["add", "-A", "--", "."], cwd=worktree, env=env)
        return _run_git(
            [
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                base_commit,
                "--",
                ".",
            ],
            cwd=worktree,
            env=env,
        )
    finally:
        if index_path:
            try:
                os.unlink(index_path)
            except FileNotFoundError:
                pass


def create_managed_worktree_proposal(
    *,
    manager: WorktreeManager,
    proposals: ProposalStore,
    approvals: ApprovalStore,
    job_id: str,
    profile: str,
    agent: AgentName,
) -> WorktreeProposalResult:
    job = manager.get(job_id, profile=profile)
    if job.status != WorktreeStatus.tested:
        raise WorktreeProposalError("worktree job is not ready for proposal creation")
    try:
        worktree = manager.validate_checkout(job_id, profile=profile)
        _validate_delivery_target(job.repository, job.base_commit)
        diff_text = build_worktree_diff(worktree, job.base_commit)
        changed_files = extract_diff_paths(diff_text, job.repository)
        proposal = proposals.create_with_task_hash(
            profile=profile,
            cwd=job.repository,
            agent=agent,
            task_hash=job.task_hash,
            diff_text=diff_text,
        )
        if job.delivery_mode == DeliveryMode.manual:
            approvals.create_pending(proposal)
        manager.mark_proposed(
            job_id,
            profile=profile,
            proposal_id=proposal["proposal_id"],
            proposal_sha256=proposal["diff_sha256"],
        )
    except (
        ApprovalError,
        ArtifactError,
        WorktreeError,
        WorktreeProposalError,
    ) as exc:
        current = manager.get(job_id, profile=profile)
        if current.status == WorktreeStatus.tested:
            manager.transition(
                job_id,
                profile=profile,
                status=WorktreeStatus.failed,
            )
        return WorktreeProposalResult(job_id=job_id, ok=False, error=str(exc))
    return WorktreeProposalResult(
        job_id=job_id,
        ok=True,
        proposal_id=proposal["proposal_id"],
        proposal_sha256=proposal["diff_sha256"],
        changed_files=changed_files,
    )
