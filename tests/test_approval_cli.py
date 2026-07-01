from __future__ import annotations

from pathlib import Path

import yaml

from host_coding_agent.approval_cli import run
from host_coding_agent.approvals import ApprovalStore
from host_coding_agent.artifacts import ProposalStore
from host_coding_agent.models import AgentName


def _setup(config, tmp_path: Path) -> tuple[Path, dict]:
    workspace = config.security.allowed_roots[0]
    (workspace / "app.py").write_text("old\n")
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    proposals = ProposalStore(
        config.artifacts.path,
        ttl_sec=3600,
        max_diff_chars=100_000,
    )
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
    ApprovalStore(config.artifacts.path).create_pending(proposal)
    return config_path, proposal


def test_cli_reviews_and_approves_proposal(config, tmp_path):
    config_path, proposal = _setup(config, tmp_path)

    shown = run(
        [
            "--config",
            str(config_path),
            "show",
            proposal["proposal_id"],
            "--profile",
            "dev-bot",
        ]
    )
    assert shown["proposal"]["diff_text"] == proposal["diff_text"]
    assert shown["approval"]["status"] == "pending"

    result = run(
        [
            "--config",
            str(config_path),
            "approve",
            proposal["proposal_id"],
            proposal["diff_sha256"],
            "--profile",
            "dev-bot",
            "--actor",
            "telegram:123",
            "--channel",
            "telegram",
        ]
    )
    assert result["approval"]["status"] == "approved"


def test_cli_rejects_proposal(config, tmp_path):
    config_path, proposal = _setup(config, tmp_path)

    result = run(
        [
            "--config",
            str(config_path),
            "reject",
            proposal["proposal_id"],
            proposal["diff_sha256"],
            "--profile",
            "dev-bot",
            "--actor",
            "telegram:123",
            "--channel",
            "telegram",
        ]
    )

    assert result["approval"]["status"] == "rejected"
