from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError
from .models import AppConfig


_CONTAINER_ID = re.compile(r"^[a-f0-9]{12,64}$")


@dataclass(frozen=True)
class DockerMount:
    destination: Path
    source: Path
    read_write: bool


@dataclass
class RuntimeRegistration:
    container_id: str
    mounts: tuple[DockerMount, ...]
    labels: dict[str, str]
    registered_at: float


class RuntimeRegistry:
    def __init__(
        self,
        config: AppConfig,
        *,
        cache_ttl_sec: int = 30,
        state_path: Path | None = None,
    ):
        self.config = config
        self.cache_ttl_sec = cache_ttl_sec
        self.state_path = state_path
        self._registrations: dict[str, RuntimeRegistration] = {}
        self._base_security_roots = set(config.security.allowed_roots)
        self._base_profile_roots = {
            name: set(profile.allowed_roots)
            for name, profile in config.profiles.items()
        }
        self._dynamic_roots_by_profile: dict[str, set[Path]] = {}
        self._restore()

    def register_docker(self, *, profile_name: str, container_id: str) -> dict[str, Any]:
        if profile_name not in self.config.profiles:
            raise ConfigError("unknown authenticated profile")
        if not _CONTAINER_ID.fullmatch(container_id):
            raise ConfigError("invalid Docker container ID")
        registration = self._inspect(container_id)
        self._validate_identity(profile_name, registration)
        dynamic_roots = self._dynamic_roots(profile_name, registration.mounts)
        self._registrations[profile_name] = registration
        self._persist()
        self._set_dynamic_roots(profile_name, dynamic_roots)
        return {
            "runtime": "docker",
            "container_id": registration.container_id[:12],
            "mount_count": len(registration.mounts),
            "workspace_roots": [str(path) for path in dynamic_roots],
        }

    def resolve(self, *, profile_name: str, container_path: str | Path) -> Path:
        profile = self.config.profiles.get(profile_name)
        if profile is None:
            raise ConfigError("unknown authenticated profile")
        raw = Path(container_path)
        if not raw.is_absolute():
            raise ConfigError("cwd must be an absolute path")
        normalized = Path(os.path.normpath(raw))
        if not any(
            normalized == root or normalized.is_relative_to(root)
            for root in profile.allowed_container_roots
        ):
            raise ConfigError("container cwd is not allowed for this profile")
        registration = self._registrations.get(profile_name)
        if registration is None:
            raise ConfigError("Docker runtime is not registered for this profile")
        if time.monotonic() - registration.registered_at > self.cache_ttl_sec:
            registration = self._inspect(registration.container_id)
            self._validate_identity(profile_name, registration)
            self._set_dynamic_roots(
                profile_name,
                self._dynamic_roots(profile_name, registration.mounts),
            )
            self._registrations[profile_name] = registration
        matches = [
            mount
            for mount in registration.mounts
            if normalized == mount.destination
            or normalized.is_relative_to(mount.destination)
        ]
        if not matches:
            raise ConfigError("container cwd is not covered by a Docker bind mount")
        mount = max(matches, key=lambda item: len(item.destination.parts))
        candidate = mount.source / normalized.relative_to(mount.destination)
        resolved = Path(os.path.realpath(candidate))
        dynamic_roots = self._dynamic_roots(profile_name, registration.mounts)
        if not any(
            resolved == root or resolved.is_relative_to(root)
            for root in dynamic_roots
        ):
            raise ConfigError("resolved Docker cwd is outside the allowed workspace")
        if not resolved.is_dir():
            raise ConfigError(f"resolved Docker cwd does not exist: {normalized}")
        if any(
            resolved == denied or resolved.is_relative_to(denied)
            for denied in self.config.security.denied_paths
        ):
            raise ConfigError("resolved Docker cwd is denied")
        return resolved

    def _dynamic_roots(
        self,
        profile_name: str,
        mounts: tuple[DockerMount, ...],
    ) -> list[Path]:
        profile = self.config.profiles[profile_name]
        roots: list[Path] = []
        for container_root in profile.allowed_container_roots:
            matches = [
                mount
                for mount in mounts
                if container_root == mount.destination
                or container_root.is_relative_to(mount.destination)
            ]
            if not matches:
                raise ConfigError(
                    f"allowed container root is not mounted: {container_root}"
                )
            mount = max(matches, key=lambda item: len(item.destination.parts))
            root = Path(
                os.path.realpath(
                    mount.source / container_root.relative_to(mount.destination)
                )
            )
            if not root.is_dir():
                raise ConfigError(
                    f"mapped workspace root does not exist: {container_root}"
                )
            roots.append(root)
        return roots

    def _inspect(self, container_id: str) -> RuntimeRegistration:
        docker = shutil.which("docker")
        if not docker:
            raise ConfigError("Docker CLI is unavailable on the MCP host")
        try:
            completed = subprocess.run(
                [docker, "inspect", container_id],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigError(f"Docker inspect failed: {exc}") from exc
        if completed.returncode != 0:
            raise ConfigError("Docker container could not be inspected")
        try:
            inspected = json.loads(completed.stdout)[0]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ConfigError("Docker inspect returned invalid data") from exc
        if inspected.get("State", {}).get("Running") is not True:
            raise ConfigError("Docker container is not running")
        full_id = str(inspected.get("Id", ""))
        if not full_id.startswith(container_id):
            raise ConfigError("Docker inspect identity mismatch")
        mounts = []
        for item in inspected.get("Mounts", []):
            if item.get("Type") != "bind":
                continue
            destination = Path(str(item.get("Destination", "")))
            source = Path(str(item.get("Source", "")))
            if not destination.is_absolute() or not source.is_absolute():
                continue
            mounts.append(
                DockerMount(
                    destination=Path(os.path.normpath(destination)),
                    source=Path(os.path.realpath(source)),
                    read_write=bool(item.get("RW", False)),
                )
            )
        if not mounts:
            raise ConfigError("Docker container has no usable bind mounts")
        return RuntimeRegistration(
            container_id=full_id,
            mounts=tuple(mounts),
            labels={
                str(key): str(value)
                for key, value in inspected.get("Config", {}).get("Labels", {}).items()
            },
            registered_at=time.monotonic(),
        )

    def _validate_identity(
        self,
        profile_name: str,
        registration: RuntimeRegistration,
    ) -> None:
        expected = self.config.profiles[profile_name].runtime_labels
        if not expected:
            raise ConfigError("profile has no trusted Docker runtime labels")
        if any(registration.labels.get(key) != value for key, value in expected.items()):
            raise ConfigError("Docker container labels do not match the profile")

    def _persist(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    profile: registration.container_id
                    for profile, registration in self._registrations.items()
                },
                sort_keys=True,
            )
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.state_path)
        os.chmod(self.state_path, 0o600)

    def _set_dynamic_roots(
        self,
        profile_name: str,
        roots: list[Path],
    ) -> None:
        previous = self._dynamic_roots_by_profile.get(profile_name, set())
        current = set(roots)
        profile = self.config.profiles[profile_name]
        base_profile = self._base_profile_roots[profile_name]
        profile.allowed_roots = [
            root
            for root in profile.allowed_roots
            if root not in previous or root in base_profile or root in current
        ]
        for root in current:
            if root not in profile.allowed_roots:
                profile.allowed_roots.append(root)
        other_dynamic = set().union(
            *(
                values
                for name, values in self._dynamic_roots_by_profile.items()
                if name != profile_name
            ),
            current,
        )
        self.config.security.allowed_roots = [
            root
            for root in self.config.security.allowed_roots
            if root not in previous
            or root in self._base_security_roots
            or root in other_dynamic
        ]
        for root in current:
            if root not in self.config.security.allowed_roots:
                self.config.security.allowed_roots.append(root)
        self._dynamic_roots_by_profile[profile_name] = current

    def _restore(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, TypeError, json.JSONDecodeError):
            return
        if not isinstance(saved, dict):
            return
        for profile_name, container_id in saved.items():
            if profile_name not in self.config.profiles or not isinstance(container_id, str):
                continue
            try:
                registration = self._inspect(container_id)
                self._validate_identity(profile_name, registration)
                self._registrations[profile_name] = registration
                dynamic_roots = self._dynamic_roots(
                    profile_name,
                    registration.mounts,
                )
            except ConfigError:
                continue
            self._set_dynamic_roots(profile_name, dynamic_roots)
