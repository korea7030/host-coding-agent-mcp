from __future__ import annotations

from dataclasses import dataclass

from fastmcp.server.auth import AccessToken

from .config import ConfigError, validate_profile_cwd
from .models import (
    AgentName,
    AppConfig,
    ExecutionContext,
    ProfileConfig,
    RunMode,
)
from .security import SecurityViolation


@dataclass(frozen=True)
class ResolvedRequest:
    profile_name: str
    profile: ProfileConfig
    cwd: str
    agent: AgentName
    mode: RunMode
    context: ExecutionContext


def authenticated_profile(access_token: AccessToken | None, config: AppConfig) -> str:
    if not config.auth.enabled:
        return "anonymous"
    if access_token is None:
        raise SecurityViolation("authentication required")
    profile_name = access_token.claims.get("profile") or access_token.client_id
    if not isinstance(profile_name, str) or profile_name not in config.profiles:
        raise SecurityViolation("authenticated profile is invalid")
    return profile_name


def merge_context(
    defaults: ExecutionContext, supplied: ExecutionContext | None
) -> ExecutionContext:
    if supplied is None:
        return defaults.model_copy(deep=True)
    return defaults.model_copy(
        update=supplied.model_dump(exclude_none=True),
        deep=True,
    )


def resolve_profile_request(
    *,
    access_token: AccessToken | None,
    config: AppConfig,
    assistant_id: str | None,
    cwd: str | None,
    agent: AgentName | None,
    mode: RunMode | None,
    context: ExecutionContext | None,
) -> ResolvedRequest:
    profile_name = authenticated_profile(access_token, config)
    if not config.auth.enabled:
        raise ConfigError("profile resolution requires authentication to be enabled")
    if assistant_id is not None and assistant_id != profile_name:
        raise SecurityViolation("assistant_id does not match authenticated profile")

    profile = config.profiles[profile_name]
    resolved_agent = agent or profile.default_agent
    resolved_mode = mode or profile.default_mode
    resolved_cwd_value = cwd or (
        str(profile.default_cwd) if profile.default_cwd is not None else None
    )
    if resolved_cwd_value is None:
        raise ConfigError("cwd is required because this profile has no default_cwd")
    if (
        resolved_agent != AgentName.auto
        and resolved_agent not in profile.allowed_agents
    ):
        raise SecurityViolation("agent is not allowed for this profile")
    if resolved_mode not in profile.allowed_modes:
        raise SecurityViolation("mode is not allowed for this profile")

    resolved_cwd = validate_profile_cwd(resolved_cwd_value, profile_name, config)
    return ResolvedRequest(
        profile_name=profile_name,
        profile=profile,
        cwd=str(resolved_cwd),
        agent=resolved_agent,
        mode=resolved_mode,
        context=merge_context(profile.context, context),
    )
