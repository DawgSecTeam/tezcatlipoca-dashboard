"""Range inventory: field projection from the workspace, and the secret-hygiene contract."""

import json
from pathlib import Path

from dashboard import comps_api
from dashboard.inventory import build_inventory
from dashboard.runner import RunManager

SECRETS = ["SUPERSECRET-admin", "SUPERSECRET-box", "SUPERSECRET-cred", "SUPERSECRET-domain",
           "SUPERSECRET-inject", "SUPERSECRET-pg", "SUPERSECRET-redis",
           "SUPERSECRET-scoring", "SUPERSECRET-team", "SUPERSECRET-teamsjson",
           "SUPERSECRET-token", "SUPERSECRET-credfile"]

BADAUTO_CONFIG = """deploy:
  red_vmid: 1470
  red_ip: 10.0.0.198
  ssh_key: /home/t/.ssh/SUPERSECRET-token
"""


class FakeStore:
    def __init__(self, runs):
        self.runs = runs

    def list_runs(self, state=None, comp_id=None):
        return list(self.runs)

    def list_range_retires(self):
        return {}


def _run(tmp_path, ws):
    return {"id": "run-1", "comp_id": "comp-a", "action": "deploy", "state": "succeeded",
            "returncode": 0, "created_at": 1.0, "ended_at": 2.0, "params": {"teams": 1},
            "run_dir": str(ws.parent), "stage": "", "log_path": None, "pid": None,
            "error": None}


def _comp(tmp_path, monkeypatch):
    comps = tmp_path / "comps"
    (comps / "comp-a").mkdir(parents=True)
    (comps / "comp-a" / "Compfile").write_text("name comp-a\n")
    (comps / "comp-a" / "boxes.json").write_text(json.dumps([
        {"name": "dc01", "last_octet": 2}, {"name": "web01", "last_octet": 4}]))
    monkeypatch.setattr(comps_api, "COMPS", comps)


def _keys(obj):
    if isinstance(obj, dict):
        for key, val in obj.items():
            yield key
            yield from _keys(val)
    elif isinstance(obj, list):
        for val in obj:
            yield from _keys(val)


