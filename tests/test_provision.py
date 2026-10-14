"""Worktree provisioning: the nakon symlink must replace the worktree's empty submodule slot."""

import subprocess
from types import SimpleNamespace

from dashboard import config
from dashboard.runner import RunManager


def test_nakon_symlink_replaces_submodule_slot(tmp_path, monkeypatch):
    worktree = tmp_path / "engine"
    (worktree / "vendor" / "nakon").mkdir(parents=True)  # git-worktree's empty submodule slot
    nakon = tmp_path / "nakon-src"
    nakon.mkdir()
    monkeypatch.setattr(config, "nakon_path", lambda: nakon)
    monkeypatch.setattr(config, "engine_env_file", lambda: None)
    monkeypatch.setattr(config, "engine_proxmox_file", lambda: None)
    monkeypatch.setattr(config, "badauto_path", lambda: None)
    monkeypatch.setattr("dashboard.runner.subprocess.run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))

    mgr = RunManager(store=None, bus=None)
    mgr._provision_worktree(worktree, "main", tmp_path / "run")

    slot = worktree / "vendor" / "nakon"
    assert slot.is_symlink() and slot.resolve() == nakon.resolve()
