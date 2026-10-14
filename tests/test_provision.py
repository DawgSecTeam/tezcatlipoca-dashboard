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


def test_seed_workspace_carries_the_pfsense_seed(tmp_path):
    """The firewall bootstrap needs `pfsense/pfsense-config-orig.xml` from the definitions repo.

    The seeder used to skip `pfsense/` and `vyos/` as per-run state, so phase 5 died with
    "pfsense-config-orig.xml is missing" after phases 1-4 had built the whole range
    (2026-10-14, run-8482163b).
    """
    from dashboard.workspaces import seed_workspace
    src = tmp_path / "defs"
    (src / "pfsense").mkdir(parents=True)
    (src / "pfsense" / "pfsense-config-orig.xml").write_text("<config/>")
    (src / "vyos").mkdir()
    (src / "vyos" / "config-team120.cmds").write_text("set system host-name fw\n")
    (src / "Compfile").write_text("name scrim-one\n")
    (src / ".automated-tests").mkdir()
    (src / ".automated-tests" / "old.json").write_text("{}")
    (src / ".deploy_state.json").write_text('{"run_id": "run-old"}')
    (src / "terraform").mkdir()
    (src / "terraform" / "terraform.tfstate").write_text("{}")
    ws = seed_workspace(src, tmp_path / "ws")
    assert (ws / "pfsense" / "pfsense-config-orig.xml").exists()
    assert (ws / "vyos" / "config-team120.cmds").exists()
    assert (ws / "Compfile").exists()
    # per-run state still stays behind
    assert not (ws / ".automated-tests").exists()
    assert not (ws / ".deploy_state.json").exists()
    assert not (ws / "terraform").exists()


def test_destroy_runs_against_the_workspace_holding_the_state(tmp_path, monkeypatch):
    """Ownership comes from the workspace's run id, so a destroy must not be handed a fresh one."""
    import json as _json
    from dashboard.runner import RunManager
    from dashboard.store import Store
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    runs = tmp_path / "data" / "runs"
    run_dir = runs / "run-failed"
    ws = run_dir / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name scrim-one\n")
    (ws / ".deploy_state.json").write_text(_json.dumps({"run_id": "run-engineid"}))
    store = Store(tmp_path / "db.sqlite3")
    store.create_run("run-failed", "scrim-one", "scrim", "main", {}, run_dir,
                     run_dir / "run.log")
    store.create_run("run-other", "other-comp", "scrim", "main", {}, run_dir,
                     run_dir / "run.log")
    mgr = RunManager(store, None)
    mgr.runs_dir = runs
    # destroy, verify and redeploy all act on the range's own workspace
    assert mgr._range_workspace("scrim-one", {}, "destroy") == ws
    # an explicit comp_dir wins
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert mgr._range_workspace("scrim-one", {"comp_dir": str(other)}, "destroy") == other
    # and nothing state-bearing means nothing to act on
    assert mgr._range_workspace("no-such-comp", {}, "destroy") is None
