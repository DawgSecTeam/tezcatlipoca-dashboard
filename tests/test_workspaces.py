from types import SimpleNamespace

from dashboard import config
from dashboard.runner import RunManager
from dashboard.workspaces import release, seed_workspace


class _Bus:
    def publish(self, run_id, event):
        pass


class _Proc:
    def __init__(self, rc):
        self.pid, self._rc = 4242, rc

    def wait(self):
        return self._rc


def test_release_holds_the_workspace_of_a_live_range(tmp_path):
    """The estate document (run id, ownership, teardown journal) is the only way to finish a
    failed teardown, so a run whose range still exists must not take it with it. Only the
    disposable engine worktree goes."""
    run_dir = tmp_path / "run"
    ws = run_dir / "comp"
    ws.mkdir(parents=True)
    (ws / "estate-state.json").write_text('{"identity": {"run_id": "run-engineid"}}')
    (run_dir / "engine").mkdir()

    release(run_dir, retired=False)

    assert (ws / "estate-state.json").exists()
    assert not (run_dir / "engine").exists()


def test_release_reclaims_only_bare_debris_when_retired(tmp_path):
    """A retired range's workspace is still the record a later template reclaim and the run
    page read, so release takes the bare seeded leftover and keeps the document + evidence."""
    bare, documented, with_evidence = (tmp_path / n for n in ("bare", "documented", "evidence"))
    for run_dir in (bare, documented, with_evidence):
        (run_dir / "comp").mkdir(parents=True)
        (run_dir / "engine").mkdir()
    (documented / "comp" / "estate-state.json").write_text("{}")
    (with_evidence / "comp" / ".automated-tests").mkdir()

    for run_dir in (bare, documented, with_evidence):
        release(run_dir, retired=True)

    assert not (bare / "comp").exists()
    assert (documented / "comp" / "estate-state.json").exists()
    assert (with_evidence / "comp" / ".automated-tests").exists()
    assert all(not (run_dir / "engine").exists()
               for run_dir in (bare, documented, with_evidence))


def test_a_failed_run_holds_the_workspace_that_holds_its_range(tmp_path, monkeypatch):
    """The run's deletion site must not take the estate document with it: teardown is
    journaled and idempotent so a failed destroy can be re-run, and the run id, the ownership
    set and the journal all live in `<run_dir>/comp` (known-issues: a deleted per-run
    workspace strands its range). Only the disposable engine worktree goes."""
    import json

    from dashboard.store import Store

    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    run_dir = tmp_path / "runs" / "run-deploy"
    ws = run_dir / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name mini\n")
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"run_id": "run-engineid"},
         "resources": [{"kind": "engine", "node": "pve", "vmid": 1000}]}))
    store = Store(tmp_path / "db.sqlite3")
    record = store.create_run("run-deploy", "mini", "destroy", "main",
                              {"comp_dir": str(ws)}, run_dir, run_dir / "run.log")
    mgr = RunManager(store, _Bus())
    mgr.engine_repo = tmp_path / "engine"

    def provision(self, worktree, ref, rd):
        worktree.mkdir(parents=True)
        return {"ref": ref, "commit": "cafe"}

    monkeypatch.setattr(RunManager, "_provision_worktree", provision)
    monkeypatch.setattr(RunManager, "_track_stages", lambda self, *a, **k: None)
    monkeypatch.setattr(RunManager, "_driver_env", lambda self: {})
    monkeypatch.setattr("dashboard.runner.subprocess.Popen", lambda *a, **k: _Proc(1))
    monkeypatch.setattr("dashboard.runner.subprocess.run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))

    mgr._run(record, tmp_path / "defs", "main")

    assert (ws / "estate-state.json").exists()
    assert not (run_dir / "engine").exists()
    finished = store.get_run("run-deploy")
    assert finished["engine_head"] == "cafe" and finished["state"] == "failed"


def _defs(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "Compfile").write_text("name demo\n")
    (root / "boxes.json").write_text("[]")
    (root / "injects" / "one").mkdir(parents=True)
    (root / "injects" / "one" / "inject.json").write_text("{}")


def test_seed_strips_run_state_keeps_definitions(tmp_path):
    src, dst = tmp_path / "defs", tmp_path / "run" / "comp"
    _defs(src)
    for stale in ("estate-state.json", "teams.json", "nakon-config.json",
                  ".nakon-golden.json", "passwords.json", "run.log",
                  ".badauto-config.yaml", ".postclone-swept"):
        (src / stale).write_text("{}")
    (src / ".automated-tests").mkdir()
    (src / "run-x-archive-1").mkdir()

    seed_workspace(src, dst)
    assert (dst / "Compfile").exists() and (dst / "boxes.json").exists()
    assert (dst / "injects" / "one" / "inject.json").exists()
    for stale in ("estate-state.json", "teams.json", "nakon-config.json", "run.log",
                  ".badauto-config.yaml", ".postclone-swept"):
        assert not (dst / stale).exists()
    assert not (dst / ".automated-tests").exists()
    assert not (dst / "run-x-archive-1").exists()


def test_the_red_agents_deploy_state_never_reaches_the_workspace(tmp_path):
    """tlaloc keys its world by the competition name, so a seeded `.tlaloc-state` from a manual
    run is a range's previous event arriving in a fresh workspace — the deploy refuses it as
    cross-event state (live: scrim-one, run-5882173c)."""
    src, dst = tmp_path / "defs", tmp_path / "run" / "comp"
    _defs(src)
    (src / ".tlaloc-state").mkdir()
    (src / ".tlaloc-state" / "world.json").write_text('{"comp": "demo"}')
    (src / ".tlaloc-config.yaml").write_text("run:\n  comp: demo\n")
    (src / "red" / "payloads").mkdir(parents=True)
    (src / "red" / "payloads" / "manifest.json").write_text("{}")

    seed_workspace(src, dst)

    assert (dst / "Compfile").exists()
    assert not (dst / ".tlaloc-state").exists()
    assert not (dst / ".tlaloc-config.yaml").exists()
    assert not (dst / "red").exists()


def test_a_seeded_estate_document_never_reaches_the_workspace(tmp_path):
    """The document carries every password, the run id and the VM inventory: seeding one in
    would make a fresh deploy adopt a previous run's identity (the credential-desync corpse
    the engine's ownership guards exist for)."""
    src, dst = tmp_path / "defs", tmp_path / "run" / "comp"
    _defs(src)
    (src / "estate-state.json").write_text('{"identity": {"run_id": "run-old"}}')
    seed_workspace(src, dst)
    assert not (dst / "estate-state.json").exists()
    assert (dst / "Compfile").exists()


def test_seed_leaves_existing_workspace_alone(tmp_path):
    """Resume: a workspace that already exists is the range, not a definition to refresh."""
    src, dst = tmp_path / "defs", tmp_path / "run" / "comp"
    _defs(src)
    dst.mkdir(parents=True)
    (dst / "Compfile").write_text("name old\n")
    (dst / "estate-state.json").write_text('{"identity": {"run_id": "resume-me"}}')
    seed_workspace(src, dst)
    assert (dst / "estate-state.json").read_text() == '{"identity": {"run_id": "resume-me"}}'
    assert (dst / "Compfile").read_text() == "name old\n"