DOCUMENT = {
    "schema_version": 1,
    "identity": {"comp_name": "comp-a", "name": "comp-a", "run_id": "run-1",
                 "scoring_vm_id": 1000, "engine_mgmt_ip": "10.0.0.252",
                 "engine_mgmt_gw": "10.0.0.1", "endpoint": "https://10.0.0.150:8006",
                 "node": "pve", "datastore": "local-lvm"},
    "secrets": {"admin_password": "SUPERSECRET-admin", "box_password": "SUPERSECRET-box",
                "box_creds": {"lead": "SUPERSECRET-cred"},
                "domain_creds": {"support": "SUPERSECRET-domain"},
                "inject_password": "SUPERSECRET-inject",
                "postgres_password": "SUPERSECRET-pg",
                "redis_password": "SUPERSECRET-redis",
                "scoring_password": "SUPERSECRET-scoring",
                "teams": {"team1": {"identifier": "113", "password": "SUPERSECRET-team"}}},
    "roster": {"boxes": [{"name": "dc01", "last_octet": 2}, {"name": "web01", "last_octet": 4}],
               "targets": {"team1-web01": {"vmid": 1332, "ip": "192.168.113.4"},
                           "113-dc01": {"vmid": 1331, "ip": "192.168.113.2"}}},
    "placement": None,
    "templates": {"engine": {"hash": "h", "vmid": 1140}, "engine_template_vmid": 1140,
                  "engine_template_hash": "h", "golden": {"web01": {"vmid": 1150, "hash": "h"}},
                  "golden_template_ids": {"web01": 1150}, "golden_ids_by_slot": {"0": {"web01": 1150}},
                  "golden_hashes": {"web01": "h"}, "golden_planted": {}, "frozen": None},
    "resources": [
        {"kind": "bridge", "node": "pve", "name": "vmbr113", "vmid": None, "bridge": None,
         "tags": [], "at": "t"},
        {"kind": "bridge", "node": "pve", "name": "vmbrW113", "vmid": None, "bridge": None,
         "tags": [], "at": "t"},
        {"kind": "engine", "node": "pve", "name": "quotient-engine", "vmid": 1000,
         "bridge": None, "tags": ["tezcatlipoca", "comp-comp-a"], "at": "t"},
        {"kind": "team_box", "node": "pve", "name": "team1-web01", "vmid": 1332,
         "bridge": None, "tags": ["tezcatlipoca"], "at": "t"},
        # a non-team1 box is named "<identifier>-<box>"
        {"kind": "team_box", "node": "pve", "name": "113-dc01", "vmid": 1331,
         "bridge": None, "tags": [], "at": "t"},
        {"kind": "golden", "node": "pve", "name": "golden-web01", "vmid": 1150,
         "bridge": None, "tags": [], "at": "t"},
        {"kind": "engine_template", "node": "pve", "name": "engine-template", "vmid": 1140,
         "bridge": None, "tags": [], "at": "t"},
    ],
    "run": {"last_phase": 8, "pipeline_version": 2, "seeded": True, "multi_node": False,
            "deployed_endpoint": "https://10.0.0.150:8006",
            "degradations": [{"step": "repair", "detail": "box web01 lagged"}],
            "assume_breach": {"ok": True, "vmid": 1471, "red_ip": "10.0.0.199"}},
    "external": {"headscale": {}, "portal": {"up": False, "url": ""}, "red01": {}},
    "teardown": {"run_id": "run-down", "done": {"red01": {"at": "t"}}, "updated_at": "t"},
    "updated_at": "t",
}


def _doc_workspace(tmp_path, document=DOCUMENT):
    """A run workspace: the estate document plus the engine's own side files."""
    ws = tmp_path / "data" / "runs" / "run-1" / "comp"
    ws.mkdir(parents=True)
    (ws / "estate-state.json").write_text(json.dumps(document))
    (ws / ".badauto-config.yaml").write_text(BADAUTO_CONFIG)
    (ws / "teams.json").write_text(json.dumps(
        {"team1": {"identifier": "113", "password": "SUPERSECRET-teamsjson"}}))
    (ws / "credentials.txt").write_text("SUPERSECRET-credfile\n")
    return ws


def _doc_inventory(tmp_path, monkeypatch):
    ws = _doc_workspace(tmp_path)
    _comp(tmp_path, monkeypatch)
    # web01 carries no last_octet, so its address can only come from the document's frozen
    # target row — which is what proves that row outranks the engine's arithmetic fallback.
    (comps_api.COMPS / "comp-a" / "boxes.json").write_text(json.dumps(
        [{"name": "dc01", "last_octet": 2}, {"name": "web01"}]))
    run = _run(tmp_path, ws)
    run["engine_ref"] = "estate-v0.3.0"
    return build_inventory(FakeStore([run]), run)


def test_document_teams_boxes_and_networks(tmp_path, monkeypatch):
    payload = _doc_inventory(tmp_path, monkeypatch)
    assert payload["counts"] == {"teams": 1, "boxes": 2, "networks": 2}
    team = payload["teams"][0]
    assert (team["key"], team["identifier"], team["subnet"], team["engine_ip"]) == \
        ("team1", "113", "192.168.113.0/24", "192.168.113.1")
    boxes = {b["name"]: b for b in team["boxes"]}
    # team1-web01 maps by team key; 113-dc01 maps by the subnet identifier (the roster)
    assert boxes["web01"] == {"name": "web01", "vmid": 1332, "ip": "192.168.113.4",
                              "node": "pve", "started": None,
                              "tags": ["tezcatlipoca"]}
    assert boxes["dc01"] == {"name": "dc01", "vmid": 1331, "ip": "192.168.113.2",
                             "node": "pve", "started": None, "tags": []}
    assert {(n["name"], n["kind"]) for n in payload["networks"]} == \
        {("vmbr113", "team"), ("vmbrW113", "transit")}
    assert payload["networks"][0]["teams"] == ["team1"]


