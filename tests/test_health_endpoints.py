from __future__ import annotations

from pathlib import Path

import yaml
from starlette.testclient import TestClient

import server
from host_coding_agent.models import ProfileConfig


def test_healthz_and_readyz_expose_server_state_without_mcp_stream(
    config,
    monkeypatch,
    tmp_path: Path,
):
    workspace = config.security.allowed_roots[0]
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_TOKEN",
        allowed_roots=[workspace],
        default_cwd=workspace,
    )
    monkeypatch.setenv("TEST_DEV_TOKEN", "t" * 32)
    config.artifacts.path = tmp_path / "artifacts" / "proposals.db"
    config.worktrees.state_path = tmp_path / "artifacts" / "worktrees.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    mcp, _ = server.create_server(config_path)

    with TestClient(mcp.http_app(path="/mcp")) as client:
        health = client.get("/healthz")
        ready = client.get("/readyz")

    assert health.status_code == 200
    health_payload = health.json()
    assert health_payload["ok"] is True
    assert health_payload["server"] == "host-coding-agent"
    assert health_payload["status"] == "alive"
    assert health_payload["tools"] > 0
    assert health_payload["configured_profiles"] == ["dev-bot"]
    assert health_payload["runtime_profiles"] == []
    assert "ClosedResourceError" in health_payload["stream_note"]

    assert ready.status_code == 200
    ready_payload = ready.json()
    assert ready_payload["ok"] is True
    assert ready_payload["status"] == "ready"
    assert ready_payload["auth_enabled"] is True
    assert ready_payload["artifacts_path"].endswith("proposals.db")
    assert ready_payload["worktree_state_path"].endswith("worktrees.db")
