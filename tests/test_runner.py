import json
from pathlib import Path

from host_coding_agent.models import AgentName, AttemptResult, ExecutionContext, RunMode
from host_coding_agent.runner import (
    OPENCODE_READONLY_CONFIG,
    _agent_text,
    _build_command,
    _extract_diff,
    _prompt,
    run_coding_agent,
)


def test_extracts_codex_agent_message():
    attempt = AttemptResult(
        agent=AgentName.codex,
        ok=True,
        stdout='{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
    )
    assert _agent_text(attempt) == "done"


def test_extracts_fenced_diff():
    assert _extract_diff("x\n```diff\n--- a/a\n+++ b/a\n```\n") == "--- a/a\n+++ b/a\n"


def test_opencode_readonly_policy_enables_omo_delegation_but_denies_mutation_and_shell():
    config = json.loads(OPENCODE_READONLY_CONFIG)
    assert config["plugin"] == ["oh-my-openagent@latest"]
    policy = config["agent"]["host-mcp-readonly"]["permission"]
    assert policy["edit"] == "deny"
    assert policy["bash"] == "deny"
    assert policy["task"] == "allow"
    assert policy["external_directory"] == "deny"


def test_prompt_includes_assistant_and_structured_execution_context():
    prompt = _prompt(
        "implement the endpoint",
        RunMode.propose_patch,
        Path("/tmp/workspace"),
        assistant_id="frontend-bot",
        context=ExecutionContext(
            language="한국어",
            runtime="node",
            runtime_version="24",
            framework="Next.js",
            package_manager="pnpm",
            test_command="pnpm test",
        ),
    )

    assert "Invoking assistant: frontend-bot" in prompt
    assert '"language": "한국어"' in prompt
    assert '"runtime": "node"' in prompt
    assert '"test_command": "pnpm test"' in prompt
    assert "Resolved host working directory: /tmp/workspace" in prompt


def test_run_result_echoes_context_and_audit_only_stores_context_hash(
    config, monkeypatch
):
    context = ExecutionContext(language="한국어", runtime="python", framework="FastAPI")

    def successful_attempt(agent, task, mode, cwd, timeout_sec, app_config, assistant_id, supplied):
        assert assistant_id == "dev-bot"
        assert supplied == context
        return AttemptResult(agent=agent, ok=True, returncode=0, stdout="done")

    monkeypatch.setattr("host_coding_agent.runner._run_attempt", successful_attempt)
    result = run_coding_agent(
        task="inspect",
        cwd=str(config.security.allowed_roots[0]),
        agent=AgentName.codex,
        mode=RunMode.read_only,
        timeout_sec=30,
        config=config,
        assistant_id="dev-bot",
        context=context,
    )

    audit = json.loads(config.logging.path.read_text().strip())
    assert result.assistant_id == "dev-bot"
    assert result.context == context
    assert audit["assistant_id"] == "dev-bot"
    assert audit["context_hash"].startswith("sha256:")
    assert "context" not in audit


def test_auto_routing_is_limited_to_profile_allowed_agents(config, monkeypatch):
    attempted = []

    def successful_attempt(agent, *args):
        attempted.append(agent)
        return AttemptResult(agent=agent, ok=True, returncode=0, stdout="done")

    monkeypatch.setattr("host_coding_agent.runner._run_attempt", successful_attempt)
    result = run_coding_agent(
        task="refactor this project",
        cwd=str(config.security.allowed_roots[0]),
        agent=AgentName.auto,
        mode=RunMode.read_only,
        timeout_sec=30,
        config=config,
        allowed_agents={AgentName.codex},
    )

    assert result.ok
    assert attempted == [AgentName.codex]


def test_antigravity_print_prompt_immediately_follows_flag(config):
    cwd = config.security.allowed_roots[0]
    command, stdin_prompt = _build_command(
        AgentName.antigravity,
        "inspect the workspace",
        RunMode.read_only,
        cwd,
        config,
    )

    print_index = command.index("--print")
    assert command[print_index + 1].endswith("Task:\ninspect the workspace")
    assert command[print_index + 2 : print_index + 4] == ["--print-timeout", "30m"]
    assert stdin_prompt is None
