from __future__ import annotations

import argparse
import contextvars
import hashlib
import re
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
from host_coding_agent.automated_delivery import (
    AutomatedDelivery,
    AutomatedDeliveryError,
)
from host_coding_agent.config import validate_profile_cwd
from host_coding_agent.delivery import ManualDelivery, ManualDeliveryError
from host_coding_agent.health import (
    check_execution_health as build_execution_health,
    compact_execution_health,
)
from host_coding_agent.jobs import JobError, JobStore
from host_coding_agent.models import DeliveryMode, IsolationMode, WorktreeStatus
from host_coding_agent.profiles import (
    authenticated_profile,
    merge_context,
    resolve_profile_request,
)
from host_coding_agent.progress import emit_progress, progress_events
from host_coding_agent.proposals import (
    WorktreeProposalError,
    create_managed_worktree_proposal,
)
from host_coding_agent.runner import run_managed_worktree_agent
from host_coding_agent.runtime import RuntimeRegistry
from host_coding_agent.task_classification import non_development_response
from host_coding_agent.security import validate_task
from host_coding_agent.testing import WorktreeTestError, run_managed_worktree_tests
from host_coding_agent.worktrees import WorktreeError, WorktreeManager


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
    job_store = JobStore(
        artifact_path.with_name("jobs.db"),
        max_workers=2,
    )
    runtime_registry = RuntimeRegistry(
        config,
        state_path=artifact_path.with_name("runtimes.json"),
    )
    patch_applier = PatchApplier(
        config=config,
        proposals=proposal_store,
        approvals=approval_store,
    )
    worktree_state_path = config.worktrees.state_path
    if not worktree_state_path.is_absolute():
        worktree_state_path = resolved_config_path.parent / worktree_state_path
    worktree_manager = WorktreeManager(
        root=config.worktrees.root,
        state_path=worktree_state_path,
        branch_prefix=config.worktrees.branch_prefix,
        ttl_sec=config.worktrees.ttl_sec,
    )
    manual_delivery = ManualDelivery(
        manager=worktree_manager,
        applier=patch_applier,
    )
    automated_delivery = AutomatedDelivery(
        manager=worktree_manager,
        proposals=proposal_store,
        config=config,
    )
    mcp = FastMCP(
        "host-coding-agent",
        auth=auth,
        mask_error_details=config.server.mask_error_details,
    )

    def normalize_proposal_sha256(value: str) -> str:
        candidate = value.strip()
        if re.fullmatch(r"[0-9a-fA-F]{64}", candidate):
            return f"sha256:{candidate.lower()}"
        if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", candidate):
            return candidate.lower()
        return candidate

    def proposal_apply_command(proposal_id: str, proposal_sha256: str) -> str:
        return (
            f"/apply_proposal {proposal_id} "
            f"{normalize_proposal_sha256(proposal_sha256)}"
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
                runtime_registry=runtime_registry,
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
            result.requested_cwd = cwd or str(resolved.profile.default_cwd)
            result.path_mapping_applied = (
                result.requested_cwd is not None
                and Path(result.requested_cwd) != result.cwd
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
            result.requested_cwd = cwd
            result.path_mapping_applied = False
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

    def development_profile(assistant_id: str | None = None):
        if not config.auth.enabled:
            raise ConfigError("development jobs require profile authentication")
        profile_name = authenticated_profile(get_access_token(), config)
        if assistant_id is not None and assistant_id != profile_name:
            raise SecurityViolation(
                "assistant_id does not match authenticated profile"
            )
        return profile_name, config.profiles[profile_name]

    def development_job_payload(job) -> dict:
        payload = job.model_dump(mode="json")
        payload["delivery_target"] = worktree_manager.get_delivery_target(
            job.job_id,
            profile=job.profile,
        )
        try:
            payload["selected_agent"] = worktree_manager.get_selected_agent(
                job.job_id,
                profile=job.profile,
            ).value
        except WorktreeError:
            payload["selected_agent"] = None
        try:
            payload["proposal"] = worktree_manager.get_proposal_link(
                job.job_id,
                profile=job.profile,
            )
        except WorktreeError:
            payload["proposal"] = None
        return payload

    def create_managed_job(
        *,
        task: str,
        cwd: str | None,
        delivery_mode: DeliveryMode,
        assistant_id: str | None,
    ):
        profile_name, profile = development_profile(assistant_id)
        if delivery_mode not in profile.allowed_delivery_modes:
            raise SecurityViolation(
                "delivery mode is not allowed for this profile"
            )
        if delivery_mode == DeliveryMode.report:
            raise SecurityViolation("report delivery mode is not implemented")
        validate_task(task)
        requested_cwd = cwd or (
            str(profile.default_cwd)
            if profile.default_cwd is not None
            else None
        )
        if requested_cwd is None:
            raise ConfigError(
                "cwd is required because this profile has no default_cwd"
            )
        repository = validate_profile_cwd(
            requested_cwd,
            profile_name,
            config,
            runtime_registry=runtime_registry,
        )
        repository = worktree_manager.repository_root(repository)
        validate_profile_cwd(
            repository,
            profile_name,
            config,
            runtime_registry=runtime_registry,
        )
        job = worktree_manager.create(
            repository=repository,
            profile=profile_name,
            task=task,
            delivery_mode=delivery_mode,
        )
        return profile_name, profile, job

    def execute_direct_task(
        *,
        task: str,
        cwd: str | None,
        agent: AgentName,
        timeout_sec: int,
        assistant_id: str | None,
        context: ExecutionContext | None,
    ) -> dict:
        profile_name, profile = development_profile(assistant_id)
        if IsolationMode.direct not in profile.allowed_isolation_modes:
            raise SecurityViolation(
                "direct isolation mode is not allowed for this profile"
            )
        if agent != AgentName.auto and agent not in profile.allowed_agents:
            raise SecurityViolation("agent is not allowed for this profile")
        requested_cwd = cwd or (
            str(profile.default_cwd)
            if profile.default_cwd is not None
            else None
        )
        if requested_cwd is None:
            raise ConfigError(
                "cwd is required because this profile has no default_cwd"
            )
        direct_cwd = validate_profile_cwd(
            requested_cwd,
            profile_name,
            config,
            runtime_registry=runtime_registry,
        )
        result = execute_agent(
            task=task,
            cwd=str(direct_cwd),
            agent=agent,
            mode=RunMode.apply_patch,
            timeout_sec=timeout_sec,
            config=config,
            assistant_id=profile_name,
            context=merge_context(profile.context, context),
            allowed_agents=set(profile.allowed_agents),
            allow_apply_patch_override=True,
        )
        return {
            "ok": result.ok,
            "stage": "completed" if result.ok else "direct",
            "isolation_mode": IsolationMode.direct.value,
            "applied_immediately": result.ok,
            "selected_agent": (
                result.selected_agent.value
                if result.selected_agent
                else None
            ),
            "cwd": str(result.cwd),
            "summary": result.summary,
            "error": result.error,
        }

    @mcp.custom_route(
        "/runtime/register",
        methods=["POST"],
        include_in_schema=False,
    )
    async def runtime_register(request: Request) -> JSONResponse:
        authorization = request.headers.get("authorization", "")
        scheme, _, raw_token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not raw_token or auth is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        token = await auth.verify_token(raw_token)
        if token is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
            if payload.get("runtime") != "docker":
                raise ConfigError("unsupported runtime registration")
            registration = runtime_registry.register_docker(
                profile_name=token.claims["profile"],
                container_id=str(payload.get("container_id", "")),
            )
            return JSONResponse({"ok": True, **registration})
        except (ConfigError, KeyError, TypeError, ValueError) as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )

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
            proposal_sha256 = normalize_proposal_sha256(
                str(payload.get("proposal_sha256", ""))
            )
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
                response = {"ok": True, "approval": approval}
                found = worktree_manager.find_by_proposal(
                    proposal_id,
                    profile=profile_name,
                )
                if found is not None:
                    job, _ = found
                    if job.status.value == "proposed":
                        worktree_manager.transition(
                            job.job_id,
                            profile=profile_name,
                            status=WorktreeStatus.abandoned,
                        )
                        cleanup = worktree_manager.cleanup(
                            job.job_id,
                            profile=profile_name,
                        )
                        response["job_id"] = job.job_id
                        response["cleanup"] = cleanup.model_dump(mode="json")
                return JSONResponse(response)
            if action == "approve":
                approval = approval_store.get_for_proposal(
                    proposal_id,
                    profile=profile_name,
                )
                if approval["status"] == "pending":
                    approval = approval_store.decide(
                        proposal_id=proposal_id,
                        profile=profile_name,
                        proposal_sha256=proposal_sha256,
                        approved=True,
                        decided_by=actor,
                        decision_channel="telegram",
                    )
                elif approval["status"] not in {"approved", "applied"}:
                    raise ApprovalError(
                        f"approval cannot be applied from status {approval['status']}"
                    )
                if manual_delivery.applies_to(
                    proposal_id=proposal_id,
                    profile=profile_name,
                ):
                    return JSONResponse(
                        manual_delivery.deliver(
                            proposal_id=proposal_id,
                            profile=profile_name,
                            proposal_sha256=proposal_sha256,
                        )
                    )
                if approval["status"] != "approved":
                    raise ApprovalError("approved request not found")
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
            ManualDeliveryError,
            PatchApplyError,
            WorktreeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )

    @mcp.tool
    def check_host_coding_agents(
        cwd: str | None = None,
        isolation_mode: IsolationMode | None = None,
        include_execution_health: bool = True,
    ) -> dict:
        """Discover configured host CLIs and agents selectable by this profile."""
        try:
            allowed_agents = None
            profile_name = None
            if config.auth.enabled:
                profile_name = request_profile()
                allowed_agents = set(config.profiles[profile_name].allowed_agents)
            result = check_agents(config, allowed_agents=allowed_agents)
            result["profile"] = profile_name
            result["discovery_scope"] = "cli_availability"
            result["execution_health_tool"] = "check_execution_health"
            result["warning"] = (
                "CLI availability does not guarantee profile runtime, cwd mapping, "
                "sandbox, or worktree readiness. Call check_execution_health before "
                "running development tasks."
            )
            if include_execution_health and profile_name is not None:
                health = build_execution_health(
                    config=config,
                    profile_name=profile_name,
                    runtime_registry=runtime_registry,
                    cwd=cwd,
                    isolation_mode=isolation_mode,
                )
                result["execution_ready"] = health["ok"]
                result["execution_health"] = compact_execution_health(health)
            return result
        except (ConfigError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def check_execution_health(
        cwd: str | None = None,
        isolation_mode: IsolationMode | None = None,
        assistant_id: str | None = None,
    ) -> dict:
        """Check profile runtime, cwd mapping, sandbox, and isolation readiness."""
        try:
            profile_name, _ = development_profile(assistant_id)
            return build_execution_health(
                config=config,
                profile_name=profile_name,
                runtime_registry=runtime_registry,
                cwd=cwd,
                isolation_mode=isolation_mode,
            )
        except (ConfigError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

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
    def create_development_job(
        task: str,
        cwd: str | None = None,
        delivery_mode: DeliveryMode = DeliveryMode.manual,
        assistant_id: str | None = None,
    ) -> dict:
        """Create an isolated Git worktree job for the authenticated profile."""
        try:
            _, _, job = create_managed_job(
                task=task,
                cwd=cwd,
                delivery_mode=delivery_mode,
                assistant_id=assistant_id,
            )
            return {"ok": True, "job": development_job_payload(job)}
        except (ConfigError, SecurityViolation, WorktreeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def run_development_task(
        task: str,
        cwd: str | None = None,
        agent: AgentName | None = None,
        delivery_mode: DeliveryMode = DeliveryMode.manual,
        isolation_mode: IsolationMode | None = None,
        timeout_sec: int = 900,
        assistant_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> dict:
        """Run development after discovery; pass an explicit agent when user-selected."""
        rejected = non_development_response(task)
        if rejected is not None:
            return {**rejected, "stage": "classification"}
        stage = "create"
        job = None
        try:
            profile_name, profile = development_profile(assistant_id)
            resolved_isolation = (
                isolation_mode or profile.default_isolation_mode
            )
            if resolved_isolation not in profile.allowed_isolation_modes:
                raise SecurityViolation(
                    "isolation mode is not allowed for this profile"
                )
            selected_agent = agent or profile.default_agent
            if (
                selected_agent != AgentName.auto
                and selected_agent not in profile.allowed_agents
            ):
                raise SecurityViolation("agent is not allowed for this profile")
            if resolved_isolation == IsolationMode.direct:
                if delivery_mode != DeliveryMode.manual:
                    raise SecurityViolation(
                        "delivery_mode applies only to worktree isolation"
                    )
                stage = "direct"
                return execute_direct_task(
                    task=task,
                    cwd=cwd,
                    agent=selected_agent,
                    timeout_sec=timeout_sec,
                    assistant_id=assistant_id,
                    context=context,
                )
            profile_name, profile, job = create_managed_job(
                task=task,
                cwd=cwd,
                delivery_mode=delivery_mode,
                assistant_id=assistant_id,
            )
            emit_progress(
                "create",
                "Managed worktree created",
                {"job_id": job.job_id, "repository": str(job.repository)},
            )
            stage = "agent"
            run_result = run_managed_worktree_agent(
                manager=worktree_manager,
                job_id=job.job_id,
                profile=profile_name,
                task=task,
                agent=selected_agent,
                timeout_sec=timeout_sec,
                config=config,
                assistant_id=profile_name,
                context=merge_context(profile.context, context),
                allowed_agents=set(profile.allowed_agents),
            )
            if not run_result.ok:
                return {
                    "ok": False,
                    "stage": stage,
                    "job_id": job.job_id,
                    "status": WorktreeStatus.failed.value,
                    "error": run_result.error,
                }
            if run_result.selected_agent is None:
                raise WorktreeError("coding agent result did not identify an agent")
            stage = "test"
            emit_progress(
                "test",
                "Running trusted project tests",
                {"job_id": job.job_id},
            )
            test_result = run_managed_worktree_tests(
                manager=worktree_manager,
                job_id=job.job_id,
                profile=profile_name,
                config=config.worktrees,
            )
            if not test_result.ok:
                return {
                    "ok": False,
                    "stage": stage,
                    "job_id": job.job_id,
                    "status": WorktreeStatus.failed.value,
                    "error": test_result.error,
                    "failed_command": (
                        test_result.results[-1].command
                        if test_result.results
                        else None
                    ),
                }
            stage = "proposal"
            emit_progress(
                "proposal",
                "Creating immutable patch proposal",
                {"job_id": job.job_id},
            )
            proposal_result = create_managed_worktree_proposal(
                manager=worktree_manager,
                proposals=proposal_store,
                approvals=approval_store,
                job_id=job.job_id,
                profile=profile_name,
                agent=run_result.selected_agent,
            )
            if not proposal_result.ok:
                return {
                    "ok": False,
                    "stage": stage,
                    "job_id": job.job_id,
                    "status": WorktreeStatus.failed.value,
                    "error": proposal_result.error,
                }
            if delivery_mode == DeliveryMode.manual:
                return {
                    "ok": True,
                    "stage": "awaiting_approval",
                    "job_id": job.job_id,
                    "status": WorktreeStatus.proposed.value,
                    "selected_agent": run_result.selected_agent.value,
                    "proposal_id": proposal_result.proposal_id,
                    "proposal_sha256": proposal_result.proposal_sha256,
                    "apply_command": proposal_apply_command(
                        proposal_result.proposal_id,
                        proposal_result.proposal_sha256,
                    ),
                    "changed_files": proposal_result.changed_files,
                    "requires_approval": True,
                    "isolation_mode": IsolationMode.worktree.value,
                }
            stage = "delivery"
            emit_progress(
                "delivery",
                "Delivering tested changes",
                {"job_id": job.job_id, "delivery_mode": delivery_mode.value},
            )
            delivery_result = automated_delivery.deliver(
                job_id=job.job_id,
                profile=profile_name,
            )
            return {
                **delivery_result,
                "stage": "delivered",
                "status": WorktreeStatus.delivered.value,
                "selected_agent": run_result.selected_agent.value,
                "proposal_id": proposal_result.proposal_id,
                "proposal_sha256": proposal_result.proposal_sha256,
                "changed_files": proposal_result.changed_files,
                "test_commands": len(test_result.results),
                "isolation_mode": IsolationMode.worktree.value,
            }
        except (
            ApprovalError,
            ArtifactError,
            AutomatedDeliveryError,
            ConfigError,
            SecurityViolation,
            WorktreeError,
            WorktreeProposalError,
            WorktreeTestError,
            ValueError,
        ) as exc:
            response = {"ok": False, "stage": stage, "error": str(exc)}
            if job is not None:
                response["job_id"] = job.job_id
                try:
                    current = worktree_manager.get(
                        job.job_id,
                        profile=job.profile,
                    )
                    if current.status not in {
                        WorktreeStatus.delivered,
                        WorktreeStatus.failed,
                        WorktreeStatus.abandoned,
                    }:
                        current = worktree_manager.transition(
                            job.job_id,
                            profile=job.profile,
                            status=WorktreeStatus.failed,
                        )
                    response["status"] = current.status.value
                except WorktreeError:
                    pass
            return response

    @mcp.tool
    def start_development_task(
        task: str,
        cwd: str | None = None,
        agent: AgentName | None = None,
        delivery_mode: DeliveryMode = DeliveryMode.manual,
        isolation_mode: IsolationMode | None = None,
        timeout_sec: int = 900,
        assistant_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> dict:
        """Queue development and immediately return a job_id for polling."""
        rejected = non_development_response(task)
        if rejected is not None:
            return {**rejected, "stage": "classification"}
        try:
            profile_name, profile = development_profile(assistant_id)
            selected_agent = agent or profile.default_agent
            if (
                selected_agent != AgentName.auto
                and selected_agent not in profile.allowed_agents
            ):
                raise SecurityViolation("agent is not allowed for this profile")
            validate_task(task)
            request_context = contextvars.copy_context()
            task_hash = "sha256:" + hashlib.sha256(task.encode()).hexdigest()

            def worker(emit):
                emit(
                    "workflow",
                    "Development workflow started",
                    {"agent": selected_agent.value},
                )
                def execute_with_progress():
                    with progress_events(emit):
                        return run_development_task(
                            task=task,
                            cwd=cwd,
                            agent=selected_agent,
                            delivery_mode=delivery_mode,
                            isolation_mode=isolation_mode,
                            timeout_sec=timeout_sec,
                            assistant_id=assistant_id,
                            context=context,
                        )

                result = request_context.run(execute_with_progress)
                emit(
                    result.get("stage", "completed"),
                    "Development workflow completed",
                    {"ok": bool(result.get("ok"))},
                )
                return result

            job = job_store.submit(
                profile=profile_name,
                kind="development_task",
                metadata={
                    "task_hash": task_hash,
                    "requested_agent": selected_agent.value,
                    "delivery_mode": delivery_mode.value,
                    "isolation_mode": (
                        isolation_mode or profile.default_isolation_mode
                    ).value,
                    "timeout_sec": max(
                        1,
                        min(timeout_sec, config.security.max_timeout_sec),
                    ),
                },
                worker=worker,
            )
            return {
                "ok": True,
                "job_id": job["job_id"],
                "status": job["status"],
                "stage": job["stage"],
                "poll_with": "get_async_job",
                "events_with": "get_async_job_events",
            }
        except (
            ConfigError,
            JobError,
            SecurityViolation,
            ValueError,
        ) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def get_async_job(
        job_id: str,
        assistant_id: str | None = None,
    ) -> dict:
        """Poll asynchronous job status, stage, timestamps, and final result."""
        try:
            profile_name, _ = development_profile(assistant_id)
            return {"ok": True, "job": job_store.get(job_id, profile_name)}
        except (ConfigError, JobError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def get_async_job_events(
        job_id: str,
        after: int = 0,
        limit: int = 100,
        assistant_id: str | None = None,
    ) -> dict:
        """Poll ordered progress events after a sequence cursor."""
        try:
            profile_name, _ = development_profile(assistant_id)
            return {
                "ok": True,
                **job_store.events(
                    job_id,
                    profile_name,
                    after=after,
                    limit=limit,
                ),
            }
        except (ConfigError, JobError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def list_async_jobs(
        limit: int = 20,
        assistant_id: str | None = None,
    ) -> dict:
        """List asynchronous jobs owned by the authenticated profile."""
        try:
            profile_name, _ = development_profile(assistant_id)
            return {"ok": True, "jobs": job_store.list(profile_name, limit=limit)}
        except (ConfigError, JobError, SecurityViolation, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def run_development_job(
        job_id: str,
        task: str,
        agent: AgentName | None = None,
        timeout_sec: int = 900,
        assistant_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> dict:
        """Run a coding agent with write access limited to a managed worktree."""
        try:
            profile_name, profile = development_profile(assistant_id)
            selected_agent = agent or profile.default_agent
            if (
                selected_agent != AgentName.auto
                and selected_agent not in profile.allowed_agents
            ):
                raise SecurityViolation("agent is not allowed for this profile")
            result = run_managed_worktree_agent(
                manager=worktree_manager,
                job_id=job_id,
                profile=profile_name,
                task=task,
                agent=selected_agent,
                timeout_sec=timeout_sec,
                config=config,
                assistant_id=profile_name,
                context=merge_context(profile.context, context),
                allowed_agents=set(profile.allowed_agents),
            )
            return result.model_dump(mode="json")
        except (ConfigError, SecurityViolation, WorktreeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def test_development_job(
        job_id: str,
        assistant_id: str | None = None,
    ) -> dict:
        """Run trusted base-commit tests for an active development job."""
        try:
            profile_name, _ = development_profile(assistant_id)
            result = run_managed_worktree_tests(
                manager=worktree_manager,
                job_id=job_id,
                profile=profile_name,
                config=config.worktrees,
            )
            return result.model_dump(mode="json")
        except (
            ConfigError,
            SecurityViolation,
            WorktreeError,
            WorktreeTestError,
            ValueError,
        ) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def propose_development_job(
        job_id: str,
        assistant_id: str | None = None,
    ) -> dict:
        """Create an immutable proposal from a tested worktree job."""
        try:
            profile_name, _ = development_profile(assistant_id)
            selected_agent = worktree_manager.get_selected_agent(
                job_id,
                profile=profile_name,
            )
            result = create_managed_worktree_proposal(
                manager=worktree_manager,
                proposals=proposal_store,
                approvals=approval_store,
                job_id=job_id,
                profile=profile_name,
                agent=selected_agent,
            )
            return result.model_dump(mode="json")
        except (
            ArtifactError,
            ConfigError,
            SecurityViolation,
            WorktreeError,
            WorktreeProposalError,
            ValueError,
        ) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def deliver_development_job(
        job_id: str,
        assistant_id: str | None = None,
    ) -> dict:
        """Deliver commit/auto/PR jobs; manual jobs await external approval."""
        try:
            profile_name, _ = development_profile(assistant_id)
            job = worktree_manager.get(job_id, profile=profile_name)
            if job.delivery_mode == DeliveryMode.manual:
                if job.status.value == "delivered":
                    return {
                        "ok": True,
                        "job_id": job_id,
                        "delivery_status": job.status.value,
                    }
                link = worktree_manager.get_proposal_link(
                    job_id,
                    profile=profile_name,
                )
                approval = approval_store.get_for_proposal(
                    link["proposal_id"],
                    profile=profile_name,
                )
                return {
                    "ok": False,
                    "awaiting_approval": True,
                    "proposal_id": link["proposal_id"],
                    "proposal_sha256": link["proposal_sha256"],
                    "apply_command": proposal_apply_command(
                        link["proposal_id"],
                        link["proposal_sha256"],
                    ),
                    "approval_status": approval["status"],
                }
            return automated_delivery.deliver(
                job_id=job_id,
                profile=profile_name,
            )
        except (
            ApprovalError,
            AutomatedDeliveryError,
            ConfigError,
            SecurityViolation,
            WorktreeError,
            ValueError,
        ) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def get_development_job(
        job_id: str,
        assistant_id: str | None = None,
    ) -> dict:
        """Return one development job owned by the authenticated profile."""
        try:
            profile_name, _ = development_profile(assistant_id)
            job = worktree_manager.get(job_id, profile=profile_name)
            return {"ok": True, "job": development_job_payload(job)}
        except (ConfigError, SecurityViolation, WorktreeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def list_development_jobs(
        limit: int = 20,
        assistant_id: str | None = None,
    ) -> dict:
        """List development jobs owned by the authenticated profile."""
        try:
            profile_name, _ = development_profile(assistant_id)
            jobs = worktree_manager.list(profile=profile_name, limit=limit)
            return {
                "ok": True,
                "jobs": [development_job_payload(job) for job in jobs],
            }
        except (ConfigError, SecurityViolation, WorktreeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def abandon_development_job(
        job_id: str,
        assistant_id: str | None = None,
    ) -> dict:
        """Abandon a non-delivered job, release its lock, and clean its worktree."""
        try:
            profile_name, _ = development_profile(assistant_id)
            job = worktree_manager.get(job_id, profile=profile_name)
            if job.status.value == "delivered":
                raise WorktreeError("delivered jobs cannot be abandoned")
            if job.status.value not in {"failed", "abandoned"}:
                job = worktree_manager.transition(
                    job_id,
                    profile=profile_name,
                    status=WorktreeStatus.abandoned,
                )
            cleanup = worktree_manager.cleanup(
                job_id,
                profile=profile_name,
            )
            return {
                "ok": cleanup.ok,
                "job": development_job_payload(job),
                "cleanup": cleanup.model_dump(mode="json"),
            }
        except (ConfigError, SecurityViolation, WorktreeError, ValueError) as exc:
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
        """Run a host agent; discover first and pass agent explicitly when selected."""
        rejected = non_development_response(task)
        if rejected is not None:
            return rejected
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
        """Run Antigravity with direct writes; pass read_only for analysis only."""
        rejected = non_development_response(task)
        if rejected is not None:
            return rejected
        try:
            if mode != RunMode.read_only:
                return execute_direct_task(
                    task=task,
                    cwd=cwd,
                    agent=AgentName.antigravity,
                    timeout_sec=timeout_sec,
                    assistant_id=assistant_id,
                    context=context,
                )
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
        """Run Codex with direct writes; pass read_only for analysis only."""
        rejected = non_development_response(task)
        if rejected is not None:
            return rejected
        try:
            if mode != RunMode.read_only:
                return execute_direct_task(
                    task=task,
                    cwd=cwd,
                    agent=AgentName.codex,
                    timeout_sec=timeout_sec,
                    assistant_id=assistant_id,
                    context=context,
                )
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
        """Run OpenCode with direct writes; pass read_only for analysis only."""
        rejected = non_development_response(task)
        if rejected is not None:
            return rejected
        try:
            if mode != RunMode.read_only:
                return execute_direct_task(
                    task=task,
                    cwd=cwd,
                    agent=AgentName.opencode,
                    timeout_sec=timeout_sec,
                    assistant_id=assistant_id,
                    context=context,
                )
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
