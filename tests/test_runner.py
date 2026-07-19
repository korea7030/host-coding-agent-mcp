import json
from pathlib import Path

from host_coding_agent.models import AgentName, AttemptResult, ExecutionContext, RunMode
from host_coding_agent.progress import progress_events
from host_coding_agent.runner import (
    OPENCODE_READONLY_CONFIG,
    OPENCODE_WORKTREE_CONFIG,
    _agent_text,
    _build_command,
    _extract_diff,
    _prompt,
    check_agents,
    run_coding_agent,
    run_managed_worktree_agent,
)


def test_agent_attempt_emits_start_and_finish_progress(config, monkeypatch):
    events = []

    class FakeProcess:
        pid = 123
        returncode = 0

        def communicate(self, prompt, timeout):
            return "done", ""

    monkeypatch.setattr("host_coding_agent.runner.subprocess.Popen", lambda *args, **kwargs: FakeProcess())
    with progress_events(lambda stage, message, details=None: events.append((stage, message, details))):
        from host_coding_agent.runner import _run_attempt

        result = _run_attempt(
            AgentName.codex,
            "inspect",
            RunMode.read_only,
            config.security.allowed_roots[0],
            30,
            config,
        )

    assert result.ok
    assert [message for _, message, _ in events] == [
        "Starting codex coding agent",
        "Finished codex coding agent",
    ]
    assert events[-1][2]["returncode"] == 0
    assert events[-1][2]["timed_out"] is False


def test_agent_discovery_reports_installation_selection_and_profile_policy(
    config, monkeypatch
):
    def fake_probe(name, app_config):
        enabled = app_config.agents[name].enabled
        installed = name != AgentName.opencode
        return name, {
            "name": name.value,
            "configured": True,
            "enabled": enabled,
            "installed": installed,
            "available": installed,
            "selectable": enabled and installed,
            "path": f"/tools/{name.value}" if installed else None,
            "version": "1.2.3" if installed else "",
            "version_ok": installed,
            "probe_error": None,
            "unavailable_reason": None if installed else "configured command was not found",
            "priority": app_config.agents[name].priority,
        }

    monkeypatch.setattr("host_coding_agent.runner._probe_agent", fake_probe)
    result = check_agents(config, allowed_agents={AgentName.codex, AgentName.opencode})

    assert result["selection_required"] is True
    assert result["auto_supported"] is True
    assert result["selectable_agents"] == ["codex"]
    assert result["tools"]["antigravity"]["installed"] is True
    assert result["tools"]["antigravity"]["profile_allowed"] is False
    assert result["tools"]["antigravity"]["selectable"] is False
    assert result["tools"]["opencode"]["installed"] is False
    assert result["tools"]["opencode"]["selectable"] is False


def test_agent_discovery_probes_versions_in_parallel(config, monkeypatch):
    calls = []

    class Completed:
        returncode = 0
        stdout = "agent 9.9.9\n"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr("host_coding_agent.runner.subprocess.run", fake_run)
    result = check_agents(config)

    assert result["selectable_agents"] == ["antigravity", "codex", "opencode"]
    assert len(calls) == 3
    assert all(kwargs["timeout"] == 3 for _, kwargs in calls)
    assert all(item["version"] == "agent 9.9.9" for item in result["agents"])


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


def test_opencode_worktree_policy_allows_development_but_denies_external_paths():
    config = json.loads(OPENCODE_WORKTREE_CONFIG)
    policy = config["agent"]["host-mcp-worktree"]["permission"]
    assert policy["edit"] == "allow"
    assert policy["bash"] == "allow"
    assert policy["task"] == "allow"
    assert policy["external_directory"] == "deny"


def test_write_mode_commands_are_restricted_to_managed_worktree(
    config,
):
    worktree = config.security.allowed_roots[0]

    codex_command, _ = _build_command(
        AgentName.codex,
        "change app",
        RunMode.apply_patch,
        worktree,
        config,
    )
    sandbox_index = codex_command.index("-s")
    assert codex_command[sandbox_index + 1] == "workspace-write"
    assert codex_command[codex_command.index("-C") + 1] == str(worktree)

    for agent in (AgentName.opencode, AgentName.antigravity):
        command, _ = _build_command(
            agent,
            "change app",
            RunMode.apply_patch,
            worktree,
            config,
        )
        assert Path(command[0]).name == "sandbox-exec"
        profile = command[command.index("-p") + 1]
        assert f'(subpath "{worktree}")' in profile
        assert "(deny file-write*)" in profile


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

    audit_lines = config.logging.path.read_text().splitlines()
    started_audit = json.loads(audit_lines[0])
    audit = json.loads(audit_lines[-1])
    assert started_audit["tool"] == "run_coding_agent_started"
    assert started_audit["timeout_sec"] == 30
    assert result.assistant_id == "dev-bot"
    assert result.context == context
    assert result.requested_agent == AgentName.codex
    assert result.selection_mode == "explicit"
    assert result.candidate_agents == [AgentName.codex]
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
    assert result.requested_agent == AgentName.auto
    assert result.selection_mode == "automatic"
    assert result.candidate_agents == [AgentName.codex]
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
