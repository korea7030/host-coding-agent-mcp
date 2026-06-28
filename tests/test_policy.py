import pytest

from host_coding_agent.security import SecurityViolation, redact, validate_task


def test_blocks_secret():
    with pytest.raises(SecurityViolation):
        validate_task("call URL with api_key=super-secret")


def test_blocks_destructive_command():
    with pytest.raises(SecurityViolation):
        validate_task("run rm -rf /")


def test_redacts_output():
    text, changed = redact("Authorization: Bearer abcdefghijklmnop")
    assert changed
    assert "abcdefghijklmnop" not in text
