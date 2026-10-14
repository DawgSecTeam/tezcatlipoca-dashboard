"""Comps API routes, offline: temp comps root, fixture catalog. Pins that the names the
routes reach into the engine (config_prompts.list_proxmox_templates, the catalog
constants) still resolve. Needs a sibling ../tezcatlipoca engine checkout for those
imports; skipped without one."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

REPO = Path(__file__).resolve().parent.parent
ENGINE = REPO.parent / "tezcatlipoca"


def _load_server():
    spec = importlib.util.spec_from_file_location("comps_api", REPO / "dashboard" / "comps_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
@unittest.skipIf(not (ENGINE / "common").is_dir(), "no sibling ../tezcatlipoca engine checkout")
class CompsRoutesTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ENGINE))
        sys.path.insert(0, str(ENGINE / "vendor" / "nakon"))
        self.addCleanup(lambda: sys.path.remove(str(ENGINE / "vendor" / "nakon")))
        self.addCleanup(lambda: sys.path.remove(str(ENGINE)))
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (root / "competitions").mkdir()
        cat = root / "catalog.json"
        cat.write_text(json.dumps([{"name": "mysql", "platform": "linux",
                                    "category": "service", "script": "x"}]))
        env = patch.dict(os.environ, {"TEZ_WEBUI_CATALOG_FILE": str(cat),
                                      "TF_VAR_proxmox_endpoint": "",
                                      "TF_VAR_proxmox_api_token": ""})
        env.start()
        self.addCleanup(env.stop)
        self.server = _load_server()
        self.server.COMPS = root / "competitions"
        self.client = TestClient(self.server.app)
        self.assertEqual(self.client.post("/api/comps", json={"id": "t1", "name": "T"}).status_code, 200)
        self.client.post("/api/comps/t1/boxes", json={"name": "web01", "last_octet": 4,
                                                      "template": "base-ubuntu24.04-fix"})

    def test_read_routes(self):
        self.assertEqual([c["id"] for c in self.client.get("/api/comps").json()], ["t1"])
        self.assertEqual(self.client.get("/api/comps/t1").status_code, 200)
        self.assertEqual(self.client.get("/api/comps/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/comps/t1/packet").status_code, 200)
        self.assertEqual(self.client.put("/api/comps/t1/packet", json={"body": "# hi\n"}).status_code, 200)
        self.assertEqual(self.client.get("/api/catalog").status_code, 200)
        self.assertEqual(self.client.get("/api/catalog/mysql").status_code, 200)
        self.assertEqual(self.client.get("/api/catalog/zzz").status_code, 404)

    def test_inject_get_put_delete(self):
        self.assertEqual(self.client.put("/api/comps/t1/injects/01-a",
                                         json={"meta": {"title": "A"}, "body": "b"}).status_code, 200)
        self.assertEqual(self.client.get("/api/comps/t1/injects/01-a").status_code, 200)
        self.assertEqual(self.client.delete("/api/comps/t1/injects/01-a").status_code, 200)

    def test_templates_use_config_ops_when_live(self):
        r = self.client.get("/api/templates").json()
        self.assertIn("base-ubuntu24.04-fix", [t["name"] for t in r])
        with patch.dict(os.environ, {"TF_VAR_proxmox_endpoint": "https://x",
                                     "TF_VAR_proxmox_api_token": "t"}), \
                patch("config.config_prompts.list_proxmox_templates", return_value=["live-tpl"]):
            r = self.client.get("/api/templates").json()
        self.assertTrue([t for t in r if t["name"] == "live-tpl" and t["live"]])

    def test_nodes(self):
        self.assertFalse(self.client.get("/api/nodes").json()["multi"])
        (self.server.COMPS / "nodes.json").write_text(json.dumps(
            {"nodes": [{"name": "a", "node": "pve1", "datastore": "hdd"}]}))
        self.assertTrue(self.client.get("/api/nodes").json()["multi"])


if __name__ == "__main__":
    unittest.main()
