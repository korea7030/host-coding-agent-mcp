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

## Allowed development path

- `mcp_host_coding_agent_run_development_task`
- `mcp_host_coding_agent_run_coding_agent`
- `mcp_host_coding_agent_run_antigravity`
- `mcp_host_coding_agent_run_codex`
- `mcp_host_coding_agent_run_opencode`

The normal default is `run_development_task(agent="auto",
isolation_mode="direct", timeout_sec=900)`. Direct mode does not require Git
and modifies the mapped workspace immediately. Worktree mode is selected only
for explicit isolation, approval, commit, or PR requests.
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
