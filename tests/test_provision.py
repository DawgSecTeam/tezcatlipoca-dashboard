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
                        lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=""))

    mgr = RunManager(store=None, bus=None)
    mgr._provision_worktree(worktree, "main", tmp_path / "run")

    slot = worktree / "vendor" / "nakon"
    assert slot.is_symlink() and slot.resolve() == nakon.resolve()


def test_engine_ref_prefers_the_fetched_remote_branch(tmp_path, monkeypatch):
    """`engine_ref: main` must mean the fresh origin/main, not the clone's stale local branch.

    Measured 2026-10-14: the service's engine clone sat 9 commits behind origin/main and every
    run executed the stale engine while reporting `engine_ref: main` — the fetch in
    _provision_worktree updated origin/main, but `worktree add main` resolves the LOCAL branch.
    """
    import subprocess as sp
    clone = tmp_path / "engine"
    clone.mkdir()
    sp.run(["git", "init", "-q", str(clone)], check=True)
    sp.run(["git", "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "local"],
           check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
                            "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    sp.run(["git", "-C", str(clone), "update-ref", "refs/remotes/origin/main", "HEAD"],
           check=True)
    mgr = RunManager(store=None, bus=None)
    mgr.engine_repo = clone
    assert mgr._resolve_ref("main") == "origin/main"
    # a SHA or tag passes through, so pinned runs are unaffected
    sha = sp.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                 capture_output=True, text=True).stdout.strip()
    assert mgr._resolve_ref(sha) == sha
    assert mgr._resolve_ref("no-such-ref") == "no-such-ref"
