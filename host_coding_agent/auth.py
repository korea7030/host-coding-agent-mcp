from __future__ import annotations

import os
import secrets

from fastmcp.server.auth import AccessToken, TokenVerifier

from .config import ConfigError
from .models import AppConfig


class ProfileTokenVerifier(TokenVerifier):
    """Verify opaque bearer tokens loaded from environment variables."""

    def __init__(self, tokens: dict[str, str], required_scope: str):
        super().__init__(required_scopes=[required_scope])
        self._tokens = tokens
        self._required_scope = required_scope

    async def verify_token(self, token: str) -> AccessToken | None:
        for expected, profile_name in self._tokens.items():
            if secrets.compare_digest(token, expected):
                return AccessToken(
                    token=token,
                    client_id=profile_name,
                    subject=profile_name,
                    scopes=[self._required_scope],
                    claims={"profile": profile_name},
                )
        return None


def build_auth_provider(config: AppConfig) -> ProfileTokenVerifier | None:
    if not config.auth.enabled:
        return None

    tokens: dict[str, str] = {}
    for profile_name, profile in config.profiles.items():
        token = os.environ.get(profile.token_env, "")
        if not token:
            raise ConfigError(
                f"missing bearer token environment variable: {profile.token_env}"
            )
        if len(token) < 32:
            raise ConfigError(
                f"bearer token for profile {profile_name!r} must be at least 32 characters"
            )
        if token in tokens:
            raise ConfigError("bearer tokens must be unique per profile")
        tokens[token] = profile_name

    return ProfileTokenVerifier(tokens, config.auth.required_scope)
