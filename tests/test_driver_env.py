"""The driver child's environment: `opencode` must be findable by the harness's blue cycles.

Regression for the 2026-10-14 e2e preflight: a dashboard run spawns the drivers as children of
the systemd unit, whose PATH is `/usr/local/bin:/usr/bin` — while opencode installs itself into
`~/.opencode/bin`. Without a prepend every blue cycle execs `opencode`, fails rc=127, and the run
degrades to watchdog-only defense while still reporting a "successful" scrim.
"""

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from dashboard import config
from dashboard.runner import RunManager, RunRefused, missing_driver_deps
from dashboard.store import Store


def test_driver_path_prepends_the_configured_bin(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("TEZ_OPENCODE_BIN", "/opt/oc/bin")
    monkeypatch.setenv("HOME", str(tmp_path))          # no ~/.local/bin in the picture
    assert config.driver_path() == "/opt/oc/bin:/usr/local/bin:/usr/bin"


def test_driver_path_adds_the_users_local_bin(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("TEZ_OPENCODE_BIN", "/opt/oc/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".local" / "bin").mkdir(parents=True)   # terraform/sshpass live here
    assert config.driver_path() == "/opt/oc/bin:" + str(tmp_path / ".local" / "bin") + ":/usr/local/bin:/usr/bin"


def test_driver_path_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/opt/oc/bin:/usr/bin")
    monkeypatch.setenv("TEZ_OPENCODE_BIN", "/opt/oc/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.driver_path() == "/opt/oc/bin:/usr/bin"


def test_empty_override_leaves_path_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("TEZ_OPENCODE_BIN", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.driver_path() == "/usr/local/bin:/usr/bin"


def test_default_comes_from_the_users_opencode_install(monkeypatch, tmp_path):
    monkeypatch.delenv("TEZ_OPENCODE_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".opencode" / "bin").mkdir(parents=True)
    assert config.opencode_bin() == str(tmp_path / ".opencode" / "bin")


def test_the_spawned_driver_gets_the_prepended_path(monkeypatch, tmp_path):
    """End of the contract: what the child actually receives."""
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("TEZ_OPENCODE_BIN", "/opt/oc/bin")
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    store = Store(tmp_path / "db.sqlite3")
    manager = RunManager(store, None)
    manager.data = tmp_path
    manager.runs_dir = tmp_path / "runs"
    manager.runs_dir.mkdir(parents=True, exist_ok=True)
    comp = tmp_path / "data" / "competitions" / "mini"
    comp.mkdir(parents=True)
    (comp / "Compfile").write_text("name mini\n")
    run_dir = tmp_path / "runs" / "run-xyz"
    run_dir.mkdir(parents=True)
    record = store.create_run("run-xyz", "mini", "scrim", "main", {},
                              run_dir, run_dir / "run.log")
    manager._emit = lambda *a, **k: None
    captured = {}

    class FakeProc:
        pid = 4242

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

        # subprocess.run() (the runner's `git worktree prune` in its finally block) goes
        # through the same patched Popen, so it must look enough like one.
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, *a, **kw):
            return b"", b""

    def fake_popen(cmd, **kw):
        captured.update(kw)
        return FakeProc()

    monkeypatch.setattr(RunManager, "_provision_worktree",
                        lambda self, worktree, ref, rd: (Path(worktree).mkdir(parents=True,
                                                                  exist_ok=True),
                                                         {"ref": "origin/main",
                                                          "commit": "0" * 40})[1])
    monkeypatch.setattr("dashboard.runner.build_command",
                        lambda action, comp_id, ws, params: ["python3", "-c", "pass"])
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # ...and the runner's `git worktree prune` in its finally block talks to subprocess.run,
    # which would otherwise reach into the fake process.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    manager._run(record, comp, "main")
    assert captured["env"]["PATH"].startswith("/opt/oc/bin:"), captured["env"]["PATH"]
    assert captured["env"]["PYTHONUNBUFFERED"] == "1"
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["start_new_session"] is True
    assert captured["cwd"].endswith("engine")


def test_missing_driver_deps_reports_what_this_interpreter_lacks():
    def no_such(name):
        raise ModuleNotFoundError(name)

    from dashboard.runner import DRIVER_DEPS
    assert missing_driver_deps(spec=no_such) == list(DRIVER_DEPS)


def test_missing_driver_deps_is_empty_for_a_complete_venv():
    assert missing_driver_deps() == []


def test_submit_refuses_when_a_driver_dep_is_missing(monkeypatch, tmp_path):
    """A stale service venv must cost a refusal at submit, not a dead deploy minutes later."""
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    comp = tmp_path / "data" / "competitions" / "mini"
    comp.mkdir(parents=True)
    (comp / "Compfile").write_text("name mini\n")
    store = Store(tmp_path / "db.sqlite3")
    manager = RunManager(store, None)
    manager.runs_dir = tmp_path / "runs"
    manager.runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    monkeypatch.setattr(RunManager, "refresh_comps", lambda self: None)
    monkeypatch.setattr("dashboard.runner.missing_driver_deps", lambda: ["toml"])
    with pytest.raises(RunRefused) as raised:
        manager.submit("mini", "scrim", "main", {"teams": 1})
    assert "toml" in str(raised.value)
    assert "requirements.txt" in str(raised.value)
    assert store.list_runs() == [], "a refused submit must not leave a run record"


def test_driver_env_forces_an_absolute_ssh_key_path(monkeypatch, tmp_path):
    """A relative key path is resolved against the worktree by the engine and recorded in state,
    so it must never reach a driver relative (it made the range un-destroyable once the worktree
    was gone: `terraform destroy` -> "no file exists at <worktree>/proxmox")."""
    key = tmp_path / "tez-proxmox"
    key.write_text("KEY")
    monkeypatch.setattr(config, "engine_proxmox_file", lambda: key)
    monkeypatch.setenv("TF_VAR_ssh_private_key_path", "../proxmox")
    mgr = RunManager(store=None, bus=None)
    env = mgr._driver_env()
    assert env["TF_VAR_ssh_private_key_path"] == str(key.resolve())
    assert env["PYTHONUNBUFFERED"] == "1"


def test_driver_env_leaves_the_path_alone_without_a_key_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "engine_proxmox_file", lambda: tmp_path / "missing")
    monkeypatch.setenv("TF_VAR_ssh_private_key_path", "/already/absolute")
    mgr = RunManager(store=None, bus=None)
    assert mgr._driver_env()["TF_VAR_ssh_private_key_path"] == "/already/absolute"
