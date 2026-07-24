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

Use this standard development sequence:

```text
check_host_coding_agents
→ check_execution_health
→ start_development_task(agent=<explicit selected agent>, ...)
→ get_async_job_events
→ get_async_job
```

For interactive requests, present `selectable_agents` and pass the user's
explicit choice such as `opencode`, `codex`, or `antigravity`. Do not silently
default to `auto`; `agent="auto"` is only for existing automation compatibility.
If `check_execution_health.ok=false`, report the blocker and
`recommended_next_action` instead of starting development. Direct mode does not
require Git and modifies the authenticated workspace immediately. Do not check
for `.git` before direct execution. Use `direct_write_policy=fail_if_changed`
when the user intent is read-only verification. Use worktree mode only when the
user explicitly requests isolation, report-only review, approval, commit, or PR
delivery.
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
path. If MCP calls fail with `ClosedResourceError` or another HTTP stream/client
error, check `GET /healthz` or `GET /readyz`; if `ok=true`, report an HTTP stream
reconnect/client-state issue instead of claiming that the server is down. Direct
mode may write immediately. When worktree manual mode returns a
`proposal_id`, report both `proposal_id` and
`proposal_sha256` to the user. The user can inspect it with
`/proposal <proposal_id>`, approve and apply it with
`/apply_proposal <proposal_id> <proposal_sha256>`, or reject it with
`/reject <proposal_id> <proposal_sha256>`. Do not claim that the patch was
applied unless `/apply_proposal` returns an applied status.

Worktree report delivery creates an immutable proposal for review but does not
create approval, commit, PR, or modify the original workspace.
