import pytest
from fastmcp.server.auth import AccessToken

from host_coding_agent.auth import ProfileTokenVerifier, build_auth_provider
from host_coding_agent.models import (
    AgentName,
    ExecutionContext,
    PathMapping,
    ProfileConfig,
    RunMode,
)
from host_coding_agent.profiles import resolve_profile_request
from host_coding_agent.security import SecurityViolation


def _enable_profile(config):
    root = config.security.allowed_roots[0]
    config.auth.enabled = True
    config.profiles["dev-bot"] = ProfileConfig(
        token_env="TEST_DEV_BOT_TOKEN",
        allowed_roots=[root],
        allowed_agents=[AgentName.codex],
        allowed_modes=[RunMode.read_only, RunMode.propose_patch],
        default_cwd=root,
        default_agent=AgentName.codex,
        default_mode=RunMode.read_only,
        context=ExecutionContext(
            language="한국어",
            runtime="python",
            package_manager="uv",
        ),
    )
    return root


def _access_token(profile="dev-bot"):
    return AccessToken(
        token="x" * 32,
        client_id=profile,
        subject=profile,
        scopes=["host-coding-agent"],
        claims={"profile": profile},
    )


@pytest.mark.asyncio
async def test_profile_token_verifier_maps_token_to_profile():
    verifier = ProfileTokenVerifier({"a" * 32: "dev-bot"}, "host-coding-agent")

    accepted = await verifier.verify_token("a" * 32)
    rejected = await verifier.verify_token("b" * 32)

    assert accepted is not None
    assert accepted.claims["profile"] == "dev-bot"
    assert rejected is None


def test_auth_provider_requires_profile_token_environment(config, monkeypatch):
    _enable_profile(config)
    monkeypatch.delenv("TEST_DEV_BOT_TOKEN", raising=False)

    with pytest.raises(ValueError, match="missing bearer token"):
        build_auth_provider(config)


def test_profile_defaults_and_call_context_are_merged(config):
    root = _enable_profile(config)

    resolved = resolve_profile_request(
        access_token=_access_token(),
        config=config,
        assistant_id=None,
        cwd=None,
        agent=None,
        mode=None,
        context=ExecutionContext(framework="FastAPI", runtime_version="3.12"),
    )

    assert resolved.profile_name == "dev-bot"
    assert resolved.cwd == str(root)
    assert resolved.agent == AgentName.codex
    assert resolved.mode == RunMode.read_only
    assert resolved.context.language == "한국어"
    assert resolved.context.runtime == "python"
    assert resolved.context.framework == "FastAPI"
    assert resolved.context.runtime_version == "3.12"


def test_profile_rejects_spoofed_assistant_id(config):
    _enable_profile(config)

    with pytest.raises(SecurityViolation, match="does not match"):
        resolve_profile_request(
            access_token=_access_token(),
            config=config,
            assistant_id="admin",
            cwd=None,
            agent=None,
            mode=None,
            context=None,
        )


def test_profile_rejects_disallowed_agent_and_mode(config):
    _enable_profile(config)

    with pytest.raises(SecurityViolation, match="agent is not allowed"):
        resolve_profile_request(
            access_token=_access_token(),
            config=config,
            assistant_id=None,
            cwd=None,
            agent=AgentName.opencode,
            mode=None,
            context=None,
        )

    with pytest.raises(SecurityViolation, match="mode is not allowed"):
        resolve_profile_request(
            access_token=_access_token(),
            config=config,
            assistant_id=None,
            cwd=None,
            agent=None,
            mode=RunMode.apply_patch,
            context=None,
        )


def test_profile_rejects_cwd_outside_profile_root(config, tmp_path):
    _enable_profile(config)
    other = tmp_path / "other"
    other.mkdir()
    config.security.allowed_roots.append(other)

    with pytest.raises(ValueError, match="not allowed for this profile"):
        resolve_profile_request(
            access_token=_access_token(),
            config=config,
            assistant_id=None,
            cwd=str(other),
            agent=None,
            mode=None,
            context=None,
        )


def test_profile_maps_container_workspace_to_host(config):
    root = _enable_profile(config)
    container_root = "/opt/data/profiles/dev-bot/workspace"
    config.profiles["dev-bot"].path_mappings = [
        PathMapping(container_root=container_root, host_root=root)
    ]
    child = root / "repo"
    child.mkdir()

    resolved = resolve_profile_request(
        access_token=_access_token(),
        config=config,
        assistant_id=None,
        cwd=f"{container_root}/repo",
        agent=None,
        mode=None,
        context=None,
    )

    assert resolved.cwd == str(child)


def test_profile_does_not_map_entire_container_data_root(config):
    _enable_profile(config)

    with pytest.raises(ValueError, match="existing directory|not allowed"):
        resolve_profile_request(
            access_token=_access_token(),
            config=config,
            assistant_id=None,
            cwd="/opt/data",
            agent=None,
            mode=None,
            context=None,
        )
