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
    for stale in ("estate-state.json", "teams.json", "nakon-config.json",
                  ".nakon-golden.json", "passwords.json", "run.log",
                  ".badauto-config.yaml", ".postclone-swept"):
        (src / stale).write_text("{}")
    (src / ".automated-tests").mkdir()
    (src / "run-x-archive-1").mkdir()

    seed_workspace(src, dst)
    assert (dst / "Compfile").exists() and (dst / "boxes.json").exists()
    assert (dst / "injects" / "one" / "inject.json").exists()
    for stale in ("estate-state.json", "teams.json", "nakon-config.json", "run.log",
                  ".badauto-config.yaml", ".postclone-swept"):
        assert not (dst / stale).exists()
    assert not (dst / ".automated-tests").exists()
    assert not (dst / "run-x-archive-1").exists()


def test_a_seeded_estate_document_never_reaches_the_workspace(tmp_path):
    """The document carries every password, the run id and the VM inventory: seeding one in
    would make a fresh deploy adopt a previous run's identity (the credential-desync corpse
    the engine's ownership guards exist for)."""
    src, dst = tmp_path / "defs", tmp_path / "run" / "comp"
    _defs(src)
    (src / "estate-state.json").write_text('{"identity": {"run_id": "run-old"}}')
    seed_workspace(src, dst)
    assert not (dst / "estate-state.json").exists()
    assert (dst / "Compfile").exists()


def test_seed_leaves_existing_workspace_alone(tmp_path):
    """Resume: a workspace that already exists is the range, not a definition to refresh."""
    src, dst = tmp_path / "defs", tmp_path / "run" / "comp"
    _defs(src)
    dst.mkdir(parents=True)
    (dst / "Compfile").write_text("name old\n")
    (dst / "estate-state.json").write_text('{"identity": {"run_id": "resume-me"}}')
    seed_workspace(src, dst)
    assert (dst / "estate-state.json").read_text() == '{"identity": {"run_id": "resume-me"}}'
    assert (dst / "Compfile").read_text() == "name old\n"
