from __future__ import annotations

from typing import Any

from .applier import PatchApplier, PatchApplyError
from .approvals import ApprovalError
from .models import DeliveryMode, WorktreeStatus
from .worktrees import WorktreeError, WorktreeManager


class ManualDeliveryError(ValueError):
    pass


class ManualDelivery:
    def __init__(
        self,
        *,
        manager: WorktreeManager,
        applier: PatchApplier,
    ):
        self.manager = manager
        self.applier = applier

    def applies_to(self, *, proposal_id: str, profile: str) -> bool:
        return self.manager.find_by_proposal(
            proposal_id,
            profile=profile,
        ) is not None

    def deliver(
        self,
        *,
        proposal_id: str,
        profile: str,
        proposal_sha256: str,
    ) -> dict[str, Any]:
        found = self.manager.find_by_proposal(proposal_id, profile=profile)
        if found is None:
            raise ManualDeliveryError("worktree proposal not found")
        job, link = found
        if job.delivery_mode != DeliveryMode.manual:
            raise ManualDeliveryError("worktree job is not in manual delivery mode")
        if link["proposal_sha256"] != proposal_sha256:
            raise ManualDeliveryError("worktree proposal SHA-256 does not match")
        if job.status == WorktreeStatus.delivered:
            apply_result: dict[str, Any] = {
                "ok": True,
                "proposal": proposal_id,
                "already_delivered": True,
            }
        elif job.status == WorktreeStatus.proposed:
            approval = self.applier.approvals.get_for_proposal(
                proposal_id,
                profile=profile,
            )
            if approval["status"] == "applied":
                apply_result = {
                    "ok": True,
                    "proposal": proposal_id,
                    "approval": approval,
                    "recovered_delivery": True,
                }
            else:
                try:
                    apply_result = self.applier.apply(
                        proposal_id=proposal_id,
                        profile=profile,
                        proposal_sha256=proposal_sha256,
                    )
                except (ApprovalError, PatchApplyError):
                    self.manager.transition(
                        job.job_id,
                        profile=profile,
                        status=WorktreeStatus.failed,
                    )
                    raise
            self.manager.transition(
                job.job_id,
                profile=profile,
                status=WorktreeStatus.delivered,
            )
        else:
            raise ManualDeliveryError(
                f"worktree job cannot be delivered from status {job.status.value}"
            )
        cleanup = self.manager.cleanup(job.job_id, profile=profile)
        return {
            **apply_result,
            "job_id": job.job_id,
            "delivery_status": WorktreeStatus.delivered.value,
            "proposal_status": "applied",
            "requires_approval": False,
            "applied": True,
            "message": "Proposal applied to the original workspace.",
            "cleanup": cleanup.model_dump(mode="json"),
        }
