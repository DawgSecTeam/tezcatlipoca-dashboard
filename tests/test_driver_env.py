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


def test_driver_path_prepends_the_configured_bin(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("TEZ_OPENCODE_BIN", "/opt/oc/bin")
    assert config.driver_path() == "/opt/oc/bin:/usr/local/bin:/usr/bin"


def test_driver_path_is_idempotent(monkeypatch):
    monkeypatch.setenv("PATH", "/opt/oc/bin:/usr/bin")
    monkeypatch.setenv("TEZ_OPENCODE_BIN", "/opt/oc/bin")
    assert config.driver_path() == "/opt/oc/bin:/usr/bin"


def test_empty_override_leaves_path_alone(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("TEZ_OPENCODE_BIN", "")
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
                        lambda self, worktree, ref, rd: Path(worktree).mkdir(parents=True,
                                                                            exist_ok=True))
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

    assert missing_driver_deps(spec=no_such) == list(
        ("toml", "requests", "urllib3", "yaml", "dotenv", "PIL", "cryptography", "websocket",
         "paramiko"))


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