def test_document_machines_and_deploy_facts(tmp_path, monkeypatch):
    payload = _doc_inventory(tmp_path, monkeypatch)
    machines = payload["machines"]
    assert machines["scoring_engine"] == {
        "name": "quotient-engine", "vmid": 1000, "node": "pve",
        "mgmt_ip": "10.0.0.252", "team_ips": ["192.168.113.1"]}
    # the bad-auto config still wins over the document's assume-breach record
    assert machines["red01"] == {"vmid": 1470, "ip": "10.0.0.198"}
    assert machines["engine_template"] == {"vmid": 1140, "node": "pve"}
    assert machines["goldens"] == [{"name": "web01", "vmid": 1150}]
    assert payload["deploy"] == {
        "last_phase": 8, "endpoint": "https://10.0.0.150:8006", "seeded": True,
        "multi_node": False, "degradations": [{"step": "repair", "detail": "box web01 lagged"}]}
    assert payload["range"]["comp_id"] == "comp-a"


def test_teardown_section_reports_step_names(tmp_path, monkeypatch):
    document = json.loads(json.dumps(DOCUMENT))
    document["teardown"] = {"run_id": "run-down",
                            "done": {"estate": {"at": 1}, "red01": {"at": 2}}}
    ws = _doc_workspace(tmp_path, document)
    _comp(tmp_path, monkeypatch)
    run = _run(tmp_path, ws)
    payload = build_inventory(FakeStore([run]), run)
    assert payload["teardown"] == {"run_id": "run-down", "done": ["estate", "red01"],
                                   "red01_done": True}


def test_a_document_with_nothing_recorded_projects_empty(tmp_path, monkeypatch):
    """A deploy that has built nothing projects empty sections rather than raising."""
    ws = tmp_path / "data" / "runs" / "run-1" / "comp"
    ws.mkdir(parents=True)
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "comp-a", "run_id": "run-1"}, "resources": []}))
    _comp(tmp_path, monkeypatch)
    run = _run(tmp_path, ws)
    payload = build_inventory(FakeStore([run]), run)
    assert payload["teams"] == [] and payload["networks"] == [] and payload["machines"] == {}


def test_no_document_password_leaves_the_workspace(tmp_path, monkeypatch):
    payload = _doc_inventory(tmp_path, monkeypatch)
    blob = json.dumps(payload)
    for secret in SECRETS:
        assert secret not in blob, secret
    forbidden = {"admin_password", "box_password", "box_creds", "domain_creds",
                 "inject_password", "postgres_password", "redis_password",
                 "scoring_password", "packet_pw", "packet_credlists"}
    leaked = [k for k in _keys(payload) if k in forbidden or k.endswith("_password")]
    assert leaked == []
    # the raw section names never ride along either
    assert "secrets" not in blob and "teams.json" not in blob


def _document_without_roster_boxes(identifier="113"):
    return {"identity": {"comp_name": "comp-a", "run_id": "run-1"},
            "secrets": {"teams": {"team1": {"identifier": identifier}}},
            "roster": {"targets": {"team1-web01": {"ip": "192.168.113.4"}}},
            "resources": [
                {"kind": "team_box", "node": "pve", "name": "team1-web01", "vmid": 1332},
                {"kind": "team_box", "node": "pve", "name": "team1-dc01", "vmid": 1331}]}


