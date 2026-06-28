from __future__ import annotations

from .models import AgentName, AppConfig


def route_agents(task: str, requested: AgentName, config: AppConfig) -> list[AgentName]:
    if requested != AgentName.auto:
        candidates = [requested]
    else:
        lowered = task.casefold()
        if any(keyword.casefold() in lowered for keyword in config.routing.opencode_keywords):
            preferred = AgentName.opencode
        elif any(keyword.casefold() in lowered for keyword in config.routing.codex_keywords):
            preferred = AgentName.codex
        else:
            preferred = config.routing.default_order[0]
        candidates = [preferred] + [agent for agent in config.routing.default_order if agent != preferred]
    return [
        agent for agent in candidates
        if agent in config.agents and config.agents[agent].enabled
    ]
