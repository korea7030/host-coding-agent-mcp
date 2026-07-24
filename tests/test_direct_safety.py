from __future__ import annotations

from host_coding_agent.direct_safety import changed_files, snapshot_workspace


def test_snapshot_workspace_detects_modified_and_created_files(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    before = snapshot_workspace(tmp_path)

    (tmp_path / "app.py").write_text("modified\n")
    (tmp_path / "new.py").write_text("new\n")
    after = snapshot_workspace(tmp_path)

    assert changed_files(before, after) == ["app.py", "new.py"]


def test_snapshot_workspace_ignores_heavy_runtime_directories(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    (tmp_path / "node_modules").mkdir()
    before = snapshot_workspace(tmp_path)

    (tmp_path / "node_modules" / "dep.js").write_text("ignored\n")
    after = snapshot_workspace(tmp_path)

    assert changed_files(before, after) == []
