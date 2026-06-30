from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import AgentName, AppConfig


class ConfigError(ValueError):
    pass


def _canonical_directory(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ConfigError(f"path must be absolute: {raw}")
    resolved = Path(os.path.realpath(raw))
    if not resolved.is_dir():
        raise ConfigError(f"path is not an existing directory: {raw}")
    return resolved


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config = AppConfig.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid config: {exc}") from exc

    config.security.allowed_roots = [
        _canonical_directory(path) for path in config.security.allowed_roots
    ]
    # Denied paths may not exist on every machine. Canonicalize without requiring existence.
    config.security.denied_paths = [
        Path(os.path.realpath(Path(path).expanduser())) for path in config.security.denied_paths
    ]
    if not config.security.allowed_roots:
        raise ConfigError("at least one allowed root is required")
    if config.auth.enabled and not config.profiles:
        raise ConfigError("at least one profile is required when auth is enabled")
    for name, profile in config.profiles.items():
        if not name.strip():
            raise ConfigError("profile name must not be empty")
        profile.allowed_roots = [
            _canonical_directory(path) for path in profile.allowed_roots
        ]
        if not profile.allowed_roots:
            raise ConfigError(f"profile {name!r} requires at least one allowed root")
        for root in profile.allowed_roots:
            if not any(
                root == global_root or root.is_relative_to(global_root)
                for global_root in config.security.allowed_roots
            ):
                raise ConfigError(
                    f"profile {name!r} root is outside global allowed roots: {root}"
                )
            if any(
                root == denied or root.is_relative_to(denied)
                for denied in config.security.denied_paths
            ):
                raise ConfigError(f"profile {name!r} root is denied: {root}")
        seen_container_roots: set[Path] = set()
        for mapping in profile.path_mappings:
            container_root = mapping.container_root.expanduser()
            if not container_root.is_absolute():
                raise ConfigError(
                    f"profile {name!r} container_root must be absolute"
                )
            container_root = Path(os.path.normpath(container_root))
            if container_root in seen_container_roots:
                raise ConfigError(
                    f"profile {name!r} has duplicate container_root: {container_root}"
                )
            seen_container_roots.add(container_root)
            mapping.container_root = container_root
            mapping.host_root = _canonical_directory(mapping.host_root)
            if not any(
                mapping.host_root == root or mapping.host_root.is_relative_to(root)
                for root in profile.allowed_roots
            ):
                raise ConfigError(
                    f"profile {name!r} mapped host_root is outside its allowed roots"
                )
        if profile.default_cwd is not None:
            profile.default_cwd = _canonical_directory(profile.default_cwd)
            if not any(
                profile.default_cwd == root
                or profile.default_cwd.is_relative_to(root)
                for root in profile.allowed_roots
            ):
                raise ConfigError(
                    f"profile {name!r} default_cwd is outside its allowed roots"
                )
        if AgentName.auto in profile.allowed_agents:
            raise ConfigError(
                f"profile {name!r} allowed_agents must contain concrete agents only"
            )
        if (
            profile.default_agent != AgentName.auto
            and profile.default_agent not in profile.allowed_agents
        ):
            raise ConfigError(
                f"profile {name!r} default_agent is not in allowed_agents"
            )
        if profile.default_mode not in profile.allowed_modes:
            raise ConfigError(
                f"profile {name!r} default_mode is not in allowed_modes"
            )
    return config


def validate_cwd(value: str | Path, config: AppConfig) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ConfigError("cwd must be an absolute path")
    resolved = _canonical_directory(raw)
    if not any(resolved == root or resolved.is_relative_to(root) for root in config.security.allowed_roots):
        raise ConfigError("cwd is not allowed")
    if any(resolved == denied or resolved.is_relative_to(denied) for denied in config.security.denied_paths):
        raise ConfigError("cwd is denied")
    return resolved


def validate_profile_cwd(
    value: str | Path, profile_name: str, config: AppConfig
) -> Path:
    profile = config.profiles.get(profile_name)
    if profile is None:
        raise ConfigError("unknown authenticated profile")
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ConfigError("cwd must be an absolute path")
    normalized = Path(os.path.normpath(raw))
    matches = [
        mapping
        for mapping in profile.path_mappings
        if normalized == mapping.container_root
        or normalized.is_relative_to(mapping.container_root)
    ]
    if matches:
        mapping = max(matches, key=lambda item: len(item.container_root.parts))
        relative = normalized.relative_to(mapping.container_root)
        raw = mapping.host_root / relative
    resolved = validate_cwd(raw, config)
    if not any(
        resolved == root or resolved.is_relative_to(root)
        for root in profile.allowed_roots
    ):
        raise ConfigError("cwd is not allowed for this profile")
    return resolved
