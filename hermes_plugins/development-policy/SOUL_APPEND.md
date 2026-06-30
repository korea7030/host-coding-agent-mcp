## Host development execution policy

All code analysis, generation, modification, testing, refactoring, and deployment
preparation for host projects MUST use the `host-coding-agent` MCP server.

Default to `run_coding_agent(agent="auto", mode="propose_patch",
timeout_sec=900)`. Pass the current container workspace path as `cwd`
(`/opt/data/profiles/<profile>/workspace` or a child); the MCP maps it to the
authenticated host workspace. Do not pass `/opt/data` itself. Split broad
repository analysis into narrowly scoped calls to limit result size. Use
`run_opencode` with `ultrawork` for OMO orchestration when explicitly requested.

Never use Hermes `terminal`, `execute_code`, `write_file`, `patch`,
`delegate_task`, SSH, or a directly launched coding-agent CLI for development.
If the MCP call fails, report the failure and do not use a fallback execution
path. Coding agents remain read-only until a separate human approval is
verified. When MCP returns a `proposal_id`, report both `proposal_id` and
`proposal_sha256` to the user and do not claim that the patch was applied.
