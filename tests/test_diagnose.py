"""Diagnosis is a rule over local facts, so it is pinned like one: a signature is named, the
line is quoted (redacted) with its offset, and the next step is stated. No engine, no model,
no network — every case here builds a run record and a log file."""

import json
import os
import time
from pathlib import Path


from dashboard import diagnose, ranges


class FakeStore:
    """The two store reads a diagnosis makes, scoped exactly like the real ones."""

    def __init__(self, events=None, requests=None):
        self._events = events or []
        self._requests = requests or []

    def get_events(self, run_id, since=0):
        events = [e for e in self._events
                  if e.get("run_id") in (None, run_id) and e.get("seq", 0) > since]
        return (events[-1]["seq"] if events else since), events

    def list_requests(self):
        return self._requests


class RangeStore:
    def __init__(self, runs):
        self.runs = sorted(runs, key=lambda r: r["created_at"], reverse=True)

    def list_runs(self, state=None, comp_id=None):
        return self.runs

    def list_range_retires(self):
        return {}


def _run(tmp_path, rid="run-1", action="deploy", state="failed", rc=1, error=None, log="",
         stage="phase 5/8 firewall_bootstrap", created=None, started=None):
    run_dir = tmp_path / "runs" / rid
    (run_dir / "comp").mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"
    log_path.write_text(log)
    now = time.time()
    return {"id": rid, "comp_id": "c", "action": action, "state": state, "stage": stage,
            "returncode": rc, "error": error,
            "created_at": now - 120 if created is None else created,
            "started_at": now - 90 if started is None else started,
            "ended_at": now - 30, "log_path": str(log_path), "run_dir": str(run_dir),
            "params": {}, "engine_ref": "main", "engine_head": "abc123def4567890"}


def _stage(seq, label, index=None, total=None, run_id=None, ts=None):
    return {"seq": seq, "kind": "stage", "stage": label, "index": index, "total": total,
            "ts": time.time() - 60 if ts is None else ts, "run_id": run_id}


def _record(rid, action, ws, state="succeeded", rc=0, created=1.0, log=None, error=None):
    return {"id": rid, "comp_id": "c", "action": action, "state": state, "returncode": rc,
            "created_at": created, "ended_at": created + 1,
            "params": ({"_destroy_comp_dir": str(ws)}
                       if action in ("destroy", "verify", "redeploy") else {}),
            "run_dir": str(ws.parent), "stage": "", "log_path": log, "pid": None,
            "error": error, "engine_ref": "main", "engine_head": None}


def _workspace(tmp_path, rid="run-1", vms=False, doc_extra=None):
    ws = tmp_path / "runs" / rid / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name c\n")
    resources = ([{"kind": "engine", "node": "pve", "name": "quotient-engine", "vmid": 1000,
                   "tags": ["tezcatlipoca"], "at": "t"}] if vms else [])
    doc = {"identity": {"comp_name": "c", "run_id": rid, "engine_mgmt_ip": "10.0.0.9"},
           "resources": resources}
    doc.update(doc_extra or {})
    (ws / "estate-state.json").write_text(json.dumps(doc))
    return ws


# ── the cause line ──────────────────────────────────────────────────────────────────────

def test_failed_deploy_names_the_phase_and_the_missing_dependency(tmp_path):
    out = diagnose.diagnose_run(FakeStore([_stage(1, "phase 5/8 firewall_bootstrap", 5, 8)]),
                                _run(tmp_path, log=(
                                    "[10:00:01] [templates] ok\n"
                                    "[10:00:03] phase 5/8 firewall_bootstrap\n"
                                    "Traceback (most recent call last):\n"
                                    "ModuleNotFoundError: No module named 'toml'\n"
                                    "\n[dashboard] failed rc=1\n")))
    assert out["severity"] == "error"
    assert out["findings"][0]["code"] == "missing_dependency"
    assert "phase 5/8" in out["headline"] and "firewall bootstrap" in out["headline"]
    assert out["cause"]["source"] == "run-log" and "No module named 'toml'" in out["cause"]["quote"]
    assert (out["stage"]["label"], out["stage"]["index"], out["stage"]["total"]) == (
        "phase 5/8 firewall_bootstrap", 5, 8)


def test_the_runner_summary_is_a_fallback_never_the_cause(tmp_path):
    out = diagnose.diagnose_run(FakeStore(), _run(tmp_path, log=(
        "nondescript output\n\n[dashboard] failed rc=1\n")))
    assert out["cause"]["source"] == "run-summary" and "failed rc=1" in out["cause"]["quote"]


