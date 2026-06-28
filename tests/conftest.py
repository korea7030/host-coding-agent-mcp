from pathlib import Path

import pytest

from host_coding_agent.models import (
    AgentConfig,
    AgentName,
    AppConfig,
    LoggingConfig,
    RoutingConfig,
    SecurityConfig,
    ServerConfig,
)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    root = tmp_path / "projects"
    root.mkdir()
    return AppConfig(
        server=ServerConfig(),
        security=SecurityConfig(allowed_roots=[root]),
        agents={
            AgentName.antigravity: AgentConfig(command="/bin/echo", priority=1),
            AgentName.codex: AgentConfig(command="/bin/echo", default_args=["exec"], priority=2),
            AgentName.opencode: AgentConfig(command="/bin/echo", default_args=["run"], priority=3),
        },
        routing=RoutingConfig(
            default_order=[AgentName.antigravity, AgentName.codex, AgentName.opencode],
            codex_keywords=["bug", "diff"],
            opencode_keywords=["refactor", "리팩토링"],
        ),
        logging=LoggingConfig(path=tmp_path / "calls.jsonl"),
    )
