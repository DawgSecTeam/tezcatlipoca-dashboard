"""The estate document reader: the service-side mirror of the engine's estate.state.

The dashboard never imports a per-run engine worktree, so it cannot call
`estate.state.load_flat` itself — it must agree with it. These pin the shape (section
order, flat keys, resource list) and the read-only legacy fallback that keeps a pre-v0.3
range displayable.
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


def _write(tmp_path, doc, name=estate.STATE_NAME):
    ws = tmp_path / "comp"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / name).write_text(json.dumps(doc))
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


def test_the_legacy_deploy_state_still_reads(tmp_path):
    ws = tmp_path / "comp"
    ws.mkdir()
    (ws / ".deploy_state.json").write_text(json.dumps({"run_id": "run-old", "last_phase": 7}))
    assert estate.read_flat(ws) == {"run_id": "run-old", "last_phase": 7}
    assert estate.has_state(ws) is True
    # read-only: displaying a legacy range never migrates it as a side effect
    assert not estate.state_path(ws).exists()


def test_the_document_wins_over_a_stale_legacy_file(tmp_path):
    ws = _write(tmp_path, _doc())
    (ws / ".deploy_state.json").write_text(json.dumps({"run_id": "run-stale", "last_phase": 1}))
    assert estate.load_flat(ws)["run_id"] == "run-1"


def test_a_partial_document_falls_back_to_the_legacy_run_id(tmp_path):
    """A folded record can be written before the deploy records its identity, so a document
    without a run id must not shadow the legacy file's — losing it is what made teardown
    refuse a pre-estate range ("carries no run id, so ownership cannot be proven")."""
    ws = tmp_path / "comp"
    ws.mkdir()
    (ws / estate.STATE_NAME).write_text(json.dumps(
        {"schema_version": 1, "teardown": {"run_id": "run-down", "done": {}}}))
    (ws / ".deploy_state.json").write_text(json.dumps({"run_id": "run-old", "last_phase": 7}))
    flat = estate.load_flat(ws)
    assert flat["run_id"] == "run-old" and flat["last_phase"] == 7


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


def test_a_legacy_workspace_has_no_document_inventory(tmp_path):
    ws = tmp_path / "comp"
    ws.mkdir()
    (ws / ".deploy_state.json").write_text("{}")
    assert estate.resources(ws) == [] and estate.vms(ws) == []
    assert estate.has_inventory(ws) is False


def test_a_migrated_document_carries_no_inventory(tmp_path):
    """`EstateState.migrate` folds identity/secrets in WITHOUT building `resources`, so the
    workspace's terraform state is still the only VM inventory — `has_inventory` is the
    signal every inventory reader falls back on."""
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
