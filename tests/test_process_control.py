from __future__ import annotations

from host_coding_agent.process_control import ProcessRegistry


def test_process_registry_terminates_registered_job_process_groups(monkeypatch):
    registry = ProcessRegistry()
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(
        "host_coding_agent.process_control.os.killpg",
        lambda pid, sig: calls.append((pid, sig)),
    )
    monkeypatch.setattr("host_coding_agent.process_control.time.sleep", lambda _: None)

    with registry.job_context("job-1"):
        with registry.register_current_process(123):
            result = registry.terminate_job("job-1")

    assert result.process_count == 1
    assert result.pids == [123]
    assert result.process_killed is True
    assert result.process_kill_guaranteed is True
    assert [pid for pid, _ in calls] == [123, 123]


def test_process_registry_ignores_processes_without_job_context(monkeypatch):
    registry = ProcessRegistry()
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "host_coding_agent.process_control.os.killpg",
        lambda pid, sig: calls.append((pid, sig)),
    )

    with registry.register_current_process(123):
        result = registry.terminate_job("job-1")

    assert result.process_count == 0
    assert result.process_killed is False
    assert calls == []
