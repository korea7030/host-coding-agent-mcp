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

The normal default is `run_coding_agent(agent="auto", mode="propose_patch")`.

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

The coding agents remain read-only. A future patch-application service must:

- persist immutable proposal artifacts with SHA-256 hashes;
- receive approval from a gateway command handled outside the LLM tool loop;
- bind approval to profile, user, workspace, proposal, and expiry;
- reject replay, stale base files, path traversal, and symlink escapes;
- run `git apply --check` before applying;
- retain an audit event and rollback artifact.

An LLM-visible MCP tool call is not proof of human approval.
