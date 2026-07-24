---
name: host-coding-agent
description: Route host-project software development through the authenticated host-coding-agent MCP, with explicit agent selection and policy-enforced isolation.
---

# Host Coding Agent

Use the `host-coding-agent` MCP server for code analysis, generation, modification,
testing, refactoring, and deployment preparation in host projects.

## Required routing sequence

1. Call `check_host_coding_agents` before any development execution.
2. Call `check_execution_health` for the target `cwd` and intended isolation
   mode. Do not start development if `ok=false`; report the
   `recommended_next_action` and blocker details instead.
3. Present the available configured agents (`antigravity`, `codex`, and/or
   `opencode`) and ask the user to explicitly choose one. Do not silently select
   `auto`, infer a preference, or start development before the user chooses.
4. Confirm the target workspace, requested write/isolation behavior, and delivery
   expectation when they are not already explicit.
5. Prefer `start_development_task` for execution. Pass the user's explicit agent
   name, not `auto`.
6. Poll `get_async_job_events` and `get_async_job` until the job reaches a
   terminal state.
7. Report the selected agent, resolved workspace, isolation mode, files/results,
   and any proposal, approval, or job identifiers returned by the server.

If the selected agent is unavailable or disallowed, report that result and ask the
user to select from the remaining available agents. Never bypass the MCP by
launching a coding-agent CLI directly.

## Current MCP tools

Use these names exactly as exposed by the server:

- Discovery: `check_host_coding_agents`
- Execution health: `check_execution_health`
- Asynchronous workflow: `start_development_task`, `get_async_job`,
  `get_async_job_events`, `list_async_jobs`
- Synchronous compatibility workflow: `run_development_task`
- Managed worktree workflow: `create_development_job`, `run_development_job`,
  `test_development_job`, `propose_development_job`,
  `deliver_development_job`, `get_development_job`,
  `list_development_jobs`, `abandon_development_job`
- Legacy/read-only and proposal workflow: `run_coding_agent`,
  `get_patch_proposal`, `list_patch_proposals`
- Agent-specific entry points: `run_antigravity`, `run_codex`, `run_opencode`

Standard execution flow:

```text
check_host_coding_agents
→ check_execution_health
→ start_development_task
→ get_async_job_events
→ get_async_job
→ cancel_async_job when the user asks to stop a queued/running job
```

Prefer `start_development_task` for ordinary development after explicit agent
selection and a successful execution health result. `check_host_coding_agents`
returns CLI availability plus a compact `execution_health` summary by default,
but a detailed `check_execution_health` call is still the standard preflight for
execution. Direct mode modifies the resolved workspace immediately and returns
`changed_files`. If the user asks to inspect or verify without changes but
direct mode is still used, pass `direct_write_policy: fail_if_changed`; report
any `direct_write_policy_violation` as a failed safety check, not as successful
development. `start_development_task` returns a job identifier immediately. Poll
`get_async_job` until `status` is `succeeded` or `failed`, and call
`get_async_job_events` with the latest `next_after` cursor to explain the current
stage without repeating old events. Read the final development response from the
job's `result`. Use `list_async_jobs` to recover recent identifiers after a
client interruption. If the user asks to stop a job, call `cancel_async_job`.
Cancellation marks the job as terminal with `status=failed` and
`stage=cancelled`; it does not guarantee OS-level process termination.

If MCP tool calls fail with `ClosedResourceError` or another HTTP stream/client
error, check `GET /healthz` or `GET /readyz` outside the MCP stream. If
`ok=true`, report a stream reconnect/client-state issue instead of claiming that
the host-coding-agent server is down.

Use synchronous `run_development_task` only for compatibility or bounded work.
Use the staged development-job tools when the user needs manual control over
worktree creation, testing, proposal, or delivery. Use read-only mode when no
file changes are requested.

## Security and approval boundaries

- Every MCP HTTP request is authenticated with a profile-specific Bearer token.
  The token determines the profile; never copy, print, persist, or request the
  token, and do not attempt to impersonate another `assistant_id`.
- `cwd` must pass both global and authenticated-profile allowed-root checks.
  Container paths may be translated to canonical host paths. A returned host
  path is expected; use `requested_cwd` and `path_mapping_applied` to explain it.
- Agent, run mode, isolation mode, delivery mode, context, and defaults remain
  bounded by profile and global policy. Do not work around a policy rejection.
- Read-only/proposal execution uses agent or macOS read-only sandboxes.
  Direct/worktree writes are limited to the authenticated workspace by the
  profile and process sandbox.
- `direct` can modify the original workspace immediately. It creates no
  worktree, proposal, commit, approval, or automatic rollback. Get clear user
  intent before using it for writes.
- `worktree` isolates changes. Manual delivery requires external human approval.
  Return `proposal_id` and `proposal_sha256` exactly, and do not claim application
  until the external approval/apply path reports an applied/delivered state.
  Report delivery creates an immutable proposal for review only; it does not
  create approval, commit, PR, or modify the original workspace.
- An LLM-visible tool call is not human approval. Do not invoke or simulate the
  external Telegram approval identity or patch applier.
- General MCP `apply_patch` is disabled by policy. Do not seek a fallback write
  path if an MCP operation fails.
- The server invokes subprocesses without a shell, enforces timeouts and process
  sandboxes, checks secret patterns, redacts output, and records limited audit
  metadata. Still avoid putting credentials or secret values in tasks/context.

A `non_development_task` classification is final for this route. Handle account
login, token refresh, MCP/skill lifecycle management, and runtime installation in
the appropriate system instead of retrying them as coding tasks.
