import pytest

from dashboard.runner import RunRefused, build_command


def test_deploy_command_carries_flags():
    cmd = build_command("deploy", "comp-a", "/ws", {"teams": 3, "scoring_vmid": 1300,
                                                    "engine_node": "node-a"})
    assert cmd[1] == "create-competition.py"
    assert cmd[cmd.index("--comp-dir") + 1] == "/ws"
    assert cmd[cmd.index("--competition") + 1] == "comp-a"
    assert cmd[cmd.index("--teams") + 1] == "3"
    assert cmd[cmd.index("--scoring-vmid") + 1] == "1300"
    assert "--yes" in cmd and "--plan-only" not in cmd


def test_deploy_can_give_a_run_its_own_team_block_and_mgmt_ip():
    # Two ranges of one competition on one node: the shared engine env pins one
    # TEZ_TEAM_IDENTIFIERS block and one engine mgmt IP, so without these flags the
    # second range can only collide (2026-10-14).
    cmd = build_command("deploy", "comp-a", "/ws", {"teams": 1, "team_identifiers": "130",
                                                    "engine_mgmt_ip": "10.0.0.251"})
    assert cmd[cmd.index("--team-identifiers") + 1] == "130"
    assert cmd[cmd.index("--engine-mgmt-ip") + 1] == "10.0.0.251"
    # absent -> the engine keeps the env/catalog defaults, exactly as before
    plain = build_command("deploy", "comp-a", "/ws", {"teams": 1})
    assert "--team-identifiers" not in plain and "--engine-mgmt-ip" not in plain


def test_plan_and_verify_and_destroy_shapes():
    plan = build_command("plan", "c", "/ws", {"teams": 2})
    assert "--plan-only" in plan
    verify = build_command("verify", "c", "/ws", {})
    assert verify[1] == "verify-competition.py" and verify[2] == "/ws"
    destroy = build_command("destroy", "c", "/ws", {"teams_only": True})
    assert destroy[1] == "destroy-competition.py" and "--teams-only" in destroy


def test_scrim_can_place_its_own_deploy_stage():
    # A scrim stages a full deploy; while another range holds the shared env's engine vmid,
    # team block or management IP, that deploy has to be told where to go.
    scrim = build_command("scrim", "c", "/ws", {"teams": 1, "scoring_vmid": 1300,
                                                "team_identifiers": "130",
                                                "engine_mgmt_ip": "10.0.0.248"})
    assert scrim[scrim.index("--scoring-vmid") + 1] == "1300"
    assert scrim[scrim.index("--team-identifiers") + 1] == "130"
    assert scrim[scrim.index("--engine-mgmt-ip") + 1] == "10.0.0.248"
    plain = build_command("scrim", "c", "/ws", {"teams": 1})
    for flag in ("--scoring-vmid", "--team-identifiers", "--engine-mgmt-ip"):
        assert flag not in plain


def test_scrim_and_redeploy_shapes():
    scrim = build_command("scrim", "c", "/ws", {"teams": 2, "duration_min": 45,
                                                "from_template": "agent-scrim", "new": "c"})
    assert scrim[1] == "run-agent-scrim.py"
    assert scrim[scrim.index("--from-template") + 1] == "agent-scrim"
    assert "--new" in scrim and scrim[scrim.index("--duration-min") + 1] == "45"
    redeploy = build_command("redeploy", "c", "/ws", {"mode": "reset", "boxes": "web01"})
    assert redeploy[1] == "redeploy-competition.py"
    assert redeploy[redeploy.index("--mode") + 1] == "reset"
    assert redeploy[redeploy.index("--boxes") + 1] == "web01"


def test_extra_args_and_unknown_action():
    cmd = build_command("verify", "c", "/ws", {"extra_args": ["--engine-ip", "10.0.0.50"]})
    assert cmd[-2:] == ["--engine-ip", "10.0.0.50"]
    with pytest.raises(RunRefused):
        build_command("nuke", "c", "/ws", {})


def test_deploy_carries_the_runs_nodes_file():
    cmd = build_command("deploy", "c", "/ws", {"teams": 1}, "/run/nodes.json")
    assert cmd[cmd.index("--nodes") + 1] == "/run/nodes.json"
    # nothing selected -> single-node from .env stays the default
    assert "--nodes" not in build_command("deploy", "c", "/ws", {"teams": 1})
    # companion ops resolve the workspace's recorded placement; they do not take the list
    assert "--nodes" not in build_command("verify", "c", "/ws", {}, "/run/nodes.json")


def test_scrim_carries_the_runs_nodes_file():
    # the scrim action drives a deploy stage; without the flag that stage landed on the
    # env's host while the UI showed the selected nodes
    cmd = build_command("scrim", "c", "/ws", {"teams": 1}, "/run/nodes.json")
    assert cmd[cmd.index("--nodes") + 1] == "/run/nodes.json"


def test_run_nodes_file_and_driver_env(tmp_path, monkeypatch):
    from dashboard import config, nodes as nodes_mod
    from dashboard.runner import RunManager

    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    nodes_mod.save([{"name": "node-a", "endpoint": "https://node-a.example:8006",
                     "node": "pve1", "datastore": "store-a", "engine_base_vmid": 901,
                     "token": "sec"}], {})
    (tmp_path / "run").mkdir()
    mgr = RunManager(store=None, bus=None)

    path = mgr._run_nodes_file({"nodes": ["node-a"]}, tmp_path / "run")
    import json as _json
    written = _json.loads(path.read_text())
    assert written["nodes"][0]["token_env"] == "TEZ_NODE_TOKEN_NODE_A"
    assert "sec" not in path.read_text()
    assert mgr._run_nodes_file({}, tmp_path / "run") is None

    # every catalog token rides the child env, so a later verify/destroy can resolve it
    assert mgr._driver_env()["TEZ_NODE_TOKEN_NODE_A"] == "sec"
