"""A failed deploy must not poison the estate for its own retry.

Live 2026-11-06 (run-90f48b3a, reproduced twice): the deploy died at phase 3 with its
engine VM (1420) and team bridge (vmbr121) already recorded in the estate document. The
range then reported `state: live`, the board offered nothing but scoring chrome, and
every later deploy refused at preflight on exactly those names with advice ("sync
templates, free capacity") that cannot possibly help while the leftovers are owned.
The reclaim path that works is a destroy of the failed range; these tests pin that the
dashboard says so, keeps the range visible, and — since the boss directive of
2026-11-08 — submits that destroy automatically the moment any failed deploy rests.
The old per-deploy `auto_reclaim` opt-in is gone; a leftover param is accepted and
ignored, and a cancel is the only way to rest a deploy without reclaiming.
"""

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard import diagnose, ranges
from dashboard import runner as runner_mod


# ── (a) the range model: leftovers do not make a failed deploy `live` ────────────────────

class FakeStore:
    def __init__(self, runs: list):
        self.runs = sorted(runs, key=lambda r: r["created_at"], reverse=True)

    def list_runs(self, state=None, comp_id=None):
        return self.runs

    def list_range_retires(self):
        return {}

    def get_events(self, run_id, since=0):
        return 0, []

    def list_requests(self):
        return []


def _rec(rid, action, state="succeeded", rc=0, created=1.0, run_dir=None, params=None,
         stage="", error=None):
    return {"id": rid, "comp_id": "c", "action": action, "state": state, "returncode": rc,
            "created_at": created, "ended_at": created + 1, "params": params or {},
            "run_dir": str(run_dir), "stage": stage, "log_path": None, "pid": None,
            "error": error}


def _ws(tmp_path, rid, resources=None, run=None):
    ws = tmp_path / rid / "comp"
    ws.mkdir(parents=True)
    (ws / "estate-state.json").write_text(json.dumps({
        "identity": {"comp_name": "c", "run_id": rid},
        "run": run or {},
        "resources": resources if resources is not None else
        [{"kind": "engine", "node": "pve", "name": "quotient-engine", "vmid": 1420,
          "bridge": None},
         {"kind": "bridge", "node": "pve", "name": "vmbr121", "vmid": None,
          "bridge": "vmbr121"}]}))
    return ws


def test_a_failed_deploy_holding_leftovers_is_failed_not_live(tmp_path):
    """has_vms is true BECAUSE the deploy died: `live` here hides the one action that clears."""
    _ws(tmp_path, "run-poison")
    rng = ranges.build_ranges(FakeStore([
        _rec("run-poison", "deploy", state="failed", rc=1, run_dir=tmp_path / "run-poison",
             stage="phase 3/8 repair_sweep", error="preflight later refuses on vmid 1420")]))[0]
    assert rng["state"] == "failed"
    assert rng["deploy_failed"] is True
    # Still on the board and still owning its inventory: the leftovers are the story.
    assert rng["live"] is True and rng["has_recorded_vms"] is True
    # The run page and the board gate Verify/Reset on this: the build stopped before boxes.
    assert rng["deploy_phase"] == 3


def test_a_failed_deploy_that_owns_nothing_is_off_the_board(tmp_path):
    """The pre-existing contract: a failed range with nothing recorded stays `failed`/not-live."""
    _ws(tmp_path, "run-clean", resources=[])
    rng = ranges.build_ranges(FakeStore([
        _rec("run-clean", "deploy", state="failed", rc=1, run_dir=tmp_path / "run-clean")]))[0]
    assert (rng["state"], rng["live"], rng["has_recorded_vms"]) == ("failed", False, False)


def test_a_completed_deploy_that_crashed_afterwards_stays_live(tmp_path):
    """The estate document is the completion authority: seeded + full phase means live."""
    _ws(tmp_path, "run-seed", run={"seeded": True, "last_phase": 8})
    rng = ranges.build_ranges(FakeStore([
        _rec("run-seed", "deploy", state="failed", rc=1, run_dir=tmp_path / "run-seed")]))[0]
    assert rng["state"] == "live" and rng["live"] is True


def test_a_successful_destroy_of_the_poisoned_range_clears_it(tmp_path):
    """The way out today (destroy rc=0, reclaimed cleanly) must retire, not `failed`."""
    _ws(tmp_path, "run-fix", resources=[])
    rng = ranges.build_ranges(FakeStore([
        _rec("run-fixed", "destroy", created=2, run_dir=tmp_path / "run-fixed",
             params={"_destroy_comp_dir": str(tmp_path / "run-fix" / "comp")}),
        _rec("run-fix", "deploy", state="failed", rc=1, run_dir=tmp_path / "run-fix")]))[0]
    assert (rng["state"], rng["live"]) == ("retired", False)


def test_a_range_diagnosis_tells_the_operator_to_destroy_the_leftovers(tmp_path):
    ws = _ws(tmp_path, "run-diag")
    rng = ranges.build_ranges(FakeStore([
        _rec("run-diag", "deploy", state="failed", rc=1, run_dir=tmp_path / "run-diag",
             stage="phase 3/8 repair_sweep", error="driver exited 1")]))[0]
    out = diagnose.diagnose_range(FakeStore([]), {**rng, "runs": rng["runs"]})
    codes = [f["code"] for f in out["findings"]]
    assert "failed_deploy_leftovers" in codes
    f = next(f for f in out["findings"] if f["code"] == "failed_deploy_leftovers")
    assert "Destroy this range first" in f["advice"]
    assert "still allocated" in out["headline"]


