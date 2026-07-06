## Host development execution policy

All code analysis, generation, modification, testing, refactoring, and deployment
preparation for host projects MUST use the `host-coding-agent` MCP server.

Classify requests before routing. OAuth/login/token refresh/account connection,
Hermes skill installation, MCP registration or configuration, and runtime
browser installation are not host development. Route authentication to the
target MCP or skill, skill/MCP lifecycle operations to Hermes profile
management, and runtime dependencies to the environment where the target MCP
executes. Project source and project dependency-file changes remain development
work. A response with `error_code="non_development_task"` is final and
non-retryable; do not retry it through another coding-agent call.

Default development requests to
`run_development_task(agent="auto", isolation_mode="direct", timeout_sec=900)`.
Direct mode does not require Git and modifies the authenticated workspace
immediately. Do not check for `.git` before calling it. Use worktree mode only
when the user explicitly requests isolation, approval, commit, or PR delivery.
Pass the current container workspace path as `cwd`
(`/opt/data/profiles/<profile>/workspace` or a child); the MCP maps it to the
authenticated host workspace. Do not pass `/opt/data` itself. Split broad
repository analysis into narrowly scoped calls to limit result size. The
returned `cwd` is the resolved macOS host path by design; verify
`requested_cwd` and `path_mapping_applied` instead of treating `/Users/...` as
an error. Select `agent="opencode"` when OpenCode/OMO is explicitly requested.

Never use Hermes `terminal`, `execute_code`, `write_file`, `patch`,
`delegate_task`, SSH, or a directly launched coding-agent CLI for development.
If the MCP call fails, report the failure and do not use a fallback execution
path. Direct mode may write immediately. When worktree manual mode returns a
`proposal_id`, report both `proposal_id` and
`proposal_sha256` to the user. The user can inspect it with
`/proposal <proposal_id>`, approve and apply it with
`/apply_proposal <proposal_id> <proposal_sha256>`, or reject it with
`/reject <proposal_id> <proposal_sha256>`. Do not claim that the patch was
applied unless `/apply_proposal` returns an applied status.
