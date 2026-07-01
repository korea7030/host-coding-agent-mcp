from __future__ import annotations

import argparse
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import JSONResponse

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
from host_coding_agent.approvals import ApprovalError, ApprovalStore
from host_coding_agent.applier import PatchApplier, PatchApplyError
from host_coding_agent.artifacts import ArtifactError, ProposalStore
from host_coding_agent.profiles import authenticated_profile, resolve_profile_request


def create_server(config_path: str | Path) -> tuple[FastMCP, object]:
    resolved_config_path = Path(config_path).expanduser().resolve()
    config = load_config(resolved_config_path)
    auth = build_auth_provider(config)
    artifact_path = config.artifacts.path
    if not artifact_path.is_absolute():
        artifact_path = resolved_config_path.parent / artifact_path
    proposal_store = ProposalStore(
        artifact_path,
        ttl_sec=config.artifacts.proposal_ttl_sec,
        max_diff_chars=config.artifacts.max_diff_chars,
    )
    approval_store = ApprovalStore(artifact_path)
    patch_applier = PatchApplier(
        config=config,
        proposals=proposal_store,
        approvals=approval_store,
    )
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
            profile_name = resolved.profile_name
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
            profile_name = assistant_id or "anonymous"
        if (
            result.ok
            and result.mode == RunMode.propose_patch
            and result.selected_agent is not None
            and result.proposed_diff
        ):
            try:
                proposal = proposal_store.create(
                    profile=profile_name,
                    cwd=result.cwd,
                    agent=result.selected_agent,
                    task=task,
                    diff_text=result.proposed_diff,
                )
                result.proposal_id = proposal["proposal_id"]
                result.proposal_sha256 = proposal["diff_sha256"]
                approval_store.create_pending(proposal)
            except (ApprovalError, ArtifactError) as exc:
                result.artifact_error = str(exc)
        return result.model_dump(mode="json")

    def request_profile() -> str:
        if not config.auth.enabled:
            return "anonymous"
        return authenticated_profile(get_access_token(), config)

    @mcp.custom_route(
        "/approval/telegram",
        methods=["POST"],
        include_in_schema=False,
    )
    async def telegram_approval(request: Request) -> JSONResponse:
        try:
            authorization = request.headers.get("authorization", "")
            scheme, _, raw_token = authorization.partition(" ")
            if scheme.casefold() != "bearer" or not raw_token or auth is None:
                return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
            token = await auth.verify_token(raw_token)
            if token is None:
                return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
            profile_name = token.claims["profile"]
            profile = config.profiles[profile_name]
            payload = await request.json()
            action = str(payload.get("action", ""))
            proposal_id = str(payload.get("proposal_id", ""))
            proposal_sha256 = str(payload.get("proposal_sha256", ""))
            actor = f"telegram:{payload.get('telegram_user_id', '')}"
            if actor not in profile.approval_identities:
                return JSONResponse(
                    {"ok": False, "error": "approver is not allowed"},
                    status_code=403,
                )
            if action == "show":
                return JSONResponse(
                    {
                        "ok": True,
                        "proposal": proposal_store.get(
                            proposal_id,
                            profile=profile_name,
                        ),
                        "approval": approval_store.get_for_proposal(
                            proposal_id,
                            profile=profile_name,
                        ),
                    }
                )
            if action == "reject":
                approval = approval_store.decide(
                    proposal_id=proposal_id,
                    profile=profile_name,
                    proposal_sha256=proposal_sha256,
                    approved=False,
                    decided_by=actor,
                    decision_channel="telegram",
                )
                return JSONResponse({"ok": True, "approval": approval})
            if action == "approve":
                approval_store.decide(
                    proposal_id=proposal_id,
                    profile=profile_name,
                    proposal_sha256=proposal_sha256,
                    approved=True,
                    decided_by=actor,
                    decision_channel="telegram",
                )
                return JSONResponse(
                    patch_applier.apply(
                        proposal_id=proposal_id,
                        profile=profile_name,
                        proposal_sha256=proposal_sha256,
                    )
                )
            return JSONResponse(
                {"ok": False, "error": "unknown action"},
                status_code=400,
            )
        except (
            ApprovalError,
            ArtifactError,
            PatchApplyError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )

    @mcp.tool
    def check_host_coding_agents() -> dict:
        """Check whether configured host coding-agent CLIs are available."""
        return check_agents(config)

    @mcp.tool
    def get_patch_proposal(proposal_id: str) -> dict:
        """Return one immutable patch proposal owned by the authenticated profile."""
        try:
            return {
                "ok": True,
                "proposal": proposal_store.get(
                    proposal_id,
                    profile=request_profile(),
                ),
            }
        except (ArtifactError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def list_patch_proposals(limit: int = 20) -> dict:
        """List immutable patch proposal metadata for the authenticated profile."""
        try:
            return {
                "ok": True,
                "proposals": proposal_store.list(
                    profile=request_profile(),
                    limit=limit,
                ),
            }
        except (ArtifactError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

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
