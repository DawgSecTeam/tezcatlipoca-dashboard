"""Worktree provisioning: the nakon symlink must replace the worktree's empty submodule slot."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard import config
from dashboard.runner import RunManager, RunRefused


def _no_config(monkeypatch):
    monkeypatch.setattr(config, "nakon_path", lambda: None)
    monkeypatch.setattr(config, "engine_env_file", lambda: None)
    monkeypatch.setattr(config, "engine_config_file", lambda: None)
    monkeypatch.setattr(config, "engine_proxmox_file", lambda: None)
    monkeypatch.setattr(config, "badauto_path", lambda: None)


def test_a_failed_fetch_refuses_before_cutting_a_worktree(tmp_path, monkeypatch):
    """A discarded fetch return code is how a round executed `2358606` while its records said
    `main`: `_resolve_ref` then resolved origin/main from a cached remote-tracking ref. A run
    that cannot guarantee which code it executes must not start."""
    worktree = tmp_path / "engine"
    _no_config(monkeypatch)

    def run(cmd, *a, **k):
        if "fetch" in cmd:
            return SimpleNamespace(returncode=128, stdout="",
                                   stderr="fatal: could not read from remote repository")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("dashboard.runner.subprocess.run", run)
    with pytest.raises(RunRefused, match=r"cannot fetch engine ref 'main' from origin: fatal"):
        RunManager(store=None, bus=None)._provision_worktree(worktree, "main", tmp_path / "run")
    assert not worktree.exists()


_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin"}


def _origin_clone(tmp_path):
    """A clone whose `origin` really moves, so `fetch origin` is meaningful."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True)
    env = {**_GIT_ENV, "HOME": str(tmp_path)}
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "--allow-empty", "-m", "one"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "HEAD:main"], check=True,
                   env=env)
    clone = tmp_path / "engine"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    return clone


def test_the_cut_worktree_head_is_the_resolved_commit(tmp_path, monkeypatch):
    """`engine_head` must be the commit the worktree actually carries, not a re-resolution of
    the branch that could move between the fetch and the cut."""
    clone = _origin_clone(tmp_path)
    head = subprocess.run(["git", "-C", str(clone), "rev-parse", "origin/main"],
                          capture_output=True, text=True).stdout.strip()
    _no_config(monkeypatch)
    mgr = RunManager(store=None, bus=None)
    mgr.engine_repo = clone
    worktree = tmp_path / "run" / "engine"
    used = mgr._provision_worktree(worktree, "main", tmp_path / "run")
    assert used["commit"] == head
    assert subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip() == head


class _Bus:
    def publish(self, run_id, event):
        pass


def test_a_run_that_cannot_fetch_the_engine_is_failed_with_the_refusal(tmp_path, monkeypatch):
    """The fetch runs in the worker, so the refusal must land as the run's outcome — never as
    a driver started against an unknown revision."""
    from dashboard.store import Store

    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    run_dir = tmp_path / "runs" / "run-x"
    ws = run_dir / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name mini\n")
    store = Store(tmp_path / "db.sqlite3")
    record = store.create_run("run-x", "mini", "verify", "main", {"comp_dir": str(ws)},
                              run_dir, run_dir / "run.log")
    mgr = RunManager(store, _Bus())
    mgr.engine_repo = tmp_path / "engine"
    refusal = "cannot fetch engine ref 'main' from origin: fatal: could not read from remote"
    monkeypatch.setattr(RunManager, "_provision_worktree",
                        lambda self, *a: (_ for _ in ()).throw(RunRefused(refusal)))

    mgr._run(record, tmp_path / "defs", "main")

    row = store.get_run("run-x")
    assert row["state"] == "failed" and row["error"] == f"RunRefused: {refusal}"
    assert not (run_dir / "engine").exists()


