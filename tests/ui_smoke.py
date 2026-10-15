#!/usr/bin/env python3
"""Synthetic UI smoke server. Run with the repository's Python; never uses real ranges.

URL http://127.0.0.1:8791, bearer token range-ui-smoke. Serves the actual built web/dist.
All data is temporary. Every driver, subprocess and outbound HTTP operation is blocked.
"""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
import uuid

# Direct execution needs the repository root, not tests/, on the module path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def build_fixture_app(root, patches):
    root = Path(root)
    # Do not load service.py/.env or inherit any service/engine configuration.
    for name in list(os.environ):
        if name.startswith("TEZ_"):
            os.environ.pop(name)
    os.environ.update(TEZ_DASHBOARD_DATA=str(root),
                      TEZ_DASHBOARD_TOKENS="ui=range-ui-smoke",
                      TEZ_NAKON_PATH=str(root / "nakon"))
    (root / "competitions").mkdir(parents=True)
    (root / "nakon").mkdir()

    def blocked(*args, **kwargs):
        raise RuntimeError("UI smoke fixture: real drivers and outbound HTTP are disabled")

    patches.enter_context(patch("subprocess.run", blocked))
    patches.enter_context(patch("subprocess.Popen", blocked))
    patches.enter_context(patch("requests.sessions.Session.request", blocked))

    from dashboard import competition, comps_api, config, nodes, ranges
    from dashboard.runner import RunManager, RunRefused
    from dashboard.store import Store

    patches.enter_context(patch.object(comps_api, "COMPS", root / "competitions"))
    patches.enter_context(patch.object(config, "data_dir", lambda: root))
    store = Store(root / "dashboard.sqlite3")
    engines = {}
    boxes = [{"name": "web01", "last_octet": 4, "template": "ubuntu", "cpu": 1,
              "memory_mb": 1024, "disk_gb": 16},
             {"name": "dc01", "last_octet": 2, "template": "windows", "cpu": 2,
              "memory_mb": 2048, "disk_gb": 32}]

    def write_json(path, data):
        path.write_text(json.dumps(data, indent=2))

    def definition(comp_id):
        path = root / "competitions" / comp_id
        path.mkdir(exist_ok=True)
        (path / "Compfile").write_text(f"name {comp_id}\nscenario Synthetic UI fixture\n")
        for name, value in (("boxes.json", boxes), ("box_services.json", {}),
                            ("box_vulns.json", {})):
            write_json(path / name, value)
        (path / "injects").mkdir(exist_ok=True)
        return path

    definition("shared-scenario")
    definition("fixture-lab")
    nodes.save([{"name": "fixture-node", "node": "fixture-pve",
                 "endpoint": "https://192.0.2.10:8006", "datastore": "fixture-store",
                 "engine_base_vmid": 900, "token": "synthetic-node-token"}], {})

    scenarios = [
        ("ready", "shared-scenario", "succeeded", True, "ready", None),
        ("scoring", "shared-scenario", "succeeded", True, "running", None),
        ("frozen", "fixture-lab", "succeeded", True, "paused", None),
        ("building", "fixture-lab", "running", True, "ready", None),
        ("paused-build", "fixture-lab", "paused", True, "ready", None),
        ("failed-live", "fixture-lab", "failed", True, "ready", None),
        ("failed-teardown-live", "fixture-lab", "succeeded", True, "ready", 1),
        ("failed-teardown-empty", "fixture-lab", "succeeded", False, "ready", 1),
        ("retired", "fixture-lab", "succeeded", False, "ready", 0),
        ("missing-definition", "removed-scenario", "succeeded", True, "ready", None),
        ("failed-empty", "fixture-lab", "failed", False, "ready", None),
        ("engine-unavailable", "fixture-lab", "succeeded", True, "unavailable", None),
    ]
    for number, (slug, comp, run_state, vms, lifecycle, destroy_rc) in enumerate(scenarios):
        rid = f"run-fixture-{slug}"
        run_dir = root / "runs" / rid
        ws = run_dir / "comp"
        ws.mkdir(parents=True)
        (ws / "Compfile").write_text(f"name Fixture {slug.replace('-', ' ')}\n")
        write_json(ws / "boxes.json", boxes)
        resources = []
        if vms:
            resources.append({"kind": "engine", "name": f"fixture-{slug}-engine",
                              "node": "fixture-pve", "vmid": 1000 + number * 10})
            for team in (1, 2):
                resources.append({"kind": "bridge", "name": f"vmbr{110 + team}",
                                  "node": "fixture-pve"})
                for offset, box in enumerate(boxes):
                    prefix = "team1" if team == 1 else "112"
                    resources.append({"kind": "team_box", "name": f"{prefix}-{box['name']}",
                                      "node": "fixture-pve", "vmid": 1001 + number * 10
                                      + (team - 1) * 2 + offset})
        write_json(ws / "estate-state.json", {
            "identity": {"run_id": rid, "comp_name": comp,
                         "engine_mgmt_ip": f"192.0.2.{30 + number}"},
            "secrets": {"admin_password": "synthetic-admin", "inject_password": "synthetic-inject",
                        "scoring_password": "synthetic-scoring", "teams": {
                            "team1": {"identifier": "111", "password": "synthetic-team1"},
                            "team2": {"identifier": "112", "password": "synthetic-team2"}}},
            "roster": {"boxes": boxes}, "resources": resources,
            "run": {"last_phase": 4 if run_state in ("running", "paused") else 8,
                    "hold_start": lifecycle == "ready", "seeded": True,
                    "paused_after_phase": 4 if run_state == "paused" else None,
                    "degradations": ([{"step": "repair", "detail": "Synthetic web01 repair lag"}]
                                     if slug == "failed-live" else [])}})
        log = run_dir / "run.log"
        log.write_text(f"Synthetic {slug} fixture; no driver executed.\n")
        store.create_run(rid, comp, "deploy", f"fixture-ref-{number}", {"teams": 2}, run_dir, log)
        store.update_run(rid, state=run_state, stage="phase 4/8 golden_set",
                         returncode=None if run_state in ("running", "paused") else
                         (1 if run_state == "failed" else 0), started_at=time.time() - 120,
                         ended_at=None if run_state in ("running", "paused") else time.time() - 30)
        engines[str(ws)] = (None if lifecycle == "unavailable" else
                            {"competition_started": lifecycle != "ready",
                             "running": lifecycle == "running"})
        evidence = ws / ".automated-tests" / "fixture-evidence"
        evidence.mkdir(parents=True)
        (evidence / "REPORT.md").write_text(
            f"# Synthetic {slug} evidence\n\nOffline UI fixture; no infrastructure contacted.\n\n"
            "| Check | Result |\n| --- | --- |\n| web01 HTTP | PASS |\n| dc01 DNS | PASS |\n")
        write_json(evidence / "run.json", {"id": "fixture-evidence", "status": "passed",
                                          "phases": [{"phase": "verify", "status": "passed"}]})
        (evidence / "checks.txt").write_text("Synthetic verification evidence: all checks passed.\n")
        if destroy_rc is not None:
            down_dir = root / "runs" / f"{rid}-destroy"
            down_dir.mkdir()
            down_log = down_dir / "run.log"
            down_log.write_text("Synthetic teardown result.\n")
            store.create_run(f"{rid}-destroy", comp, "destroy", f"fixture-ref-{number}",
                             {"_destroy_comp_dir": str(ws)}, down_dir, down_log)
            store.update_run(f"{rid}-destroy", state="succeeded" if destroy_rc == 0 else "failed",
                             returncode=destroy_rc, ended_at=time.time() - 10)
        if slug in ("ready", "scoring", "failed-live"):
            store.upsert_requests(str(ws), comp, [
                {"id": 1, "team": "team2", "box": "web01", "by": "Synthetic player",
                 "at": "2026-10-15T12:00:00Z"}])

    def submit(self, comp_id, action, engine_ref=None, params=None):
        params = dict(params or {})
        rid = "run-fixture-click-" + uuid.uuid4().hex[:8]
        if action in ("verify", "destroy", "redeploy", "start", "freeze", "resume", "end",
                      "delete_team") or params.get("comp_dir"):
            target = self._range_workspace(comp_id, params, action)
            if target is None:
                raise RunRefused("no synthetic target workspace")
            params["_destroy_comp_dir"] = str(target)
            if action in ("start", "freeze", "resume", "end"):
                engines[str(target)] = {"competition_started": True,
                                        "running": action in ("start", "resume")}
        directory = root / "runs" / rid
        directory.mkdir()
        log = directory / "run.log"
        log.write_text(f"Synthetic {action}; no driver executed.\n")
        record = self.store.create_run(rid, comp_id, action, engine_ref or "fixture-ref", params,
                                       directory, log)
        self._emit(rid, "lifecycle", data={"state": "queued", "action": action})
        return record

    def state_stub(state):
        def operation(self, run_id):
            if self.store.get_run(run_id) is None:
                raise RunRefused("no such synthetic run")
            self.store.update_run(run_id, state=state)
            self._emit(run_id, "lifecycle", data={"state": state})
            return {"run": self.store.get_run(run_id), "detail": "synthetic UI fixture"}
        return operation

    for method, replacement in (("ensure_repos", lambda self: None),
                                ("refresh_comps", lambda self: None), ("_run", blocked),
                                ("submit", submit), ("cancel", state_stub("cancelled")),
                                ("pause", state_stub("paused")), ("resume", state_stub("running"))):
        patches.enter_context(patch.object(RunManager, method, replacement))
    patches.enter_context(patch.object(competition, "engine_status",
                                     lambda ws, state: engines.get(str(ws))))
    from dashboard.app import create_app
    app = create_app()
    app.state.fixture_ranges = ranges.build_ranges(store)
    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="tez-range-ui-smoke-") as root, ExitStack() as patches:
        app = build_fixture_app(root, patches)
        print(f"Synthetic-only UI smoke: http://127.0.0.1:{args.port} | bearer range-ui-smoke | data {root}",
              flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
