from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from host_coding_agent.config import ConfigError
from host_coding_agent.models import ProfileConfig
from host_coding_agent.runtime import RuntimeRegistry


def _registry(config, tmp_path: Path, monkeypatch):
    mount_source = tmp_path / ".hermes-invest"
    workspace = mount_source / "profiles" / "invest-bot" / "workspace"
    workspace.mkdir(parents=True)
    config.auth.enabled = True
    config.profiles["invest-bot"] = ProfileConfig(
        token_env="TEST_TOKEN",
        allowed_container_roots=[
            Path("/opt/data/profiles/invest-bot/workspace")
        ],
        runtime_labels={"com.docker.compose.service": "hermes-invest"},
        default_cwd=Path("/opt/data/profiles/invest-bot/workspace"),
    )
    container_id = "a" * 64
    inspect_data = [
        {
            "Id": container_id,
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "hermes-invest",
                }
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(mount_source),
                    "Destination": "/opt/data",
                    "RW": True,
                }
            ],
        }
    ]
    monkeypatch.setattr("host_coding_agent.runtime.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        "host_coding_agent.runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps(inspect_data),
            stderr="",
        ),
    )
    return RuntimeRegistry(config), container_id, workspace


def test_registers_container_and_derives_host_workspace(
    config,
    tmp_path,
    monkeypatch,
):
    registry, container_id, workspace = _registry(config, tmp_path, monkeypatch)
    child = workspace / "repo"
    child.mkdir()

    registration = registry.register_docker(
        profile_name="invest-bot",
        container_id=container_id,
    )
    resolved = registry.resolve(
        profile_name="invest-bot",
        container_path="/opt/data/profiles/invest-bot/workspace/repo",
    )

    assert registration["workspace_roots"] == [str(workspace)]
    assert resolved == child
    assert workspace in config.profiles["invest-bot"].allowed_roots
    assert workspace in config.security.allowed_roots


def test_rejects_container_path_outside_profile_workspace(
    config,
    tmp_path,
    monkeypatch,
):
    registry, container_id, _ = _registry(config, tmp_path, monkeypatch)
    registry.register_docker(
        profile_name="invest-bot",
        container_id=container_id,
    )

    with pytest.raises(ConfigError, match="not allowed"):
        registry.resolve(
            profile_name="invest-bot",
            container_path="/opt/data/config.yaml",
        )


def test_rejects_unregistered_runtime(config, tmp_path, monkeypatch):
    registry, _, _ = _registry(config, tmp_path, monkeypatch)

    with pytest.raises(ConfigError, match="not registered"):
        registry.resolve(
            profile_name="invest-bot",
            container_path="/opt/data/profiles/invest-bot/workspace",
        )


def test_rejects_container_with_wrong_profile_label(
    config,
    tmp_path,
    monkeypatch,
):
    registry, container_id, _ = _registry(config, tmp_path, monkeypatch)
    config.profiles["invest-bot"].runtime_labels = {
        "com.docker.compose.service": "hermes-other"
    }

    with pytest.raises(ConfigError, match="labels"):
        registry.register_docker(
            profile_name="invest-bot",
            container_id=container_id,
        )


def test_persists_and_restores_registered_container(
    config,
    tmp_path,
    monkeypatch,
):
    registry, container_id, workspace = _registry(config, tmp_path, monkeypatch)
    state_path = tmp_path / "runtimes.json"
    registry.state_path = state_path
    registry.register_docker(
        profile_name="invest-bot",
        container_id=container_id,
    )

    restored = RuntimeRegistry(config, state_path=state_path)

    assert restored.resolve(
        profile_name="invest-bot",
        container_path="/opt/data/profiles/invest-bot/workspace",
    ) == workspace
    assert oct(state_path.stat().st_mode & 0o777) == "0o600"
