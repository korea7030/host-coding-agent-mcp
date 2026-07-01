from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .approvals import ApprovalError, ApprovalStore
from .artifacts import (
    ArtifactError,
    ProposalStore,
    _git_head,
    _reject_symlink_components,
    _sha256_bytes,
    extract_diff_paths,
)
from .config import validate_profile_cwd
from .models import AppConfig


class PatchApplyError(ValueError):
    pass


def _verify_base_files(proposal: dict[str, Any]) -> list[str]:
    cwd = Path(proposal["cwd"])
    paths = extract_diff_paths(proposal["diff_text"], cwd)
    if set(paths) != set(proposal["base_files"]):
        raise PatchApplyError("proposal file list does not match immutable base snapshot")
    for relative_path in paths:
        _reject_symlink_components(cwd, relative_path)
        target = Path(os.path.realpath(cwd / relative_path))
        if target != cwd and not target.is_relative_to(cwd):
            raise PatchApplyError("proposal path escapes workspace")
        expected = proposal["base_files"][relative_path]
        if expected is None:
            if target.exists():
                raise PatchApplyError(f"new-file path is no longer absent: {relative_path}")
            continue
        if not target.is_file():
            raise PatchApplyError(f"base file is missing or not regular: {relative_path}")
        actual = _sha256_bytes(target.read_bytes())
        if actual != expected:
            raise PatchApplyError(f"base file changed after proposal: {relative_path}")
    return paths


def _run_git_apply(
    cwd: Path,
    diff_text: str,
    *,
    check: bool,
    reverse: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(cwd), "apply"]
    if check:
        command.append("--check")
    if reverse:
        command.append("--reverse")
    command.extend(["--whitespace=error-all", "-"])
    try:
        return subprocess.run(
            command,
            input=diff_text,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PatchApplyError(f"git apply could not run: {exc}") from exc


class PatchApplier:
    def __init__(
        self,
        *,
        config: AppConfig,
        proposals: ProposalStore,
        approvals: ApprovalStore,
    ):
        self.config = config
        self.proposals = proposals
        self.approvals = approvals

    def apply(
        self,
        *,
        proposal_id: str,
        profile: str,
        proposal_sha256: str,
    ) -> dict[str, Any]:
        proposal = self.proposals.get(proposal_id, profile=profile)
        approval = self.approvals.claim_for_apply(
            proposal_id=proposal_id,
            profile=profile,
            proposal_sha256=proposal_sha256,
        )
        try:
            result = self._apply_claimed(
                proposal=proposal,
                profile=profile,
                proposal_sha256=proposal_sha256,
            )
        except (ArtifactError, PatchApplyError, ValueError) as exc:
            self.approvals.complete_apply(
                approval_id=approval["approval_id"],
                profile=profile,
                success=False,
                failure_reason=str(exc),
            )
            raise PatchApplyError(str(exc)) from exc
        try:
            completed = self.approvals.complete_apply(
                approval_id=approval["approval_id"],
                profile=profile,
                success=True,
            )
        except (ApprovalError, ValueError) as exc:
            reversed_patch = _run_git_apply(
                Path(proposal["cwd"]),
                proposal["diff_text"],
                check=False,
                reverse=True,
            )
            if reversed_patch.returncode != 0:
                raise PatchApplyError(
                    "patch applied but audit completion and rollback both failed"
                ) from exc
            try:
                self.approvals.complete_apply(
                    approval_id=approval["approval_id"],
                    profile=profile,
                    success=False,
                    failure_reason="audit completion failed; patch rolled back",
                )
            except ApprovalError:
                pass
            raise PatchApplyError("audit completion failed; patch rolled back") from exc
        return {"proposal": proposal_id, "approval": completed, **result}

    def _apply_claimed(
        self,
        *,
        proposal: dict[str, Any],
        profile: str,
        proposal_sha256: str,
    ) -> dict[str, Any]:
        if proposal["diff_sha256"] != proposal_sha256:
            raise PatchApplyError("proposal SHA-256 does not match")
        actual_diff_hash = _sha256_bytes(proposal["diff_text"].encode())
        if actual_diff_hash != proposal_sha256:
            raise PatchApplyError("immutable proposal diff hash is invalid")
        if datetime.fromisoformat(proposal["expires_at"]) <= datetime.now(timezone.utc):
            raise PatchApplyError("proposal has expired")
        cwd = validate_profile_cwd(proposal["cwd"], profile, self.config)
        if "GIT binary patch" in proposal["diff_text"] or "\nBinary files " in proposal["diff_text"]:
            raise PatchApplyError("binary patches are not allowed")
        paths = _verify_base_files(proposal)
        current_head = _git_head(cwd)
        if proposal["git_head"] is not None and current_head != proposal["git_head"]:
            raise PatchApplyError("Git HEAD changed after proposal")
        checked = _run_git_apply(cwd, proposal["diff_text"], check=True)
        if checked.returncode != 0:
            detail = checked.stderr.strip() or checked.stdout.strip()
            raise PatchApplyError(f"git apply --check failed: {detail[:1000]}")
        applied = _run_git_apply(cwd, proposal["diff_text"], check=False)
        if applied.returncode != 0:
            detail = applied.stderr.strip() or applied.stdout.strip()
            raise PatchApplyError(f"git apply failed: {detail[:1000]}")
        try:
            result_hashes = {}
            for relative_path in paths:
                target = cwd / relative_path
                result_hashes[relative_path] = (
                    _sha256_bytes(target.read_bytes()) if target.is_file() else None
                )
        except OSError as exc:
            reversed_patch = _run_git_apply(
                cwd,
                proposal["diff_text"],
                check=False,
                reverse=True,
            )
            if reversed_patch.returncode != 0:
                raise PatchApplyError(
                    "post-apply verification and rollback both failed"
                ) from exc
            raise PatchApplyError(
                "post-apply verification failed; patch rolled back"
            ) from exc
        return {
            "ok": True,
            "cwd": str(cwd),
            "changed_files": paths,
            "result_hashes": result_hashes,
        }