def test_workspace_boxes_outrank_the_edited_definition(tmp_path, monkeypatch):
    ws = tmp_path / "data" / "runs" / "run-1" / "comp"
    ws.mkdir(parents=True)
    (ws / "estate-state.json").write_text(json.dumps(_document_without_roster_boxes()))
    _comp(tmp_path, monkeypatch)
    (ws / "boxes.json").write_text(json.dumps([
        {"name": "dc01", "last_octet": 19}, {"name": "web01", "last_octet": 20}]))
    (comps_api.COMPS / "comp-a" / "boxes.json").write_text(json.dumps([
        {"name": "dc01", "last_octet": 99}]))
    run = _run(tmp_path, ws)
    payload = build_inventory(FakeStore([run]), run)
    boxes = {box["name"]: box for box in payload["teams"][0]["boxes"]}
    assert boxes["dc01"]["ip"] == "192.168.113.19"
    # A recorded address still outranks all definitions.
    assert boxes["web01"]["ip"] == "192.168.113.4"


def test_empty_snapshot_does_not_fall_through_to_current_definition(tmp_path, monkeypatch):
    ws = tmp_path / "data" / "runs" / "run-1" / "comp"
    ws.mkdir(parents=True)
    (ws / "estate-state.json").write_text(json.dumps(_document_without_roster_boxes()))
    _comp(tmp_path, monkeypatch)
    (ws / "boxes.json").write_text("[]")
    run = _run(tmp_path, ws)
    payload = build_inventory(FakeStore([run]), run)
    boxes = {box["name"]: box for box in payload["teams"][0]["boxes"]}
    assert boxes["dc01"]["ip"] is None


def test_document_roster_outranks_workspace_and_current_boxes(tmp_path, monkeypatch):
    ws = _doc_workspace(tmp_path)
    _comp(tmp_path, monkeypatch)
    (ws / "boxes.json").write_text(json.dumps([
        {"name": "dc01", "last_octet": 88}, {"name": "web01", "last_octet": 89}]))
    (comps_api.COMPS / "comp-a" / "boxes.json").write_text(json.dumps([
        {"name": "dc01", "last_octet": 98}, {"name": "web01", "last_octet": 99}]))
    run = _run(tmp_path, ws)
    payload = build_inventory(FakeStore([run]), run)
    boxes = {box["name"]: box for box in payload["teams"][0]["boxes"]}
    assert boxes["dc01"]["ip"] == "192.168.113.2"
    assert boxes["web01"]["ip"] == "192.168.113.4"


def test_snapshot_inventory_survives_a_missing_current_definition(tmp_path, monkeypatch):
    ws = _doc_workspace(tmp_path)
    monkeypatch.setattr(comps_api, "COMPS", tmp_path / "no-definitions")
    run = _run(tmp_path, ws)
    run["engine_ref"] = "origin-ref"
    payload = build_inventory(FakeStore([run]), run)
    assert payload["counts"] == {"teams": 1, "boxes": 2, "networks": 2}
    assert payload["range"]["engine_ref"] == "origin-ref"
    boxes = {box["name"]: box for box in payload["teams"][0]["boxes"]}
    assert boxes["dc01"]["ip"] == "192.168.113.2"


def test_missing_workspace_degrades_to_empty_sections(tmp_path, monkeypatch):
    _comp(tmp_path, monkeypatch)
    ws = tmp_path / "gone" / "comp"
    run = _run(tmp_path, ws)
    payload = build_inventory(FakeStore([run]), run)
    assert payload["teams"] == [] and payload["networks"] == [] and payload["machines"] == {}
    assert payload["counts"] == {"teams": 0, "boxes": 0, "networks": 0}
    assert payload["deploy"]["last_phase"] is None and payload["deploy"]["degradations"] == []
    assert payload["teardown"] == {"run_id": None, "done": [], "red01_done": False}
    assert payload["range"]["key"] == str(Path(ws).resolve())


def test_inventory_route_404_for_unknown_run(tmp_path, monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    from fastapi.testclient import TestClient
    from dashboard.app import create_app
    assert TestClient(create_app()).get("/api/runs/run-nope/inventory").status_code == 404