def test_a_quoted_line_cannot_carry_credentials(tmp_path):
    out = diagnose.diagnose_run(FakeStore(), _run(tmp_path, log=(
        "[10:00:02] phase 2/8 engine_template\n"
        "RuntimeError: auth failed for https://root:PA55WORD@node:8006/x "
        "token=abcdef1234567890 password=hunter2hunter2\n"
        "[dashboard] failed rc=1\n")))
    blob = json.dumps(out)
    for secret in ("PA55WORD", "abcdef1234567890", "hunter2hunter2"):
        assert secret not in blob
    # the key survives so the line still reads as the thing that failed
    assert "token=<redacted>" in blob


def test_the_quote_offset_is_the_files_own(tmp_path):
    run = _run(tmp_path, log="first\nsecond\nRuntimeError: boom\n[dashboard] failed rc=1\n")
    out = diagnose.diagnose_run(FakeStore(), run)
    assert out["cause"]["line"] == 3
    raw = Path(run["log_path"]).read_bytes()
    assert raw[out["cause"]["offset"]:].startswith(b"RuntimeError: boom")


# ── run states ──────────────────────────────────────────────────────────────────────────

def test_orphaned_run_is_explained(tmp_path):
    out = diagnose.diagnose_run(FakeStore(), _run(
        tmp_path, rc=None, error="orphaned: no worker after service restart"))
    assert out["findings"][0]["code"] == "orphaned_run"
    assert "reclaim" in out["findings"][0]["advice"].lower()


def test_a_bad_engine_ref_is_named_without_an_exit_code(tmp_path):
    out = diagnose.diagnose_run(FakeStore(), _run(tmp_path, rc=None, error=(
        "RunRefused: cannot fetch engine ref 'nope' from origin: "
        "fatal: couldn't find remote ref nope")))
    assert out["findings"][0]["code"] == "ref_not_found"


def test_a_worker_crash_with_no_signature_says_so(tmp_path):
    out = diagnose.diagnose_run(FakeStore(), _run(tmp_path, rc=None, error="OSError: disk gone"))
    assert out["findings"][0]["code"] == "worker_crashed"


def test_cancelled_is_information_not_alarm(tmp_path):
    out = diagnose.diagnose_run(FakeStore(), _run(tmp_path, state="cancelled", rc=-15))
    assert out["severity"] == "info" and out["findings"][0]["code"] == "run_cancelled"
    assert out["headline"] == "Deploy was cancelled in phase 5/8 (firewall bootstrap)"


def test_a_held_deploy_points_at_resume(tmp_path):
    out = diagnose.diagnose_run(FakeStore([_stage(1, "phase 5/8 firewall_bootstrap", 5, 8)]),
                                _run(tmp_path, state="paused", rc=None,
                                     stage="paused after phase 5/8"))
    assert out["findings"][0]["code"] == "run_held"
    assert "Resume" in out["findings"][0]["advice"]
    assert out["headline"] == "Deploy held in phase 5/8 (firewall bootstrap)"


def test_a_silent_running_run_is_a_warning_not_a_failure(tmp_path):
    now = time.time()
    run = _run(tmp_path, state="running", rc=None, stage="phase 6/8 repair_sweep",
               started=now - 3600)
    os.utime(run["log_path"], (now - 3600, now - 3600))
    out = diagnose.diagnose_run(FakeStore(), run, now=now)
    assert out["findings"][0]["code"] == "run_stalled"
    assert out["severity"] == "warning"


def test_a_fresh_running_run_has_nothing_to_report(tmp_path):
    out = diagnose.diagnose_run(FakeStore([_stage(1, "phase 6/8 repair_sweep", 6, 8)]),
                                _run(tmp_path, state="running", rc=None,
                                     stage="phase 6/8 repair_sweep"))
    assert out["findings"] == [] and out["severity"] == "ok"
    assert out["headline"] == "Deploy running in phase 6/8 (repair sweep)"


def test_a_long_queued_run_suspects_provisioning(tmp_path):
    now = time.time()
    run = _run(tmp_path, state="queued", rc=None, stage="", created=now - 900, started=None)
    out = diagnose.diagnose_run(FakeStore(), run, now=now)
    assert out["findings"][0]["code"] == "run_queued_long"


def test_success_reports_the_reached_phase(tmp_path):
    out = diagnose.diagnose_run(FakeStore([_stage(1, "phase 8/8 seed", 8, 8)]),
                                _run(tmp_path, state="succeeded", rc=0))
    assert out["severity"] == "ok" and out["findings"] == []
    assert out["headline"] == "Deploy succeeded in phase 8/8 (seed)"


# ── ranges ──────────────────────────────────────────────────────────────────────────────

