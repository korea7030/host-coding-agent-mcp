from host_coding_agent.models import AgentName
from host_coding_agent.routing import route_agents


def test_default_routes_antigravity_first(config):
    assert route_agents("inspect config", AgentName.auto, config)[0] == AgentName.antigravity


def test_refactor_routes_opencode_first(config):
    assert route_agents("multi-file refactor", AgentName.auto, config)[0] == AgentName.opencode


def test_bug_routes_codex_first(config):
    assert route_agents("fix this bug", AgentName.auto, config)[0] == AgentName.codex


def test_explicit_agent_has_no_fallback(config):
    assert route_agents("anything", AgentName.codex, config) == [AgentName.codex]
