"""Parity with the engine's own state accessor.

The dashboard cannot import `estate.state.load_flat` at runtime: the engine lives in a
per-run worktree, not on the service's path (the sibling checkout is only available to the
test suite). So `dashboard/estate.py` mirrors it — and these tests pin the mirror against
the original, on documents the ORIGINAL wrote. If the engine adds a section, moves a flat
key or changes the resource shape, this fails here instead of quietly blanking a page.

Skipped when there is no sibling `../tezcatlipoca` checkout (the same guard the comps API
tests use).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENGINE = REPO.parent / "tezcatlipoca"
HAVE_ENGINE = (ENGINE / "estate" / "state.py").is_file()


@unittest.skipIf(not HAVE_ENGINE, "no sibling ../tezcatlipoca engine checkout")
class EstateParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ENGINE))
        cls.addClassCleanup(lambda: sys.path.remove(str(ENGINE)))
        from estate import state as engine_state
        cls.engine_state = engine_state

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "comp"
        self.ws.mkdir()

    def _engine_document(self):
        """A document built by the engine's own writer, with the phases' flat keys."""
        from dashboard import estate as dash_estate
        doc = self.engine_state.EstateState.load(self.ws)
        doc.absorb({
            "comp_name": "comp-a", "run_id": "run-1", "scoring_vm_id": 1000,
            "engine_mgmt_ip": "10.0.0.252", "engine_mgmt_gw": "10.0.0.1",
            "deployed_endpoint": "https://10.0.0.150:8006", "node": "pve",
            "datastore": "local-lvm",
            "teams": {"team1": {"identifier": "113", "password": "secret"}},
            "admin_password": "secret", "box_password": "secret",
            "engine_template_vmid": 1140, "golden_template_ids": {"web01": 1150},
            "boxes": [{"name": "web01", "last_octet": 4}],
            "last_phase": 8, "seeded": True, "multi_node": False,
            "paused_after_phase": 3, "hold_start": True, "ended": False,
            "degradations": [{"step": "repair"}], "assume_breach": {"vmid": 1471},
            "portal_up": False, "portal_url": "",
        })
        doc.record("bridge", "pve", "vmbr113")
        doc.record("engine", "pve", "quotient-engine", vmid=1000, tags=["tezcatlipoca"])
        doc.record("team_box", "pve", "team1-web01", vmid=1332, tags=["tezcatlipoca"])
        doc.record("golden", "pve", "golden-web01", vmid=1150)
        doc.record("engine_template", "pve", "engine-template", vmid=1140)
        doc.section("teardown").update({"run_id": "run-down", "done": {"red01": {}}})
        doc.save()
        return dash_estate

    def test_load_flat_matches_the_engines(self):
        dash_estate = self._engine_document()
        self.assertEqual(self.engine_state.load_flat(self.ws), dash_estate.load_flat(self.ws))

    def test_read_flat_matches_on_the_legacy_file(self):
        from dashboard import estate as dash_estate
        (self.ws / ".deploy_state.json").write_text(
            json.dumps({"run_id": "run-old", "last_phase": 7, "teams": {}}))
        self.assertEqual(self.engine_state.read_flat(self.ws), dash_estate.read_flat(self.ws))

    def test_read_flat_matches_on_an_empty_workspace(self):
        from dashboard import estate as dash_estate
        self.assertEqual(self.engine_state.read_flat(self.ws), dash_estate.read_flat(self.ws))
        self.assertEqual(self.engine_state.load_flat(self.ws), dash_estate.load_flat(self.ws))

    def test_the_flat_view_carries_the_keys_the_dashboard_reads(self):
        from dashboard import estate as dash_estate
        self._engine_document()
        flat = dash_estate.load_flat(self.ws)
        for key in ("run_id", "last_phase", "paused_after_phase", "seeded", "ended",
                    "engine_mgmt_ip", "deployed_endpoint", "degradations", "teams",
                    "assume_breach", "engine_template_vmid", "golden_template_ids"):
            self.assertIn(key, flat, key)
        # ...and the resource list the range/inventory projections need
        kinds = [row["kind"] for row in dash_estate.resources(self.ws)]
        self.assertEqual(kinds, ["bridge", "engine", "team_box", "golden", "engine_template"])
        self.assertEqual([r["vmid"] for r in dash_estate.vms(self.ws)], [1000, 1332, 1150, 1140])
        self.assertEqual(dash_estate.teardown(self.ws)["run_id"], "run-down")


if __name__ == "__main__":
    unittest.main()