# ── (b) the runner: opt-in auto-reclaim on a failed deploy ───────────────────────────────

class _Store:
    def __init__(self, run):
        self._run = run
        self.updates = []

    def get_run(self, run_id):
        return self._run if run_id == self._run["id"] else None

    def update_run(self, run_id, **fields):
        self.updates.append((run_id, fields))

    def get_events(self, run_id, since=0):
        return 0, []

    def requests_for_run(self, run_id):
        return []


class _Manager(runner_mod.RunManager):
    """RunManager without the db/bus/repos: the reclaim test only needs _finish's branch."""

    def __init__(self, store):
        self.store = store
        self._procs = {}
        self._lock = threading.Lock()
        self.emitted = []
        self.submitted = []
        self.refuse: Exception | None = None

    def _emit(self, run_id, kind, stage="", data=None):
        self.emitted.append((run_id, kind, data))

    def submit(self, comp_id, action, engine_ref=None, params=None):
        if self.refuse:
            raise self.refuse
        self.submitted.append((comp_id, action, engine_ref, params))
        return {"id": "run-reclaim-1", "action": action}


def _deploy_run(tmp_path, params, with_vms=True, rid="run-deploy"):
    ws = tmp_path / "runs" / rid / "comp"
    ws.mkdir(parents=True)
    (ws / "estate-state.json").write_text(json.dumps({
        "identity": {"comp_name": "c", "run_id": rid},
        "resources": ([{"kind": "team_box", "node": "pve", "name": "web01", "vmid": 1420}]
                      if with_vms else [])}))
    log_path = ws.parent.parent / "run.log"
    log_path.write_text("phase 3 ... boom\n")
    return {"id": rid, "comp_id": "c", "action": "deploy", "state": "running",
            "stage": "phase 3/8 repair_sweep", "returncode": None, "error": None,
            "created_at": 1.0, "started_at": 1.0, "ended_at": None,
            "log_path": str(log_path), "run_dir": str(ws.parent),
            "params": params, "engine_ref": "main"}


def test_a_failed_deploy_submits_its_reclaim_automatically(tmp_path):
    """Boss directive 2026-11-08: no opt-in any more — a failed deploy
    reclaiming its own leftovers is the default and only behaviour."""
    run = _deploy_run(tmp_path, {})
    mgr = _Manager(_Store(run))
    mgr._finish(run["id"], 1, error="driver exited 1")
    assert len(mgr.submitted) == 1
    comp_id, action, engine_ref, params = mgr.submitted[0]
    assert (comp_id, action, engine_ref) == ("c", "destroy", "main")
    assert params["comp_dir"] == str(Path(run["run_dir"]) / "comp")
    assert run["id"] in params["reason"]
    # The failed run's own log and events say what happened: an operator returning to the
    # run must see the reclaim was submitted, not that the range vanished silently.
    assert "auto-reclaim destroy submitted: run-reclaim-1" in Path(run["log_path"]).read_text()
    assert any(d.get("auto_reclaim_run") == "run-reclaim-1" for _, _, d in mgr.emitted)


def test_the_retired_auto_reclaim_param_is_accepted_and_ignored(tmp_path):
    """Deploys queued before the directive may still carry the opt-in param;
    it must fire the one automatic reclaim, never a second teardown."""
    run = _deploy_run(tmp_path, {"auto_reclaim": True})
    mgr = _Manager(_Store(run))
    mgr._finish(run["id"], 1, error="driver exited 1")
    assert len(mgr.submitted) == 1


def test_a_cancelled_deploy_never_auto_reclaims(tmp_path):
    """Cancel is the operator's hand on the wheel: they may still want to
    inspect the half. Even the old opt-in param cannot force a reclaim on
    a cancelled deploy."""
    run = _deploy_run(tmp_path, {"auto_reclaim": True})
    mgr = _Manager(_Store(run))
    mgr._finish(run["id"], -15, cancelling=True)
    assert mgr.submitted == []


def test_a_phase_one_crash_with_nothing_recorded_skips_the_destroy(tmp_path):
    run = _deploy_run(tmp_path, {"auto_reclaim": True}, with_vms=False)
    mgr = _Manager(_Store(run))
    mgr._finish(run["id"], 1, error="driver exited 1")
    assert mgr.submitted == []
    assert "auto-reclaim" not in Path(run["log_path"]).read_text()


def test_a_refused_reclaim_is_a_note_not_a_second_failure(tmp_path):
    run = _deploy_run(tmp_path, {"auto_reclaim": True})
    mgr = _Manager(_Store(run))
    mgr.refuse = runner_mod.RunRefused("a destroy is already running on this range")
    mgr._finish(run["id"], 1, error="driver exited 1")  # must not raise
    assert mgr.submitted == []
    assert "auto-reclaim refused" in Path(run["log_path"]).read_text()
    # The deploy's own terminal state still lands.
    assert any(f.get("state") == "failed" for _, f in mgr.store.updates)
