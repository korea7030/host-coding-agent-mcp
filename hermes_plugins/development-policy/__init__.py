from __future__ import annotations

from .policy import on_pre_llm_call, on_pre_tool_call


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
