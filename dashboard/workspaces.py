"""Run workspaces: seed a comp's definitions into the run dir the engine writes in.

The competitions repo is read-only definitions; every per-run output (state, generated
configs, evidence) accumulates in the workspace — the engine treats it as its comp dir via
--comp-dir. Fresh seeds strip any run-state leftovers so a workspace never starts life
looking like someone's old deploy.

`estate-state.json` is the v0.3.0 replacement for `.deploy_state.json` and is RUNTIME state
above all else: it carries every generated password, the run id (the ownership proof
teardown/redeploy compare against) and the VM inventory the destroy path walks. Copying one
in from the definitions repo would make a fresh deploy adopt a previous run's identity —
the credential desync the engine's own ownership guards refuse to touch.
"""

import shutil
from pathlib import Path

# Names the engine treats as per-run state (mirrors tezcatlipoca's competitions/*
# gitignore section and scrim/staging.RUNTIME_FILES; `estate-state.json` and the teardown
# journal are the v0.3.0 names).
_STATE_NAMES = {
    "teams.json", "event.conf", "linux.credlist", "credentials.txt", "portal.json",
    "portal-access.log", ".deploy_state.json", "estate-state.json", ".teardown-journal.json",
    ".deploy-timings.jsonl", ".deploy.lock",
    "nakon-config.json", ".badauto-config.yaml", ".realm-c2-config.yaml", ".frozen.json",
    ".template-hashes.json", ".phase6-swept", ".postclone-swept", "targets.json",
    "known_hosts", "terraform.tfvars.json", "passwords.json", "box_baseline.json",
    "domain_accounts.json", "box_vulns.json.pre-cyberfield-prune1",
}
_STATE_DIRS = {
    # `terraform/` and the *.tfstate* globs below are pre-v0.3 leftovers: v0.3.0 has no
    # terraform at all, and the reference comps that keep a tracked terraform snapshot must
    # not have it seeded into a live run's workspace.
    "terraform", ".automated-tests", "remote-access", "evidence",
}
# NOT state (they are DEFINITIONS the deploy reads): `pfsense/` holds the factory
# `pfsense-config-orig.xml` seed phase 5 clones from and `vyos/` the VyOS equivalent — skipping
# them left a workspace whose firewall bootstrap died on a missing seed file (2026-10-14,
# run-8482163b: "pfsense-config-orig.xml is missing"). The per-team config the deploy writes
# into those dirs is regenerated on every deploy, so copying them is safe.
_STATE_GLOBS = (".nakon-*", "*.tfstate*", "*.log", "run*-archive-*", "persistence-evidence-*",
                ".prev-run-*", ".catalog-check-*")


def _is_state(name):
    from fnmatch import fnmatch
    return (name in _STATE_NAMES or name in _STATE_DIRS
            or any(fnmatch(name, g) for g in _STATE_GLOBS))


def seed_workspace(comp_src, run_dir):
    # Definitions -> run workspace. An existing workspace is left alone (resume /
    # verify / destroy all act on the state a prior run left behind).
    comp_src, run_dir = Path(comp_src), Path(run_dir)
    if run_dir.resolve() == comp_src.resolve():
        return run_dir
    if (run_dir / "Compfile").exists():
        return run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    for item in comp_src.iterdir():
        if _is_state(item.name):
            continue
        dst = run_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dst, symlinks=True)
        else:
            shutil.copy2(item, dst, follow_symlinks=False)
    return run_dir