def test_nakon_symlink_replaces_submodule_slot(tmp_path, monkeypatch):
    worktree = tmp_path / "engine"
    (worktree / "vendor" / "nakon").mkdir(parents=True)  # git-worktree's empty submodule slot
    nakon = tmp_path / "nakon-src"
    nakon.mkdir()
    monkeypatch.setattr(config, "nakon_path", lambda: nakon)
    monkeypatch.setattr(config, "engine_env_file", lambda: None)
    monkeypatch.setattr(config, "engine_config_file", lambda: None)
    monkeypatch.setattr(config, "engine_proxmox_file", lambda: None)
    monkeypatch.setattr(config, "badauto_path", lambda: None)
    monkeypatch.setattr("dashboard.runner.subprocess.run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=""))

    mgr = RunManager(store=None, bus=None)
    mgr._provision_worktree(worktree, "main", tmp_path / "run")

    slot = worktree / "vendor" / "nakon"
    assert slot.is_symlink() and slot.resolve() == nakon.resolve()


def test_provision_worktree_carries_the_config_surfaces(tmp_path, monkeypatch):
    """A run reads its config from the worktree: `.env` (canonical TEZ_* names) and, when the
    operator has one, the native `tezcatlipoca.toml` profile. Both are gitignored, so a fresh
    checkout has neither — the run would start with no endpoint, node or API token."""
    worktree = tmp_path / "engine"
    worktree.mkdir()
    env_file = tmp_path / "tez-engine.env"
    env_file.write_text("TEZ_PROXMOX_NODE=pve\n")
    profile = tmp_path / "tezcatlipoca.toml"
    profile.write_text("[proxmox]\nnode = 'pve'\n")
    monkeypatch.setattr(config, "nakon_path", lambda: None)
    monkeypatch.setattr(config, "engine_env_file", lambda: env_file)
    monkeypatch.setattr(config, "engine_config_file", lambda: profile)
    monkeypatch.setattr(config, "engine_proxmox_file", lambda: None)
    monkeypatch.setattr(config, "badauto_path", lambda: None)
    monkeypatch.setattr("dashboard.runner.subprocess.run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=""))

    RunManager(store=None, bus=None)._provision_worktree(worktree, "estate-v0.3.0",
                                                         tmp_path / "run")

    assert (worktree / ".env").read_text() == "TEZ_PROXMOX_NODE=pve\n"
    assert (worktree / "tezcatlipoca.toml").read_text() == "[proxmox]\nnode = 'pve'\n"


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
    (src / "estate-state.json").write_text('{"identity": {"run_id": "run-old"}}')
    ws = seed_workspace(src, tmp_path / "ws")
    assert (ws / "pfsense" / "pfsense-config-orig.xml").exists()
    assert (ws / "vyos" / "config-team120.cmds").exists()
    assert (ws / "Compfile").exists()
    # per-run state still stays behind — the estate document above all (it carries every
    # password and the run id a fresh deploy must NOT adopt)
    assert not (ws / ".automated-tests").exists()
    assert not (ws / "estate-state.json").exists()


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
    (ws / "estate-state.json").write_text(
        _json.dumps({"identity": {"comp_name": "scrim-one", "run_id": "run-engineid"}}))
    store = Store(tmp_path / "db.sqlite3")
    store.create_run("run-failed", "scrim-one", "scrim", "main", {}, run_dir,
                     run_dir / "run.log")
    store.create_run("run-other", "other-comp", "scrim", "main", {}, run_dir,
                     run_dir / "run.log")
    mgr = RunManager(store, None)
    mgr.runs_dir = runs
    # destroy, verify and redeploy all act on the range's own workspace
    assert mgr._range_workspace("scrim-one", {}, "destroy") == ws
    # An explicit, recorded same-comp workspace wins even before it has deploy state.
    other = tmp_path / "elsewhere" / "comp"
    other.mkdir(parents=True)
    store.create_run("run-explicit", "scrim-one", "deploy", "main", {}, other.parent,
                     other.parent / "run.log")
    assert mgr._range_workspace("scrim-one", {"comp_dir": str(other)}, "destroy") == other
    # and nothing state-bearing means nothing to act on
    assert mgr._range_workspace("no-such-comp", {}, "destroy") is None


def test_a_v030_document_is_the_range_workspace(tmp_path, monkeypatch):
    """The document's identity.run_id is the ownership proof the submit resolves against."""
    import json as _json
    from dashboard.runner import RunManager
    from dashboard.store import Store
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    runs = tmp_path / "data" / "runs"
    run_dir = runs / "run-doc"
    ws = run_dir / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name scrim-one\n")
    (ws / "estate-state.json").write_text(_json.dumps(
        {"identity": {"comp_name": "scrim-one", "run_id": "run-engineid"}, "resources": []}))
    store = Store(tmp_path / "db.sqlite3")
    store.create_run("run-doc", "scrim-one", "deploy", "estate-v0.3.0", {}, run_dir,
                     run_dir / "run.log")
    mgr = RunManager(store, None)
    mgr.runs_dir = runs
    assert mgr._range_workspace("scrim-one", {}, "destroy") == ws
    # a document without a run id is not a range (a bare seeded worktree)
    (ws / "estate-state.json").write_text(_json.dumps({"identity": {}, "resources": []}))
    assert mgr._range_workspace("scrim-one", {}, "destroy") is None


def test_submit_defaults_to_the_services_pinned_engine_ref(tmp_path, monkeypatch):
    """v0.3.0 is the estate release, so a submit that names no ref must pin it — not the
    engine clone's local `main`, which nothing in this service ever updates."""
    from dashboard.runner import RunManager
    from dashboard.store import Store

    class _Bus:
        def publish(self, run_id, event):
            pass

    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("TEZ_ENGINE_REF", raising=False)
    comp = tmp_path / "data" / "competitions" / "mini"
    comp.mkdir(parents=True)
    (comp / "Compfile").write_text("name mini\n")
    mgr = RunManager(Store(tmp_path / "db.sqlite3"), _Bus())
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    monkeypatch.setattr(RunManager, "refresh_comps", lambda self: None)
    monkeypatch.setattr(RunManager, "_run", lambda self, *a, **k: None)

    assert config.default_engine_ref() == "main"
    assert mgr.submit("mini", "deploy", None, {"teams": 1})["engine_ref"] == "main"
    assert mgr.submit("mini", "deploy", "", {"teams": 1})["engine_ref"] == "main"
    # an explicit ref always wins, and TEZ_ENGINE_REF moves the service's pin (a SHA is the
    # right pin for an event; the eventual merge commit lands here)
    monkeypatch.setenv("TEZ_ENGINE_REF", "0" * 40)
    assert config.default_engine_ref() == "0" * 40
    assert mgr.submit("mini", "deploy", None, {"teams": 1})["engine_ref"] == "0" * 40
    assert mgr.submit("mini", "deploy", "v0.2.0", {"teams": 1})["engine_ref"] == "v0.2.0"


def test_an_explicit_comp_dir_wins_for_any_action(tmp_path):
    """A scrim of a range that is already deployed acts on the workspace holding it
    (`--skip-deploy`), not a fresh seed: the range actions resolve theirs into
    `_destroy_comp_dir`, and a submit may name `comp_dir` directly (2026-10-15, the v0.3.0
    full-range scrim)."""
    from dashboard.runner import RunManager
    defs, run_dir, range_dir = tmp_path / "defs", tmp_path / "run", tmp_path / "range"
    defs.mkdir()
    rec = {"params": {"comp_dir": str(range_dir)}, "run_dir": str(run_dir)}
    assert RunManager._workspace(rec, defs) == range_dir
    resolved = {"params": {"_destroy_comp_dir": str(range_dir)}, "run_dir": str(run_dir)}
    assert RunManager._workspace(resolved, defs) == range_dir
    assert RunManager._workspace({"params": {}, "run_dir": str(run_dir)}, defs) == run_dir / "comp"
