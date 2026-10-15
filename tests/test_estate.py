"""The estate document reader: the service-side mirror of the engine's estate.state.

The dashboard never imports a per-run engine worktree, so it cannot call
`estate.state.load_flat` itself — it must agree with it. These pin the shape (section
order, flat keys, resource list).
"""

import json

from dashboard import estate


def _doc(**overrides):
    doc = {
        "schema_version": 1,
        "identity": {"comp_name": "comp-a", "run_id": "run-1", "scoring_vm_id": 1000,
                     "engine_mgmt_ip": "10.0.0.252", "node": "pve",
                     "deployed_endpoint": "https://10.0.0.150:8006"},
        "secrets": {"admin_password": "s", "teams": {"team1": {"identifier": "113",
                                                               "password": "s"}}},
        "roster": {"boxes": [{"name": "web01", "last_octet": 4}]},
        "placement": None,
        "templates": {"engine_template_vmid": 1140, "golden_template_ids": {"web01": 1150}},
        "resources": [
            {"kind": "engine", "node": "pve", "name": "quotient-engine", "vmid": 1000,
             "bridge": None, "tags": ["tezcatlipoca", "run-1"], "at": "t"},
            {"kind": "team_box", "node": "pve", "name": "team1-web01", "vmid": 1332,
             "bridge": None, "tags": [], "at": "t"},
            {"kind": "bridge", "node": "pve", "name": "vmbr113", "vmid": None,
             "bridge": None, "tags": [], "at": "t"},
            {"kind": "golden", "node": "pve", "name": "golden-web01", "vmid": 1150,
             "bridge": None, "tags": [], "at": "t"},
        ],
        "run": {"last_phase": 8, "paused_after_phase": 3, "seeded": True},
        "external": {},
        "teardown": {"run_id": "run-down", "done": {"red01": {"at": "t"}}, "updated_at": "t"},
        "updated_at": "t",
    }
    doc.update(overrides)
    return doc


def _write(tmp_path, doc):
    ws = tmp_path / "comp"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / estate.STATE_NAME).write_text(json.dumps(doc))
    return ws


def test_state_path_is_the_document_name(tmp_path):
    assert estate.state_path(tmp_path) == tmp_path / "estate-state.json"


def test_load_flat_folds_the_sections_in_the_engines_order(tmp_path):
    ws = _write(tmp_path, _doc())
    flat = estate.load_flat(ws)
    assert flat["run_id"] == "run-1"                       # identity
    assert flat["admin_password"] == "s"                   # secrets
    assert flat["boxes"] == [{"name": "web01", "last_octet": 4}]   # roster
    assert flat["engine_template_vmid"] == 1140            # templates
    assert flat["last_phase"] == 8 and flat["seeded"] is True      # run
    assert flat["paused_after_phase"] == 3
    # a section's own name never lands in the flat view
    assert "secrets" not in flat and "templates" not in flat


def test_read_flat_is_none_without_any_state(tmp_path):
    ws = tmp_path / "empty"
    ws.mkdir()
    assert estate.read_flat(ws) is None
    assert estate.load_flat(ws) == {}
    assert estate.has_state(ws) is False


def test_a_document_without_an_identity_is_still_state(tmp_path):
    """A document that exists WITHOUT an identity reads as state: `run_id` is the ownership
    key every destroy path requires, and a caller that finds none must refuse."""
    ws = tmp_path / "comp"
    ws.mkdir()
    (ws / estate.STATE_NAME).write_text(json.dumps(
        {"schema_version": 1, "teardown": {"run_id": "run-down", "done": {}}}))
    assert estate.read_flat(ws) == {}
    assert estate.has_state(ws) is True


def test_a_torn_document_degrades_instead_of_raising(tmp_path):
    ws = tmp_path / "comp"
    ws.mkdir()
    estate.state_path(ws).write_text("{not json")
    assert estate.load_flat(ws) == {}
    assert estate.document(ws) == {}
    assert estate.resources(ws) == []


def test_resources_are_the_documents_inventory(tmp_path):
    ws = _write(tmp_path, _doc())
    kinds = [r["kind"] for r in estate.resources(ws)]
    assert kinds == ["engine", "team_box", "bridge", "golden"]
    assert [r["vmid"] for r in estate.vms(ws)] == [1000, 1332, 1150]


def test_a_document_with_no_resources_carries_no_inventory(tmp_path):
    """An empty `resources` list is the signal: a deploy that has built nothing, or a
    teardown that has emptied the list, records no VM inventory."""
    ws = tmp_path / "comp"
    ws.mkdir()
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "comp-a", "run_id": "run-1"}, "resources": [],
         "roster": {"targets": {}}}))
    assert estate.has_state(ws) is True
    assert estate.has_inventory(ws) is False
    assert estate.vms(ws) == []
    # ...while a document that recorded a VM does carry one
    ws2 = _write(tmp_path / "two", _doc())
    assert estate.has_inventory(ws2) is True


def test_teardown_section_reads_the_document(tmp_path):
    ws = _write(tmp_path, _doc())
    assert estate.teardown(ws)["run_id"] == "run-down"
    assert estate.teardown(tmp_path / "nope") == {}