def _range_of(*runs):
    built = ranges.build_ranges(RangeStore(list(runs)))
    assert len(built) == 1, [r["state"] for r in built]
    return built[0]


def test_range_teardown_incomplete_quotes_the_destroy(tmp_path):
    ws = _workspace(tmp_path)
    destroy_log = tmp_path / "destroy.log"
    destroy_log.write_text("[10:00:01] teardown step red01\n"
                          "requests.exceptions.ConnectionError: connection refused to 10.0.0.9\n"
                          "[dashboard] failed rc=1\n")
    rng = _range_of(_record("run-dep", "deploy", ws),
                    _record("run-des", "destroy", ws, state="failed", rc=1, created=2.0,
                            log=str(destroy_log)))
    out = diagnose.diagnose_range(FakeStore(), rng)
    codes = [f["code"] for f in out["findings"]]
    assert "teardown_incomplete" in codes
    finding = next(f for f in out["findings"] if f["code"] == "teardown_incomplete")
    assert finding["run_id"] == "run-des"
    assert any("connection refused" in (e.get("quote") or "") for e in finding["evidence"])
    assert out["severity"] == "error"


def test_range_workspace_missing_is_an_error(tmp_path):
    missing = tmp_path / "gone" / "comp"
    rng = _range_of(_record("run-dep", "deploy", missing))
    assert rng["workspace_missing"] is True
    out = diagnose.diagnose_range(FakeStore(), rng)
    assert out["findings"][0]["code"] == "workspace_missing"


def test_kept_templates_are_information_for_a_retired_range(tmp_path):
    ws = _workspace(tmp_path, doc_extra={"resources": [
        {"kind": "engine_template", "node": "pve", "name": "tmpl", "vmid": 2000, "at": "t"}]})
    rng = _range_of(_record("run-dep", "deploy", ws),
                    _record("run-des", "destroy", ws, created=2.0))
    out = diagnose.diagnose_range(FakeStore(), rng)
    assert rng["state"] == "retired"
    assert [f["code"] for f in out["findings"]] == ["kept_templates"]
    assert out["severity"] == "info"


def test_a_live_range_with_nothing_wrong_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("dashboard.scoring_cache.snapshot", lambda rng: None)
    ws = _workspace(tmp_path, vms=True)
    rng = _range_of(_record("run-dep", "deploy", ws))
    assert rng["state"] == "live"
    out = diagnose.diagnose_range(FakeStore(), rng)
    assert out["findings"] == [] and out["severity"] == "ok"
    assert out["headline"] == "c range is live"


def test_range_degradations_and_a_failed_deploy_surface_together(tmp_path):
    ws = _workspace(tmp_path, doc_extra={"run": {"degradations": [
        {"what": "domain join", "detail": "win11-03 did not join in time"}]}})
    deploy_log = tmp_path / "deploy.log"
    deploy_log.write_text("ModuleNotFoundError: No module named 'paramiko'\n"
                          "[dashboard] failed rc=1\n")
    rng = _range_of(_record("run-dep", "deploy", ws, state="failed", rc=1, log=str(deploy_log)))
    out = diagnose.diagnose_range(FakeStore(), rng)
    codes = [f["code"] for f in out["findings"]]
    assert "missing_dependency" in codes and "degradation" in codes
    assert all(f["run_id"] == "run-dep" for f in out["findings"] if f["code"] == "missing_dependency")


def test_the_estate_documents_secrets_never_reach_a_diagnosis(tmp_path):
    ws = _workspace(tmp_path, doc_extra={
        "secrets": {"scoring_password": "s3cr3t-scoring", "teams": {"team1": {
            "identifier": "101", "password": "team1-passphrase"}}}})
    rng = _range_of(_record("run-dep", "deploy", ws))
    blob = json.dumps(diagnose.diagnose_range(FakeStore(), rng))
    assert "s3cr3t-scoring" not in blob and "team1-passphrase" not in blob


def test_a_pending_request_is_reported_without_the_whole_list(tmp_path, monkeypatch):
    monkeypatch.setattr("dashboard.scoring_cache.snapshot", lambda rng: None)
    ws = _workspace(tmp_path, vms=True)
    rng = _range_of(_record("run-dep", "deploy", ws))
    requests = [{"range_key": str(ws.resolve()), "comp_id": "c", "status": "pending",
                 "team": "team1", "box": "win11-01", "portal_id": "p1"}]
    out = diagnose.diagnose_range(FakeStore(requests=requests), rng)
    assert [f["code"] for f in out["findings"]] == ["pending_requests"]
    assert out["facts"]["pending_requests"] == 1


