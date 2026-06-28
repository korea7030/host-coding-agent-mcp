from __future__ import annotations

import argparse
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token

from host_coding_agent import (
    AgentName,
    ConfigError,
    ExecutionContext,
    RunMode,
    SecurityViolation,
    check_agents,
    load_config,
    run_coding_agent as execute_agent,
)
from host_coding_agent.auth import build_auth_provider
from host_coding_agent.profiles import resolve_profile_request


def create_server(config_path: str | Path) -> tuple[FastMCP, object]:
    config = load_config(config_path)
    auth = build_auth_provider(config)
    mcp = FastMCP(
        "host-coding-agent",
        auth=auth,
        mask_error_details=config.server.mask_error_details,
    )

    def execute_profile_request(
        *,
        task: str,
        cwd: str | None,
        agent: AgentName | None,
        mode: RunMode | None,
        timeout_sec: int,
        assistant_id: str | None,
        context: ExecutionContext | None,
    ) -> dict:
        if config.auth.enabled:
            resolved = resolve_profile_request(
                access_token=get_access_token(),
                config=config,
                assistant_id=assistant_id,
                cwd=cwd,
                agent=agent,
                mode=mode,
                context=context,
            )
            result = execute_agent(
                task=task,
                cwd=resolved.cwd,
                agent=resolved.agent,
                mode=resolved.mode,
                timeout_sec=timeout_sec,
                config=config,
                assistant_id=resolved.profile_name,
                context=resolved.context,
                allowed_agents=set(resolved.profile.allowed_agents),
            )
        else:
            if cwd is None:
                raise ConfigError("cwd is required")
            result = execute_agent(
                task=task,
                cwd=cwd,
                agent=agent or AgentName.auto,
                mode=mode or RunMode.propose_patch,
                timeout_sec=timeout_sec,
                config=config,
                assistant_id=assistant_id,
                context=context,
            )
        return result.model_dump(mode="json")

    @mcp.tool
    def check_host_coding_agents() -> dict:
        """Check whether configured host coding-agent CLIs are available."""
        return check_agents(config)

    @mcp.tool
    def run_coding_agent(
        task: str,
        cwd: str | None = None,
        agent: AgentName | None = None,
        mode: RunMode | None = None,
        timeout_sec: int = 900,
        assistant_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> dict:
        """Run a host coding agent inside the configured workspace policy."""
        try:
            return execute_profile_request(
                task=task,
                cwd=cwd,
                agent=agent,
                mode=mode,
                timeout_sec=timeout_sec,
                assistant_id=assistant_id,
                context=context,
            )
        except (ConfigError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def run_antigravity(
        task: str, cwd: str | None = None, mode: RunMode | None = None, timeout_sec: int = 900,
        assistant_id: str | None = None, context: ExecutionContext | None = None,
    ) -> dict:
        try:
            return execute_profile_request(
                task=task, cwd=cwd, agent=AgentName.antigravity, mode=mode,
                timeout_sec=timeout_sec, assistant_id=assistant_id, context=context,
            )
        except (ConfigError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def run_codex(
        task: str, cwd: str | None = None, mode: RunMode | None = None, timeout_sec: int = 900,
        assistant_id: str | None = None, context: ExecutionContext | None = None,
    ) -> dict:
        try:
            return execute_profile_request(
                task=task, cwd=cwd, agent=AgentName.codex, mode=mode,
                timeout_sec=timeout_sec, assistant_id=assistant_id, context=context,
            )
        except (ConfigError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def run_opencode(
        task: str, cwd: str | None = None, mode: RunMode | None = None, timeout_sec: int = 900,
        assistant_id: str | None = None, context: ExecutionContext | None = None,
    ) -> dict:
        try:
            return execute_profile_request(
                task=task, cwd=cwd, agent=AgentName.opencode, mode=mode,
                timeout_sec=timeout_sec, assistant_id=assistant_id, context=context,
            )
        except (ConfigError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    return mcp, config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        import json
        config = load_config(args.config)
        print(json.dumps(check_agents(config), indent=2, ensure_ascii=False))
        return
    mcp, config = create_server(args.config)
    mcp.run(
        transport="http",
        host=config.server.host,
        port=config.server.port,
        path=config.server.path,
    )


if __name__ == "__main__":
    main()
