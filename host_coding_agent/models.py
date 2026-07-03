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


class WorktreeStatus(str, Enum):
    created = "created"
    active = "active"
    tested = "tested"
    proposed = "proposed"
    delivered = "delivered"
    failed = "failed"
    abandoned = "abandoned"


class DeliveryMode(str, Enum):
    manual = "manual"
    auto = "auto"
    commit = "commit"
    pr = "pr"
    report = "report"


class IsolationMode(str, Enum):
    direct = "direct"
    worktree = "worktree"


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


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_env: str = Field(min_length=1, max_length=200)
    allowed_roots: list[Path] = Field(default_factory=list)
    allowed_container_roots: list[Path] = Field(default_factory=list)
    runtime_labels: dict[str, str] = Field(default_factory=dict)
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
    allowed_delivery_modes: list[DeliveryMode] = Field(
        default_factory=lambda: [
            DeliveryMode.manual,
            DeliveryMode.auto,
            DeliveryMode.commit,
        ]
    )
    allowed_remote_names: list[str] = Field(default_factory=lambda: ["origin"])
    allowed_remote_hosts: list[str] = Field(default_factory=lambda: ["github.com"])
    allow_git_push: bool = False
    allow_pull_requests: bool = False
    allowed_isolation_modes: list[IsolationMode] = Field(
        default_factory=lambda: [IsolationMode.worktree]
    )
    default_isolation_mode: IsolationMode = IsolationMode.worktree
    git_author_name: str = Field(
        default="host-coding-agent",
        min_length=1,
        max_length=200,
    )
    git_author_email: str = Field(
        default="host-coding-agent@localhost",
        min_length=3,
        max_length=320,
    )
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


class WorktreeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: Path = Path("~/.cache/host-coding-agent/worktrees")
    state_path: Path = Path("artifacts/worktrees.db")
    branch_prefix: str = Field(default="hca", pattern=r"^[A-Za-z0-9._-]+$")
    ttl_sec: int = Field(default=86_400, ge=300, le=2_592_000)
    policy_file: str = Field(
        default=".host-coding-agent.yaml",
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    require_tests: bool = True
    max_test_timeout_sec: int = Field(default=900, ge=1, le=3600)
    max_test_output_chars: int = Field(default=100_000, ge=1_000, le=1_000_000)


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
    worktrees: WorktreeConfig = Field(default_factory=WorktreeConfig)


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
    requested_cwd: str | None = None
    path_mapping_applied: bool = False
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


class WorktreeJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    profile: str
    repository: Path
    worktree: Path
    branch: str
    base_commit: str
    task_hash: str
    delivery_mode: DeliveryMode
    status: WorktreeStatus
    created_at: str
    expires_at: str


class ProjectTestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[list[str]] = Field(min_length=1, max_length=50)
    timeout_sec: int = Field(default=300, ge=1, le=3600)


class ProjectPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    tests: ProjectTestConfig


class TestCommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    command_index: int
    command: list[str]
    ok: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_sec: float = 0.0
    timed_out: bool = False
    redacted: bool = False


class WorktreeTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    ok: bool
    policy_commit: str
    policy_file: str
    results: list[TestCommandResult] = Field(default_factory=list)
    error: str | None = None


class WorktreeProposalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    ok: bool
    proposal_id: str | None = None
    proposal_sha256: str | None = None
    apply_command: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    error: str | None = None


class WorktreeCleanupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    ok: bool
    worktree_removed: bool = False
    branch_removed: bool = False
    error: str | None = None
