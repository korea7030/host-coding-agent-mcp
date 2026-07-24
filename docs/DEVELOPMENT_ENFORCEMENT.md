# Development enforcement policy

## Objective

Hermes profiles must not perform software-development work with native tools or
by launching coding-agent CLIs directly. Host development must go through the
authenticated `host-coding-agent` MCP server.

## Enforcement layers

1. A Hermes `pre_llm_call` hook injects the routing requirement on every turn.
2. A Hermes `pre_tool_call` hook blocks native mutation and execution tools
   before dispatch.
3. Hermes containers receive only their profile-scoped `/opt/data` mount, not
   arbitrary host project roots.
4. The MCP server enforces profile identity, allowed roots, agents, isolation
   modes, and process sandboxes.
5. If MCP execution fails, Hermes must report the failure and must not fall
   back to native development tools.
6. If `check_execution_health.ok=false`, Hermes must not start development
   execution until the reported runtime/cwd/sandbox/worktree blocker is fixed.
7. If MCP tool calls fail with stream/client errors, Hermes may check
   `GET /healthz` or `GET /readyz`; if health is ok, treat the issue as HTTP
   stream reconnect state, not server-down.

## Allowed development path

- `mcp_host_coding_agent_check_host_coding_agents`
- `mcp_host_coding_agent_check_execution_health`
- `mcp_host_coding_agent_start_development_task`
- `mcp_host_coding_agent_get_async_job`
- `mcp_host_coding_agent_get_async_job_events`
- `mcp_host_coding_agent_run_development_task`
- `mcp_host_coding_agent_run_coding_agent`
- `mcp_host_coding_agent_run_antigravity`
- `mcp_host_coding_agent_run_codex`
- `mcp_host_coding_agent_run_opencode`

The normal sequence is:

```text
check_host_coding_agents
→ check_execution_health
→ start_development_task(agent=<explicit selected agent>, ...)
→ get_async_job_events
```

Use a concrete selected agent such as `opencode`, `codex`, or `antigravity` for
interactive development. `agent="auto"` remains only for existing automation
compatibility. Direct mode does not require Git and modifies the mapped
workspace immediately; use `direct_write_policy=fail_if_changed` for read-only
intent. Worktree mode is selected only for explicit isolation, approval, commit,
PR, or report-only review requests.
Hermes passes its container workspace path as `cwd`; the MCP translates only
the profile-scoped workspace root. The gateway registers its container ID
outside the LLM tool loop, and the MCP derives the host path from Docker
`Mounts[].Destination` and `Mounts[].Source`. `/opt/data` as a whole is never
allowed. Docker labels bind the registered container identity to the
authenticated profile. Existing profile-authorized host paths bypass container
translation.

## Blocked native tools

- `terminal`
- `execute_code`
- `write_file`
- `patch`
- `delegate_task`

These tools are blocked unconditionally while the plugin is enabled. This is
intentionally fail-closed and means the profile cannot use them for non-coding
tasks either.

Read-only tools such as `read_file` and `search_files` remain available for the
container workspace. Native mutation and execution tools remain blocked.

## Direct and approval boundaries

Direct mode is a deliberate immediate-write path and does not create a proposal
or require approval. The profile must explicitly allow direct isolation.

In worktree manual mode, patch application is performed only by the external
Telegram `/apply_proposal` gateway command. Internally Hermes maps the
Telegram-compatible underscore to the plugin command `apply-proposal`. The host
endpoint:

- authenticates the profile bearer token and Telegram user allowlist;
- binds approval to profile, user, workspace, proposal hash, and expiry;
- rejects replay, stale base files, changed Git HEAD, path traversal, symlink
  escapes, and binary patches;
- runs `git apply --check` before applying;
- stores append-only approval events and result hashes;
- reverses the patch if post-apply audit completion fails.

An LLM-visible MCP tool call is not proof of human approval.

In worktree report mode, the MCP creates an immutable proposal for review but
does not create approval, commit, PR, or modify the original workspace. The
worktree remains available until explicitly abandoned or cleaned up by expiry.
