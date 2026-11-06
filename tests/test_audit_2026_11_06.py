"""Regression pins for the 2026-11-06 browser audit of the dashboard.

Two defects that were only visible by clicking through the app:

1. A `plan` (the UI's "Dry run") created a permanent board card that later reported `failed`
   and told the operator to go hunt Proxmox for VMs that never existed.
2. `/api/comps` raised FileNotFoundError on a fresh install (the competitions checkout is only
   cloned by the first run), so the SPA showed "internal server error" while /api/health said ok.
"""

import json

from dashboard import ranges


class FakeStore:
    def __init__(self, runs):
        self.runs = sorted(runs, key=lambda r: r["created_at"], reverse=True)

    def list_runs(self, state=None, comp_id=None):
        return [r for r in self.runs if not comp_id or r["comp_id"] == comp_id]

    def list_range_retires(self):
        return {}


def _rec(rid, action, comp="c", state="succeeded", rc=0, created=0.0, run_dir=None, params=None):
    return {"id": rid, "comp_id": comp, "action": action, "state": state, "returncode": rc,
            "created_at": created, "ended_at": created + 1, "params": params or {},
            "run_dir": str(run_dir), "stage": "", "log_path": None, "pid": None, "error": None}


def _workspace(tmp_path, rid):
    ws = tmp_path / rid / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name c\n")
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "c", "run_id": rid, "engine_mgmt_ip": "10.0.0.9"},
         "resources": []}))
    return ws


def test_a_plan_only_workspace_is_not_a_range(tmp_path):
    """A dry run must not board: it never built anything, so a card for it can only mislead."""
    ws = _workspace(tmp_path, "run-plan")
    built = ranges.build_ranges(FakeStore([_rec("run-plan", "plan", run_dir=ws.parent)]))
    assert built == [], [r["state"] for r in built]


def test_a_verify_only_workspace_is_not_a_range(tmp_path):
    ws = _workspace(tmp_path, "run-verify")
    built = ranges.build_ranges(FakeStore([_rec("run-verify", "verify", run_dir=ws.parent)]))
    assert built == []


def test_a_failed_deploy_still_boards(tmp_path):
    """The fix must not hide a real failed deploy: that is the card an operator needs."""
    ws = _workspace(tmp_path, "run-deploy")
    built = ranges.build_ranges(
        FakeStore([_rec("run-deploy", "deploy", state="failed", rc=1, run_dir=ws.parent)]))
    assert len(built) == 1
    assert built[0]["state"] == "failed"


def test_a_workspace_with_a_deploy_keeps_its_plan_runs(tmp_path):
    """A plan run against a real range's workspace is just another run of that range."""
    ws = _workspace(tmp_path, "run-deploy")
    built = ranges.build_ranges(FakeStore([
        _rec("run-plan", "plan", created=2.0, run_dir=ws.parent),
        _rec("run-deploy", "deploy", state="failed", rc=1, created=1.0, run_dir=ws.parent),
    ]))
    assert len(built) == 1
    assert {r["action"] for r in built[0]["runs"]} == {"plan", "deploy"}


def test_an_in_flight_plan_still_boards(tmp_path):
    """While it is running the operator must be able to see and cancel it."""
    ws = _workspace(tmp_path, "run-plan")
    built = ranges.build_ranges(
        FakeStore([_rec("run-plan", "plan", state="running", rc=None, run_dir=ws.parent)]))
    assert len(built) == 1
    assert built[0]["state"] == "building"
