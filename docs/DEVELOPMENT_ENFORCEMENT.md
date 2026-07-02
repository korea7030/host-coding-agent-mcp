# Development enforcement policy

## Objective

Hermes profiles must not perform software-development work with native tools or
by launching coding-agent CLIs directly. Host development must go through the
authenticated `host-coding-agent` MCP server.

## Enforcement layers

1. A Hermes `pre_llm_call` hook injects the routing requirement on every turn.
2. A Hermes `pre_tool_call` hook blocks native mutation and execution tools
   before dispatch.
3. Hermes containers do not mount host project roots.
4. The MCP server enforces profile identity, allowed roots, agents, modes, and
   read-only/proposal sandboxes.
5. If MCP execution fails, Hermes must report the failure and must not fall
   back to native development tools.

## Allowed development path

- `mcp_host_coding_agent_run_coding_agent`
- `mcp_host_coding_agent_run_antigravity`
- `mcp_host_coding_agent_run_codex`
- `mcp_host_coding_agent_run_opencode`

The normal default is
`run_coding_agent(agent="auto", mode="propose_patch", timeout_sec=900)`.
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

Read-only tools such as `read_file` and `search_files` remain available, but
they cannot access host project roots because those roots are not mounted in
the Hermes containers.

## Approval boundary

The coding agents remain read-only. Patch application is performed only by the
external Telegram `/apply-proposal` gateway command. The host endpoint:

- authenticates the profile bearer token and Telegram user allowlist;
- binds approval to profile, user, workspace, proposal hash, and expiry;
- rejects replay, stale base files, changed Git HEAD, path traversal, symlink
  escapes, and binary patches;
- runs `git apply --check` before applying;
- stores append-only approval events and result hashes;
- reverses the patch if post-apply audit completion fails.

An LLM-visible MCP tool call is not proof of human approval.
