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
    worktree_root = config.worktrees.root.expanduser()
    if not worktree_root.is_absolute():
        raise ConfigError("worktrees.root must be absolute")
    config.worktrees.root = Path(os.path.realpath(worktree_root))
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
        if not profile.allowed_roots and not profile.allowed_container_roots:
            raise ConfigError(
                f"profile {name!r} requires a host or container allowed root"
            )
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
        normalized_container_roots: list[Path] = []
        for container_root in profile.allowed_container_roots:
            container_root = container_root.expanduser()
            if not container_root.is_absolute():
                raise ConfigError(
                    f"profile {name!r} allowed_container_root must be absolute"
                )
            container_root = Path(os.path.normpath(container_root))
            if container_root not in normalized_container_roots:
                normalized_container_roots.append(container_root)
        profile.allowed_container_roots = normalized_container_roots
        if profile.allowed_container_roots and not profile.runtime_labels:
            raise ConfigError(
                f"profile {name!r} requires trusted runtime_labels"
            )
        if profile.default_cwd is not None:
            raw_default = profile.default_cwd.expanduser()
            if not raw_default.is_absolute():
                raise ConfigError(f"profile {name!r} default_cwd must be absolute")
            normalized_default = Path(os.path.normpath(raw_default))
            is_container_default = any(
                normalized_default == root
                or normalized_default.is_relative_to(root)
                for root in profile.allowed_container_roots
            )
            if is_container_default:
                profile.default_cwd = normalized_default
            else:
                profile.default_cwd = _canonical_directory(raw_default)
            if not is_container_default and not any(
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
        if not profile.allowed_delivery_modes:
            raise ConfigError(
                f"profile {name!r} requires at least one allowed delivery mode"
            )
        if not profile.allowed_isolation_modes:
            raise ConfigError(
                f"profile {name!r} requires at least one allowed isolation mode"
            )
        if profile.default_isolation_mode not in profile.allowed_isolation_modes:
            raise ConfigError(
                f"profile {name!r} default isolation mode is not allowed"
            )
        if any(
            not remote.strip() or any(char.isspace() for char in remote)
            for remote in profile.allowed_remote_names
        ):
            raise ConfigError(f"profile {name!r} has an invalid remote name")
        if any(
            not host.strip() or "/" in host or "@" in host
            for host in profile.allowed_remote_hosts
        ):
            raise ConfigError(f"profile {name!r} has an invalid remote host")
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
    value: str | Path,
    profile_name: str,
    config: AppConfig,
    runtime_registry=None,
) -> Path:
    profile = config.profiles.get(profile_name)
    if profile is None:
        raise ConfigError("unknown authenticated profile")
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ConfigError("cwd must be an absolute path")
    normalized = Path(os.path.normpath(raw))
    is_container_path = any(
        normalized == root or normalized.is_relative_to(root)
        for root in profile.allowed_container_roots
    )
    if is_container_path:
        if runtime_registry is None:
            raise ConfigError("Docker runtime resolver is unavailable")
        raw = runtime_registry.resolve(
            profile_name=profile_name,
            container_path=normalized,
        )
    resolved = validate_cwd(raw, config)
    if not any(
        resolved == root or resolved.is_relative_to(root)
        for root in profile.allowed_roots
    ):
        raise ConfigError("cwd is not allowed for this profile")
    return resolved
