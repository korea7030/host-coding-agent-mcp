from __future__ import annotations

from pathlib import Path

import yaml
from starlette.testclient import TestClient

import server
from host_coding_agent.approvals import ApprovalStore
from host_coding_agent.artifacts import ProposalStore
from host_coding_agent.models import AgentName, ProfileConfig


def test_telegram_endpoint_requires_profile_token_and_allowed_identity(
    config,
    monkeypatch,
    tmp_path: Path,
):
    workspace = config.security.allowed_roots[0]
    (workspace / "app.py").write_text("old\n")
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_TOKEN",
        allowed_roots=[workspace],
        approval_identities=["telegram:123"],
        default_cwd=workspace,
    )
    token = "t" * 32
    monkeypatch.setenv("TEST_DEV_TOKEN", token)
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
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
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    mcp, _ = server.create_server(config_path)

    with TestClient(mcp.http_app(path="/mcp")) as client:
        unauthorized = client.post(
            "/approval/telegram",
            json={
                "action": "show",
                "proposal_id": proposal["proposal_id"],
                "telegram_user_id": "999",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        accepted = client.post(
            "/approval/telegram",
            json={
                "action": "show",
                "proposal_id": proposal["proposal_id"],
                "telegram_user_id": "123",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert unauthorized.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["proposal"]["proposal_id"] == proposal["proposal_id"]
