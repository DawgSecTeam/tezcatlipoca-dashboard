from dashboard.workspaces import seed_workspace


def _defs(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "Compfile").write_text("name demo\n")
    (root / "boxes.json").write_text("[]")
    (root / "injects" / "one").mkdir(parents=True)
    (root / "injects" / "one" / "inject.json").write_text("{}")


def test_seed_strips_run_state_keeps_definitions(tmp_path):
    src, dst = tmp_path / "defs", tmp_path / "run" / "comp"
    _defs(src)
    for stale in (".deploy_state.json", "teams.json", "nakon-config.json",
                  ".nakon-golden.json", "passwords.json", "targets.json", "run.log",
                  "terraform.tfvars.json", ".badauto-config.yaml"):
        (src / stale).write_text("{}")
    (src / "terraform").mkdir()
    (src / "terraform" / "terraform.tfstate").write_text("{}")
    (src / ".automated-tests").mkdir()
    (src / "run-x-archive-1").mkdir()

    seed_workspace(src, dst)
    assert (dst / "Compfile").exists() and (dst / "boxes.json").exists()
    assert (dst / "injects" / "one" / "inject.json").exists()
    for stale in (".deploy_state.json", "teams.json", "nakon-config.json", "run.log"):
        assert not (dst / stale).exists()
    assert not (dst / "terraform").exists()
    assert not (dst / ".automated-tests").exists()
    assert not (dst / "run-x-archive-1").exists()


def test_seed_leaves_existing_workspace_alone(tmp_path):
    src, dst = tmp_path / "defs", tmp_path / "run" / "comp"
    _defs(src)
    dst.mkdir(parents=True)
    (dst / "Compfile").write_text("name old\n")
    (dst / ".deploy_state.json").write_text("resume me")
    seed_workspace(src, dst)
    assert (dst / ".deploy_state.json").read_text() == "resume me"
    assert (dst / "Compfile").read_text() == "name old\n"