def test_the_diagnosis_routes_serve_the_same_answer(tmp_path, monkeypatch):
    """The panel's fetch and the agent's tool call must not be two implementations."""
    from fastapi.testclient import TestClient
    from dashboard.runner import RunManager

    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("TEZ_DASHBOARD_TOKENS", "z=sekrit")
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    monkeypatch.setattr(RunManager, "refresh_comps", lambda self: None)

    from dashboard.store import Store
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    run_dir = tmp_path / "data" / "runs" / "run-x"
    (run_dir / "comp").mkdir(parents=True)
    log = run_dir / "run.log"
    log.write_text("ModuleNotFoundError: No module named 'toml'\n[dashboard] failed rc=1\n")
    store.create_run("run-x", "c", "deploy", "main", {}, run_dir, log)
    store.update_run("run-x", state="failed", returncode=1, stage="phase 2/8 engine_template")

    from dashboard.app import create_app
    client = TestClient(create_app())
    auth = {"Authorization": "Bearer sekrit"}
    r = client.get("/api/runs/run-x/diagnosis", headers=auth)
    assert r.status_code == 200
    assert r.json()["subject"]["id"] == "run-x"
    assert r.json()["findings"][0]["code"] == "missing_dependency"
    assert client.get("/api/runs/run-x/diagnosis").status_code == 401
    assert client.get("/api/runs/ghost/diagnosis", headers=auth).status_code == 404
    assert client.get("/api/ranges/range-ghost/diagnosis", headers=auth).status_code == 404


def test_a_truncated_tail_keeps_the_files_own_line_numbers(tmp_path):
    """A deploy log can pass the read limit; the quoted line must still be the file's line,
    or the panel's "jump to log" lands on the wrong one."""
    log = tmp_path / "big.log"
    lines = [f"progress {i}" for i in range(1, 101)]
    lines += ["RuntimeError: boom", "[dashboard] failed rc=1"]
    log.write_text("\n".join(lines) + "\n")
    text, offset, size, first_line = diagnose._log_tail({"log_path": str(log)}, limit=120)
    assert offset > 0 and first_line > 1
    cause = diagnose.find_cause(text, offset, first_line)
    assert cause["line"] == 101
    assert log.read_bytes()[cause["offset"]:].startswith(b"RuntimeError: boom")


def test_connection_refused_reaches_the_network_rule(tmp_path):
    out = diagnose.diagnose_run(FakeStore(), _run(
        tmp_path, log="requests.exceptions.ConnectionError: connection refused\n"
                      "[dashboard] failed rc=1\n"))
    assert out["findings"][0]["code"] == "network"


def test_an_absent_opencode_is_called_out(tmp_path):
    out = diagnose.diagnose_run(FakeStore(), _run(
        tmp_path, rc=127, log="/bin/sh: opencode: command not found\n[dashboard] failed rc=127\n"))
    assert out["findings"][0]["code"] == "opencode_missing"


def test_an_unrecognised_failure_still_says_where_it_ended(tmp_path):
    out = diagnose.diagnose_run(FakeStore(), _run(
        tmp_path, log="nothing diagnostic here\n[dashboard] failed rc=1\n"))
    assert out["findings"][0]["code"] == "run_failed"
    assert out["cause"]["source"] == "run-summary"


def test_a_range_that_records_nothing_live_is_explained(tmp_path):
    ws = _workspace(tmp_path)
    rng = _range_of(_record("run-dep", "deploy", ws))
    assert rng["state"] == "failed" and rng["workspace_missing"] is False
    out = diagnose.diagnose_range(FakeStore(), rng)
    assert out["findings"][0]["code"] == "range_unaccounted"


def test_line_counting_survives_a_growing_log(tmp_path):
    """The prefix count is cached and extended; a polled live log must keep absolute lines."""
    log = tmp_path / "grow.log"
    log.write_text("a\nb\nc\n")
    run = {"log_path": str(log)}
    diagnose._log_tail(run)
    log.write_text("a\nb\nc\nd\ne\nRuntimeError: boom\n[dashboard] failed rc=1\n")
    text, offset, size, first_line = diagnose._log_tail(run, limit=50)
    cause = diagnose.find_cause(text, offset, first_line)
    assert cause["line"] == 6


def test_paramiko_auth_failure_is_an_ssh_rule_not_a_generic_failure(tmp_path):
    out = diagnose.diagnose_run(FakeStore(), _run(
        tmp_path, log="paramiko.ssh_exception.AuthenticationException: Authentication failed.\n"
                      "[dashboard] failed rc=1\n"))
    assert out["findings"][0]["code"] == "ssh_auth"
    assert "TEZ_SSH_PRIVATE_KEY_PATH" in out["findings"][0]["advice"]
