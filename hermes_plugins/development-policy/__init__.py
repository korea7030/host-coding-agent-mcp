from __future__ import annotations

from .policy import (
    handle_approve,
    handle_proposal,
    handle_reject,
    on_pre_gateway_dispatch,
    on_pre_llm_call,
    on_pre_tool_call,
)


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
    ctx.register_command(
        "proposal",
        handler=handle_proposal,
        description="Review an immutable host patch proposal.",
        args_hint="<proposal_id>",
    )
    ctx.register_command(
        "apply-proposal",
        handler=handle_approve,
        description="Approve and apply an immutable host patch proposal.",
        args_hint="<proposal_id> <proposal_sha256>",
    )
    ctx.register_command(
        "reject",
        handler=handle_reject,
        description="Reject an immutable host patch proposal.",
        args_hint="<proposal_id> <proposal_sha256>",
    )
