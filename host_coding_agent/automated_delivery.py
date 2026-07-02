from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .artifacts import ProposalStore, _sha256_bytes, normalize_diff_text
from .models import AppConfig, DeliveryMode, WorktreeStatus
from .proposals import build_worktree_diff
from .worktrees import WorktreeError, WorktreeManager


class AutomatedDeliveryError(ValueError):
    pass


def _run(command: list[str], *, cwd: Path, timeout: int = 120) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AutomatedDeliveryError(f"delivery command could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AutomatedDeliveryError(f"delivery command failed: {detail[:1000]}")
    return completed.stdout.strip()


def _github_repository(remote_url: str, allowed_hosts: list[str]) -> tuple[str, str]:
    if re.match(r"^[^/@:]+@[^/:]+:.+$", remote_url):
        user_host, path = remote_url.split(":", 1)
        host = user_host.rsplit("@", 1)[1]
    else:
        parsed = urlparse(remote_url)
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
            raise AutomatedDeliveryError("PR remote must be a GitHub HTTPS or SSH URL")
        if parsed.username and parsed.scheme == "https":
            raise AutomatedDeliveryError("credential-bearing remote URLs are not allowed")
        host = parsed.hostname
        path = parsed.path.lstrip("/")
    if host.casefold() not in {value.casefold() for value in allowed_hosts}:
        raise AutomatedDeliveryError("remote host is not allowed for this profile")
    repository = path.removesuffix(".git").strip("/")
    if len(repository.split("/")) != 2:
        raise AutomatedDeliveryError("remote URL must identify one owner/repository")
    return host, repository


class AutomatedDelivery:
    def __init__(
        self,
        *,
        manager: WorktreeManager,
        proposals: ProposalStore,
        config: AppConfig,
    ):
        self.manager = manager
        self.proposals = proposals
        self.config = config

    def deliver(self, *, job_id: str, profile: str) -> dict[str, Any]:
        job = self.manager.get(job_id, profile=profile)
        if job.status != WorktreeStatus.proposed:
            raise AutomatedDeliveryError("worktree job is not ready for automated delivery")
        if job.delivery_mode not in {
            DeliveryMode.auto,
            DeliveryMode.commit,
            DeliveryMode.pr,
        }:
            raise AutomatedDeliveryError("worktree job is not an automated delivery mode")
        profile_config = self.config.profiles[profile]
        if job.delivery_mode not in profile_config.allowed_delivery_modes:
            raise AutomatedDeliveryError("delivery mode is not allowed for this profile")
        link = self.manager.get_proposal_link(job_id, profile=profile)
        proposal = self.proposals.get(link["proposal_id"], profile=profile)
        worktree = self.manager.validate_checkout(job_id, profile=profile)
        current_diff = normalize_diff_text(
            build_worktree_diff(worktree, job.base_commit),
            job.repository,
        )
        if _sha256_bytes(current_diff.encode()) != proposal["diff_sha256"]:
            raise AutomatedDeliveryError("worktree changed after immutable proposal creation")
        target = self.manager.get_delivery_target(job_id, profile=profile)
        resolved_mode = self._resolve_mode(job.delivery_mode, target, profile_config)
        try:
            _run(["git", "add", "-A", "--", "."], cwd=worktree)
            _run(
                [
                    "git",
                    "-c",
                    f"user.name={profile_config.git_author_name}",
                    "-c",
                    f"user.email={profile_config.git_author_email}",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    f"hca: deliver {job.job_id}",
                ],
                cwd=worktree,
            )
            commit_sha = _run(["git", "rev-parse", "HEAD"], cwd=worktree)
            pr_url = None
            if resolved_mode == DeliveryMode.pr:
                pr_url = self._push_and_create_pr(
                    job=job,
                    target=target,
                    profile_config=profile_config,
                    worktree=worktree,
                )
            self.manager.complete_automated_delivery(
                job_id,
                profile=profile,
                resolved_mode=resolved_mode,
                commit_sha=commit_sha,
                remote_name=(
                    target["remote_name"]
                    if resolved_mode == DeliveryMode.pr
                    else None
                ),
                remote_url=(
                    target["remote_url"]
                    if resolved_mode == DeliveryMode.pr
                    else None
                ),
                pr_url=pr_url,
            )
        except (AutomatedDeliveryError, WorktreeError):
            current = self.manager.get(job_id, profile=profile)
            if current.status == WorktreeStatus.proposed:
                self.manager.transition(
                    job_id,
                    profile=profile,
                    status=WorktreeStatus.failed,
                )
            raise
        cleanup = self.manager.cleanup(
            job_id,
            profile=profile,
            remove_branch=resolved_mode == DeliveryMode.pr,
        )
        return {
            "ok": True,
            "job_id": job_id,
            "requested_mode": job.delivery_mode.value,
            "resolved_mode": resolved_mode.value,
            "branch": job.branch,
            "commit_sha": commit_sha,
            "remote": (
                target["remote_name"]
                if resolved_mode == DeliveryMode.pr
                else None
            ),
            "pr_url": pr_url,
            "cleanup": cleanup.model_dump(mode="json"),
        }

    @staticmethod
    def _resolve_mode(requested, target, profile_config) -> DeliveryMode:
        if requested == DeliveryMode.commit:
            return DeliveryMode.commit
        if requested == DeliveryMode.pr:
            if not profile_config.allow_git_push or not profile_config.allow_pull_requests:
                raise AutomatedDeliveryError(
                    "PR delivery is not enabled for this profile"
                )
            if (
                not target["remote_name"]
                or not target["remote_url"]
                or not target["remote_push_url"]
                or not target["base_branch"]
            ):
                raise AutomatedDeliveryError(
                    "PR delivery requires a remote and base branch"
                )
            if target["remote_name"] not in profile_config.allowed_remote_names:
                raise AutomatedDeliveryError(
                    "remote name is not allowed for this profile"
                )
            _github_repository(
                target["remote_url"],
                profile_config.allowed_remote_hosts,
            )
            _github_repository(
                target["remote_push_url"],
                profile_config.allowed_remote_hosts,
            )
            return DeliveryMode.pr
        can_pr = (
            profile_config.allow_git_push
            and profile_config.allow_pull_requests
            and target["remote_name"] is not None
            and target["remote_url"] is not None
            and target["remote_push_url"] is not None
            and target["base_branch"] is not None
        )
        if can_pr:
            if target["remote_name"] not in profile_config.allowed_remote_names:
                can_pr = False
            else:
                try:
                    _github_repository(
                        target["remote_url"],
                        profile_config.allowed_remote_hosts,
                    )
                    _github_repository(
                        target["remote_push_url"],
                        profile_config.allowed_remote_hosts,
                    )
                except AutomatedDeliveryError:
                    can_pr = False
        return DeliveryMode.pr if can_pr else DeliveryMode.commit

    @staticmethod
    def _push_and_create_pr(*, job, target, profile_config, worktree: Path) -> str:
        remote_name = target["remote_name"]
        remote_url = target["remote_url"]
        remote_push_url = target["remote_push_url"]
        base_branch = target["base_branch"]
        if (
            not remote_name
            or not remote_url
            or not remote_push_url
            or not base_branch
        ):
            raise AutomatedDeliveryError("PR delivery requires a remote and base branch")
        if remote_name not in profile_config.allowed_remote_names:
            raise AutomatedDeliveryError("remote name is not allowed for this profile")
        host, repository = _github_repository(
            remote_url,
            profile_config.allowed_remote_hosts,
        )
        push_host, push_repository = _github_repository(
            remote_push_url,
            profile_config.allowed_remote_hosts,
        )
        if (push_host.casefold(), push_repository) != (
            host.casefold(),
            repository,
        ):
            raise AutomatedDeliveryError(
                "remote fetch and push URLs identify different repositories"
            )
        actual_url = _run(
            ["git", "remote", "get-url", remote_name],
            cwd=worktree,
        )
        if actual_url != remote_url:
            raise AutomatedDeliveryError("remote URL changed after worktree creation")
        actual_push_url = _run(
            ["git", "remote", "get-url", "--push", remote_name],
            cwd=worktree,
        )
        if actual_push_url != remote_push_url:
            raise AutomatedDeliveryError(
                "remote push URL changed after worktree creation"
            )
        _run(
            [
                "git",
                "push",
                "--no-verify",
                "--set-upstream",
                remote_name,
                job.branch,
            ],
            cwd=worktree,
            timeout=300,
        )
        output = _run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repository,
                "--head",
                job.branch,
                "--base",
                base_branch,
                "--title",
                f"Host coding agent: {job.job_id}",
                "--body",
                f"Automated delivery for worktree job `{job.job_id}`.",
            ],
            cwd=worktree,
            timeout=120,
        )
        match = re.search(
            rf"https://{re.escape(host)}/[^\s]+/pull/\d+",
            output,
        )
        if not match:
            raise AutomatedDeliveryError("GitHub CLI did not return a PR URL")
        return match.group(0)
