#!/usr/bin/env python3
"""Pre-test artifact CHECK (+ auto Build-All on miss) — in-process, no backend.

Wired into ``scripts/testing/run_tests.sh`` as an opt-OUT preflight (skip with
``--no-build-check`` / ``SKIP_BUILD_CHECK=1``). Runs the same CHECK oracle the
admin "Build CHECK" step uses (``core.build_check.run_build_check``): classifies
every in-scope IOC as present / missing / stale / not-applicable.

Any missing/stale gap is then split into "healable here" vs "Sliver-dependent"
(mirroring ``build_all._one``'s dispatch): Sliver implants and shellcode-baking
self_inject shims need the live server and are (re)built per test run, so they
are IGNORED here. Only when a locally-recompilable shim/payload is missing (e.g.
windows_hide_library's DLL) does it run Build-All once (shims/payloads only,
``include_implants=False``, reuse-recent). The common case — every gap being
Sliver-dependent — skips the build entirely, so the preflight stays near-instant.

Advisory, NOT a hard gate: it always exits 0, printing a WARNING that lists any
artifact that failed to build (self_inject shims fail here without Sliver, which
is expected). The point is to auto-heal the locally-buildable gaps and surface
the rest, so an @live run isn't silently exercising a stale/absent binary.

Runs against the same DB the dev backend uses (``DATABASE_URL`` / db/database.db)
— it creates a couple of OperatorTask rows, which ``run_tests.sh`` truncates on
its next backend startup anyway.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Make `fastapi_backend...` importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GREEN, YELLOW, RED, NC = "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def _log(msg: str) -> None:
    print(f"[build-check-preflight] {msg}", file=sys.stderr, flush=True)


async def _run() -> int:
    from fastapi_backend.core import build_all, build_check
    from fastapi_backend.core.task_tracker import create_task
    from fastapi_backend.database.db_init import DatabaseInitializer
    from fastapi_backend.database.models import OperatorTask
    from fastapi_backend.database.write_actor import write_actor

    # Ensure schema exists + the single-writer actor is running (both are normally
    # brought up by the app lifespan; a standalone script must do it itself).
    DatabaseInitializer(os.getenv("DATABASE_URL", "sqlite:///db/database.db")).initialize_database()
    write_actor.start()  # idempotent — no-op if the actor thread is already running

    def _read_task(task_id: int) -> tuple[dict, dict]:
        """Return (metadata_json, progress_json) for a finished task."""
        from sqlalchemy.orm import Session

        from fastapi_backend.database.db_writer import engine
        with Session(engine) as db:
            row = db.get(OperatorTask, task_id)
            meta = json.loads(row.metadata_json or "{}") if row and row.metadata_json else {}
            progress = json.loads(row.progress_json or "{}") if row and row.progress_json else {}
        return meta, progress

    def _reasons(progress: dict, wanted: set[str]) -> list[str]:
        """IOC names whose per-IOC `details.reason` (a JSON string) is in `wanted`."""
        out: list[str] = []
        for ioc_name, entry in progress.items():
            if not isinstance(entry, dict):
                continue
            details = entry.get("details")
            reason = None
            if isinstance(details, str):
                try:
                    reason = json.loads(details).get("reason")
                except (ValueError, AttributeError):
                    reason = None
            elif isinstance(details, dict):
                reason = details.get("reason")
            if reason in wanted:
                out.append(ioc_name)
        return out

    def _healable_here(names: list[str]) -> list[str]:
        """Filter CHECK gaps to those buildable WITHOUT a live Sliver server.

        Mirrors build_all._one's dispatch: a Sliver implant (SliverCheck +
        implant_spec, non-inject) and a shellcode-baking shim (self_inject —
        spec_wants_payload) both need the live server, so they are (re)built per
        test run, NOT here. What remains — locally-recompilable shims / payloads
        (shim_builder.is_recompilable, e.g. windows_hide_library's DLL) — is what
        the preflight can actually heal.
        """
        from fastapi_backend.c2 import shim_builder  # cycle-break
        from fastapi_backend.core.build_all import spec_wants_payload  # cycle-break
        from fastapi_backend.core.deploy_all import _get_ioc_loader  # cycle-break
        from iocs.base import SliverCheck  # cycle-break
        defs = _get_ioc_loader().ioc_definitions
        out: list[str] = []
        for n in names:
            ioc = defs.get(n)
            if ioc is None:
                continue
            is_implant = (
                isinstance(ioc.check, SliverCheck)
                and not getattr(ioc, "sliver_inject", False)
                and getattr(ioc, "implant_spec", None) is not None
            )
            if is_implant or spec_wants_payload(n):
                continue  # needs Sliver — built per-run, not here
            if shim_builder.is_recompilable(n):
                out.append(n)
        return out

    # 1) CHECK (informational). CHECK counts implants as "missing" because their
    #    binaries aren't pre-kept — that's expected; Sliver implants are (re)built
    #    per test run against the live server (run_tests.sh), NOT here.
    tid = create_task("build_check", "preflight", None, [], description="pre-test artifact CHECK")
    await build_check.run_build_check(tid)
    meta, progress = _read_task(tid)
    _log(f"CHECK: {meta.get('by_reason', {})}")
    gaps = _reasons(progress, {"missing", "stale"})
    if not gaps:
        _log(f"{GREEN}all artifacts present/current — nothing to build{NC}")
        return 0

    # Only run Build-All when a gap is actually healable here. If every gap is a
    # Sliver implant or a shellcode-baking self_inject shim (the common case —
    # those build per-run against the live server), skip the whole build pass so
    # the preflight stays near-instant.
    healable = _healable_here(gaps)
    if not healable:
        _log(f"{GREEN}{len(gaps)} gap(s) — all Sliver-dependent (implants / self_inject "
             f"shellcode, built per-run); nothing to build here{NC}")
        return 0

    # 2) BUILD-ALL, shims/payloads ONLY (include_implants=False). Heals the
    #    locally-buildable artifacts whose silent absence breaks a deploy (e.g.
    #    windows_hide_library's DLL). Implants are marked "skipped" here, so
    #    `failed` reflects only genuine local build failures (self_inject shims,
    #    which can't bake shellcode without Sliver, are the expected exception).
    _log(f"{YELLOW}{len(healable)} locally-buildable gap(s) — running Build-All "
         f"(shims/payloads): {healable}{NC}")
    btid = create_task("build_all", "preflight", None, [],
                       description="pre-test Build-All (shims/payloads)")
    await build_all.run_build_all(btid, "preflight", include_implants=False, force=False)

    # 3) Report Build-All's own (implant-excluded) result.
    b_meta, b_progress = _read_task(btid)
    _log("BUILD-ALL: " + ", ".join(
        f"{k}={b_meta.get(k, 0)}"
        for k in ("built", "reused", "not_applicable", "skipped", "failed", "total_iocs")
    ))
    failed = _reasons(b_progress, {"failed"})
    if failed:
        # Advisory only — never block the suite. Some of these are Sliver-dependent
        # (self_inject payloads bake Sliver-generated shellcode), so they can't
        # pre-build here without a Sliver server and are (re)built per test run,
        # just like implants — surfaced, not fatal.
        _log(f"{YELLOW}WARNING: {len(failed)} artifact(s) failed to build here "
             f"(Sliver-dependent ones — e.g. self_inject shellcode — build per-run): {failed}{NC}")
    else:
        _log(f"{GREEN}all in-scope (non-implant) artifacts built/present{NC}")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception as exc:  # never let a preflight hiccup block the test run
        _log(f"{RED}preflight errored (continuing without it): {exc}{NC}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
