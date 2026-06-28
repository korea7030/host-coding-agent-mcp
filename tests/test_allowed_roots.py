from pathlib import Path

import pytest

from host_coding_agent.config import ConfigError, validate_cwd


def test_allows_child_directory(config):
    child = config.security.allowed_roots[0] / "repo"
    child.mkdir()
    assert validate_cwd(child, config) == child.resolve()


def test_rejects_prefix_collision(config, tmp_path):
    evil = tmp_path / "projects-evil"
    evil.mkdir()
    with pytest.raises(ConfigError):
        validate_cwd(evil, config)


def test_rejects_symlink_escape(config, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = config.security.allowed_roots[0] / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigError):
        validate_cwd(link, config)


def test_rejects_relative_path(config):
    with pytest.raises(ConfigError):
        validate_cwd(Path("repo"), config)
