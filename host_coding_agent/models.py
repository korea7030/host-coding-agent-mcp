from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class AgentName(str, Enum):
    auto = "auto"
    antigravity = "antigravity"
    codex = "codex"
    opencode = "opencode"


class RunMode(str, Enum):
    read_only = "read_only"
    propose_patch = "propose_patch"
    apply_patch = "apply_patch"


class ExecutionContext(BaseModel):
    """Per-call project preferences supplied by the invoking assistant."""

    model_config = ConfigDict(extra="forbid")
    language: str | None = Field(default=None, max_length=100)
    environment: str | None = Field(default=None, max_length=500)
    runtime: str | None = Field(default=None, max_length=100)
    runtime_version: str | None = Field(default=None, max_length=100)
    framework: str | None = Field(default=None, max_length=100)
    package_manager: str | None = Field(default=None, max_length=100)
    test_command: str | None = Field(default=None, max_length=500)


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    required_scope: str = "host-coding-agent"


class PathMapping(BaseModel):
    """A profile-scoped container path alias for a host workspace."""

    model_config = ConfigDict(extra="forbid")
    container_root: Path
    host_root: Path


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_env: str = Field(min_length=1, max_length=200)
    allowed_roots: list[Path]
    path_mappings: list[PathMapping] = Field(default_factory=list)
    approval_identities: list[str] = Field(default_factory=list)
    allowed_agents: list[AgentName] = Field(
        default_factory=lambda: [AgentName.codex, AgentName.opencode]
    )
    allowed_modes: list[RunMode] = Field(
        default_factory=lambda: [RunMode.read_only, RunMode.propose_patch]
    )
    default_cwd: Path | None = None
    default_agent: AgentName = AgentName.auto
    default_mode: RunMode = RunMode.propose_patch
    context: ExecutionContext = Field(default_factory=ExecutionContext)


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = 8787
    path: str = "/mcp"
    mask_error_details: bool = True


class SecurityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_mode: RunMode = RunMode.propose_patch
    allow_apply_patch: bool = False
    max_timeout_sec: int = 1800
    max_output_chars: int = 200_000
    allowed_roots: list[Path]
    denied_paths: list[Path] = Field(default_factory=list)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    command: str
    default_args: list[str] = Field(default_factory=list)
    priority: int = 100


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_order: list[AgentName]
    codex_keywords: list[str] = Field(default_factory=list)
    opencode_keywords: list[str] = Field(default_factory=list)


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path = Path("logs/calls.jsonl")
    task_preview_chars: int = 0


class ArtifactConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path = Path("artifacts/proposals.db")
    proposal_ttl_sec: int = Field(default=86_400, ge=300, le=2_592_000)
    max_diff_chars: int = Field(default=1_000_000, ge=1_000, le=10_000_000)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: ServerConfig
    security: SecurityConfig
    agents: dict[AgentName, AgentConfig]
    routing: RoutingConfig
    auth: AuthConfig = Field(default_factory=AuthConfig)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    artifacts: ArtifactConfig = Field(default_factory=ArtifactConfig)


class AttemptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: AgentName
    ok: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_sec: float = 0.0
    timed_out: bool = False
    command: list[str] = Field(default_factory=list)


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    selected_agent: AgentName | None = None
    assistant_id: str | None = None
    context: ExecutionContext | None = None
    cwd: Path
    mode: RunMode
    stdout: str = ""
    stderr: str = ""
    summary: str = ""
    proposed_diff: str = ""
    proposal_id: str | None = None
    proposal_sha256: str | None = None
    artifact_error: str | None = None
    redacted: bool = False
    results: list[AttemptResult] = Field(default_factory=list)
    error: str | None = None
