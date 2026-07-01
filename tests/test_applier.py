from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from host_coding_agent.applier import PatchApplier, PatchApplyError
from host_coding_agent.approvals import ApprovalError, ApprovalStore
from host_coding_agent.artifacts import ProposalStore
from host_coding_agent.models import AgentName, ProfileConfig


def _setup(config, tmp_path: Path):
    workspace = config.security.allowed_roots[0]
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Test"],
        check=True,
    )
    target = workspace / "app.py"
    target.write_text("old\n")
    subprocess.run(["git", "-C", str(workspace), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "base"], check=True)
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_TOKEN",
        allowed_roots=[workspace],
        default_cwd=workspace,
    )
    path = tmp_path / "artifacts" / "proposals.db"
    proposals = ProposalStore(path, ttl_sec=3600, max_diff_chars=100_000)
    approvals = ApprovalStore(path)
    proposal = proposals.create(
        profile="dev-bot",
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
    approvals.create_pending(proposal)
    return workspace, proposal, approvals, PatchApplier(
        config=config,
        proposals=proposals,
        approvals=approvals,
    )


def _approve(approvals: ApprovalStore, proposal: dict):
    return approvals.decide(
        proposal_id=proposal["proposal_id"],
        profile="dev-bot",
        proposal_sha256=proposal["diff_sha256"],
        approved=True,
        decided_by="telegram:123",
        decision_channel="telegram",
    )


def test_applies_only_approved_immutable_proposal(config, tmp_path):
    workspace, proposal, approvals, applier = _setup(config, tmp_path)
    _approve(approvals, proposal)

    result = applier.apply(
        proposal_id=proposal["proposal_id"],
        profile="dev-bot",
        proposal_sha256=proposal["diff_sha256"],
    )

    assert result["ok"]
    assert result["changed_files"] == ["app.py"]
    assert (workspace / "app.py").read_text() == "new\n"
    assert result["approval"]["status"] == "applied"


def test_rejects_unapproved_and_replayed_apply(config, tmp_path):
    workspace, proposal, approvals, applier = _setup(config, tmp_path)

    with pytest.raises(ApprovalError, match="approved"):
        applier.apply(
            proposal_id=proposal["proposal_id"],
            profile="dev-bot",
            proposal_sha256=proposal["diff_sha256"],
        )
    _approve(approvals, proposal)
    applier.apply(
        proposal_id=proposal["proposal_id"],
        profile="dev-bot",
        proposal_sha256=proposal["diff_sha256"],
    )
    with pytest.raises(ApprovalError, match="approved"):
        applier.apply(
            proposal_id=proposal["proposal_id"],
            profile="dev-bot",
            proposal_sha256=proposal["diff_sha256"],
        )
    assert (workspace / "app.py").read_text() == "new\n"


def test_stale_base_file_fails_without_applying(config, tmp_path):
    workspace, proposal, approvals, applier = _setup(config, tmp_path)
    _approve(approvals, proposal)
    (workspace / "app.py").write_text("changed elsewhere\n")

    with pytest.raises(PatchApplyError, match="base file changed"):
        applier.apply(
            proposal_id=proposal["proposal_id"],
            profile="dev-bot",
            proposal_sha256=proposal["diff_sha256"],
        )

    assert (workspace / "app.py").read_text() == "changed elsewhere\n"
    approval = approvals.get_for_proposal(
        proposal["proposal_id"], profile="dev-bot"
    )
    assert approval["status"] == "failed"


def test_rolls_back_when_audit_completion_fails(config, tmp_path, monkeypatch):
    workspace, proposal, approvals, applier = _setup(config, tmp_path)
    _approve(approvals, proposal)
    original_complete = approvals.complete_apply
    calls = 0

    def fail_first_completion(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 and kwargs["success"]:
            raise ApprovalError("simulated audit failure")
        return original_complete(**kwargs)

    monkeypatch.setattr(approvals, "complete_apply", fail_first_completion)

    with pytest.raises(PatchApplyError, match="rolled back"):
        applier.apply(
            proposal_id=proposal["proposal_id"],
            profile="dev-bot",
            proposal_sha256=proposal["diff_sha256"],
        )

    assert (workspace / "app.py").read_text() == "old\n"
    assert approvals.get_for_proposal(
        proposal["proposal_id"],
        profile="dev-bot",
    )["status"] == "failed"
