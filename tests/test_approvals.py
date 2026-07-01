from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from host_coding_agent.approvals import ApprovalError, ApprovalStore
from host_coding_agent.artifacts import ProposalStore
from host_coding_agent.models import AgentName


def _stores(tmp_path: Path) -> tuple[ProposalStore, ApprovalStore]:
    path = tmp_path / "artifacts" / "proposals.db"
    proposals = ProposalStore(path, ttl_sec=3600, max_diff_chars=100_000)
    return proposals, ApprovalStore(path)


def _proposal(
    proposals: ProposalStore,
    tmp_path: Path,
    *,
    profile: str = "dev-bot",
) -> dict:
    workspace = tmp_path / f"workspace-{profile}"
    workspace.mkdir(exist_ok=True)
    (workspace / "app.py").write_text("old\n")
    return proposals.create(
        profile=profile,
        cwd=workspace,
        agent=AgentName.codex,
        task="change app",
        diff_text=(
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
    )


def test_creates_profile_scoped_pending_approval(tmp_path):
    proposals, approvals = _stores(tmp_path)
    proposal = _proposal(proposals, tmp_path)

    approval = approvals.create_pending(proposal)

    assert approval["status"] == "pending"
    assert approval["proposal_id"] == proposal["proposal_id"]
    assert approval["proposal_sha256"] == proposal["diff_sha256"]
    assert approvals.get_for_proposal(
        proposal["proposal_id"], profile="dev-bot"
    ) == approval
    with pytest.raises(ApprovalError, match="not found"):
        approvals.get(approval["approval_id"], profile="research-bot")


def test_approval_requires_matching_hash_and_is_one_time(tmp_path):
    proposals, approvals = _stores(tmp_path)
    proposal = _proposal(proposals, tmp_path)
    approvals.create_pending(proposal)

    with pytest.raises(ApprovalError, match="not found"):
        approvals.decide(
            proposal_id=proposal["proposal_id"],
            profile="dev-bot",
            proposal_sha256="sha256:wrong",
            approved=True,
            decided_by="telegram:123",
            decision_channel="telegram",
        )

    approved = approvals.decide(
        proposal_id=proposal["proposal_id"],
        profile="dev-bot",
        proposal_sha256=proposal["diff_sha256"],
        approved=True,
        decided_by="telegram:123",
        decision_channel="telegram",
    )
    assert approved["status"] == "approved"
    assert approved["decided_by"] == "telegram:123"
    assert [event["status"] for event in approvals.events(
        approval_id=approved["approval_id"],
        profile="dev-bot",
    )] == ["pending", "approved"]

    with pytest.raises(ApprovalError, match="not found"):
        approvals.decide(
            proposal_id=proposal["proposal_id"],
            profile="dev-bot",
            proposal_sha256=proposal["diff_sha256"],
            approved=True,
            decided_by="telegram:123",
            decision_channel="telegram",
        )


def test_expired_approval_cannot_be_approved(tmp_path):
    proposals, approvals = _stores(tmp_path)
    proposal = _proposal(proposals, tmp_path)
    approval = approvals.create_pending(proposal)
    future = datetime.fromisoformat(approval["expires_at"]) + timedelta(seconds=1)

    with pytest.raises(ApprovalError, match="expired"):
        approvals.decide(
            proposal_id=proposal["proposal_id"],
            profile="dev-bot",
            proposal_sha256=proposal["diff_sha256"],
            approved=True,
            decided_by="telegram:123",
            decision_channel="telegram",
            now=future,
        )

    assert approvals.get(
        approval["approval_id"], profile="dev-bot"
    )["status"] == "expired"


def test_database_rejects_identity_mutation_invalid_transition_and_delete(tmp_path):
    proposals, approvals = _stores(tmp_path)
    proposal = _proposal(proposals, tmp_path)
    approval = approvals.create_pending(proposal)

    with sqlite3.connect(approvals.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="identity"):
            connection.execute(
                "UPDATE approvals SET profile = 'other' WHERE approval_id = ?",
                (approval["approval_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            connection.execute(
                "UPDATE approvals SET status = 'applied' WHERE approval_id = ?",
                (approval["approval_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM approvals WHERE approval_id = ?",
                (approval["approval_id"],),
            )
        event_id = connection.execute(
            "SELECT event_id FROM approval_events WHERE approval_id = ?",
            (approval["approval_id"],),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE approval_events SET status = 'tampered' WHERE event_id = ?",
                (event_id,),
            )
