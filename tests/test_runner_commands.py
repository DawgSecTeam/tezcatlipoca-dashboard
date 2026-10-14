import pytest

from dashboard.runner import RunRefused, build_command


def test_deploy_command_carries_flags():
    cmd = build_command("deploy", "comp-a", "/ws", {"teams": 3, "scoring_vmid": 1300,
                                                    "engine_node": "zfs-193"})
    assert cmd[1] == "create-competition.py"
    assert cmd[cmd.index("--comp-dir") + 1] == "/ws"
    assert cmd[cmd.index("--competition") + 1] == "comp-a"
    assert cmd[cmd.index("--teams") + 1] == "3"
    assert cmd[cmd.index("--scoring-vmid") + 1] == "1300"
    assert "--yes" in cmd and "--plan-only" not in cmd


def test_plan_and_verify_and_destroy_shapes():
    plan = build_command("plan", "c", "/ws", {"teams": 2})
    assert "--plan-only" in plan
    verify = build_command("verify", "c", "/ws", {})
    assert verify[1] == "verify-competition.py" and verify[2] == "/ws"
    destroy = build_command("destroy", "c", "/ws", {"teams_only": True})
    assert destroy[1] == "destroy-competition.py" and "--teams-only" in destroy


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
