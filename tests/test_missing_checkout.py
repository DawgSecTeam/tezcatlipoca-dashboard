"""The fresh-install guard: /api/comps must name the missing checkout, not 500.

A new dashboard has no competitions clone — the runner makes it on the FIRST run — so before
2026-11-06 every read raised FileNotFoundError, the SPA showed a bare "internal server error",
and /api/health still answered ok, which made a broken install look healthy.

This module deliberately imports comps_api WITHOUT a sibling engine checkout (the module imports
fine; only the catalog routes need nakon), so it runs everywhere instead of skipping.
"""

import importlib.util
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _load(tmp: str):
    env = patch.dict(os.environ, {"TEZ_DASHBOARD_DATA": tmp,
                                  "TEZ_PROXMOX_ENDPOINT": "",
                                  "TEZ_PROXMOX_API_TOKEN": ""})
    env.start()
    try:
        spec = importlib.util.spec_from_file_location(
            "comps_api_missing_checkout", REPO / "dashboard" / "comps_api.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        env.stop()
    return mod


def test_missing_checkout_is_a_named_503_not_a_500():
    with tempfile.TemporaryDirectory() as tmp:
        mod = _load(tmp)
        mod.COMPS = Path(tmp) / "competitions"       # deliberately never created
        client = TestClient(mod.app)
        r = client.get("/api/comps")
        assert r.status_code == 503, r.status_code
        detail = r.json()["detail"]
        assert "competitions checkout is missing" in detail
        assert str(mod.COMPS) in detail
        # and it names the way out, not just the problem
        assert "clone" in detail.lower()


def test_a_present_checkout_lists_normally():
    with tempfile.TemporaryDirectory() as tmp:
        mod = _load(tmp)
        mod.COMPS = Path(tmp) / "competitions"
        mod.COMPS.mkdir()
        client = TestClient(mod.app)
        assert client.get("/api/comps").json() == []


def test_a_nameless_compfile_is_refused():
    """An empty name used to save silently and the competition fell back to its folder id."""
    with tempfile.TemporaryDirectory() as tmp:
        mod = _load(tmp)
        mod.COMPS = Path(tmp) / "competitions"
        (mod.COMPS / "c1").mkdir(parents=True)
        (mod.COMPS / "c1" / "Compfile").write_text("name Original\n")
        (mod.COMPS / "c1" / "boxes.json").write_text("[]")
        client = TestClient(mod.app)
        r = client.put("/api/comps/c1/compfile",
                       json={"items": [["name", "  "], ["scenario", "x"]]})
        assert r.status_code == 400, r.status_code
        assert "name" in r.json()["detail"].lower()
        # and the file on disk is untouched
        assert "Original" in (mod.COMPS / "c1" / "Compfile").read_text()
        # a real name still saves
        ok = client.put("/api/comps/c1/compfile", json={"items": [["name", "Renamed"]]})
        assert ok.status_code == 200
        assert "Renamed" in (mod.COMPS / "c1" / "Compfile").read_text()
