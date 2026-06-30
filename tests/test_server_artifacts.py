from __future__ import annotations

import yaml
import pytest

import server
from host_coding_agent.models import AgentName, RunMode, RunResult


@pytest.mark.asyncio
async def test_propose_patch_result_is_stored_and_exposed_by_mcp(
    config, monkeypatch, tmp_path
):
    workspace = config.security.allowed_roots[0]
    (workspace / "app.py").write_text("old\n")
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    def fake_execute_agent(**kwargs):
        return RunResult(
            ok=True,
            selected_agent=AgentName.codex,
            cwd=workspace,
            mode=RunMode.propose_patch,
            stdout="proposal",
            proposed_diff=diff,
        )

    monkeypatch.setattr(server, "execute_agent", fake_execute_agent)
    mcp, _ = server.create_server(config_path)

    run_result = await mcp.call_tool(
        "run_coding_agent",
        {
            "task": "change app",
            "cwd": str(workspace),
            "agent": "codex",
            "mode": "propose_patch",
        },
    )
    run_data = run_result.structured_content
    assert run_data is not None
    assert run_data["proposal_id"]
    assert run_data["proposal_sha256"].startswith("sha256:")
    assert run_data["artifact_error"] is None

    get_result = await mcp.call_tool(
        "get_patch_proposal",
        {"proposal_id": run_data["proposal_id"]},
    )
    get_data = get_result.structured_content
    assert get_data is not None and get_data["ok"]
    assert get_data["proposal"]["diff_text"] == diff

    list_result = await mcp.call_tool("list_patch_proposals", {"limit": 10})
    list_data = list_result.structured_content
    assert list_data is not None and list_data["ok"]
    assert len(list_data["proposals"]) == 1
    assert "diff_text" not in list_data["proposals"][0]
