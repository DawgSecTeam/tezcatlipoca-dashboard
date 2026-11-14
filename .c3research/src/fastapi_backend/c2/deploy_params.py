"""Per-deployment artifact params — resolve (deploy), record, and inject.

docs/PLAN_recorded_deploy_dirs.md. One generic seam so the deploy/check/remediate
pipelines all agree on where (and, in Part 2, under what names) an IOC's artifact
lives:

- ``resolve_deploy_params`` — the ADD-LOCATION selection seam. Part 1 picks a
  distinct pool dir; Part 2 extends THIS function to also pick service/cron/binary
  names from the tiered naming pools.
- ``params_env_prefix`` / ``with_params`` — the generic ``C3R_<KEY>=value`` env
  emitter shared by deploy/check/remediate. Emits every key in the params dict;
  Part 2 keys flow through automatically.
- ``existing_active_dirs`` / ``recorded_params`` — DB helpers for the exclude set
  (distinct-dir) and the REUSE path (maintenance re-runs inject the recorded dir,
  never pick a new one).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from fastapi_backend.c2.deploy_paths import pick_unused_deploy_path
from fastapi_backend.database.models import DeployedIOCs

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session


def os_key(box_os: str | None) -> str:
    """Map a box OS string → the deploy-pool key (windows | freebsd | linux)."""
    o = (box_os or "").lower()
    if "windows" in o or o.startswith("win"):
        return "windows"
    if "firewall" in o or "pfsense" in o or "freebsd" in o:
        return "freebsd"
    return "linux"


def resolve_deploy_params(
    ioc: Any, box_os: str, *,
    exclude_dirs: set[str] | None = None,
    exclude_names: dict[str, set[str]] | None = None,
    needs_backup: bool = False,
    fixed_drop_dir: str | None = None,
    preferred_names: dict[str, str] | None = None,
    dir_subdirs: set[str] | None = None,
) -> dict[str, str] | None:
    """Pick the artifact params for an ADD-LOCATION deploy, or None if exhausted.

    - ``DROP_DIR``: a difficulty-eligible pool dir not in ``exclude_dirs``. None
      means every eligible dir is occupied for this (box, ioc) — the caller
      refuses the add-location deploy (per-box skip).
      ``fixed_drop_dir`` short-circuits the pick: the implant deploy paths
      (``routes/c2.py``) already landed the binary in a rotation-chosen dir, so
      DROP_DIR is a fact to record, not a choice to make. With it set this never
      returns None (there is no dir to exhaust) and BACKUP_DIR is still picked
      distinct from it.
    - ``BACKUP_DIR``: picked when ``needs_backup=True`` (or ``ioc.backup_copy``);
      uses one difficulty tier higher (more obscure) than DROP_DIR, and is
      distinct from DROP_DIR.  Pool exhaustion is non-fatal — backup simply omitted.
    - Name keys (§5B.3): for each ``(env_key, pool_kind)`` in ``ioc.pooled_names``,
      a tiered name from ``naming_pools.pick_unused_name`` not in
      ``exclude_names[env_key]``. A name pool that is empty/exhausted is simply
      omitted (the script falls back to its ``:-<default>``) — it does NOT fail
      the whole deploy, unlike dir exhaustion.
    - ``preferred_names`` (docs/PLAN_soak_full_coverage.md §6.6.1): per-env_key
      overrides. When ``env_key`` is present, that exact name is used verbatim
      and the random pool-pick is bypassed. Missing env_keys fall back to the
      normal random selection. Values are NOT re-validated against the pool —
      the caller is trusted (soak-only rotation; the operator API forbids raw
      overrides).
    - ``dir_subdirs``: optional set of BASENAMES that already exist as
      directories directly under ``DROP_DIR`` on the target box (i.e. the
      output of ``ls -1 <DROP_DIR>`` filtered to directory entries). Feeds
      every pooled-name pick as an extra per-key exclude — any pooled name
      that names an existing subdir would land the deploy's file INSIDE the
      subdir (``cp src <dir>`` semantics), then downstream steps (dlopen,
      persistence read, check script) look at ``<DROP_DIR>/<name>`` as a
      file and fail. Live-observed 2026-09-11 on polus (Ubuntu 20.04): the
      ``snapd`` pool entry landed at ``path=/var/lib`` where
      ``/var/lib/snapd`` is the real snapd package's data dir; timestomp's
      ``touch <dir>`` returned success (false-clean signal), then
      persistence's ``dlopen(/var/lib/snapd)`` failed. Callers with an NXC
      context probe once per DROP_DIR (``ls -1 <dir>`` filtered by type=d)
      and pass the result. Without a probe (unit tests / callers without
      NXC), this stays None and behaviour is unchanged.
      See ``project_pool_dir_collision``.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    diff = getattr(ioc, "difficulty", None)
    if fixed_drop_dir:
        d: str | None = fixed_drop_dir
    else:
        d = pick_unused_deploy_path(os_key(box_os), difficulty=diff, exclude=exclude_dirs)
    if d is None:
        return None
    params: dict[str, str] = {"DROP_DIR": d}

    # Backup dir: one tier more obscure than DROP_DIR, distinct from it.
    _want_backup = needs_backup or getattr(ioc, "backup_copy", False)
    if _want_backup:
        _excl_dirs = (exclude_dirs or set()) | {d}
        _backup_diff = min((diff or 1) + 1, 4) if diff is not None else None
        _bd = pick_unused_deploy_path(os_key(box_os), difficulty=_backup_diff, exclude=_excl_dirs)
        if _bd is None:
            _log.warning("backup_copy: no pool dir available for %s on %s",
                         getattr(ioc, "name", "?"), box_os)
        else:
            params["BACKUP_DIR"] = _bd

    # dir_subdirs is a per-key exclude add-on: every pool pick must avoid
    # naming an existing subdir of DROP_DIR (would land ``cp`` INTO the dir).
    _subdirs = dir_subdirs or set()

    pooled = getattr(ioc, "pooled_names", None) or []
    if pooled:
        from fastapi_backend.core.naming_pools import (
            naming_pool_manager,  # cycle-break
        )
        for env_key, pool_kind in pooled:
            # docs/PLAN_soak_full_coverage.md §6.6.1: preferred_names bypass
            # the random pick — soak rotation records a specific pool entry
            # to exercise this run.
            if preferred_names and env_key in preferred_names:
                params[env_key] = preferred_names[env_key]
                continue
            exc = ((exclude_names or {}).get(env_key) or set()) | _subdirs
            name = naming_pool_manager.pick_unused_name(
                pool_kind, os_key(box_os), difficulty=diff, exclude=exc,
            )
            if name is not None:
                params[env_key] = name

    pooled_lists = getattr(ioc, "pooled_name_lists", None) or []
    if pooled_lists:
        from fastapi_backend.core.naming_pools import naming_pool_manager  # cycle-break
        for env_key, pool_kind, count in pooled_lists:
            # Preferred override for list-typed pools is a whitespace-separated
            # string (mirrors the deploy-script's ``:-<default>`` shape).
            if preferred_names and env_key in preferred_names:
                params[env_key] = preferred_names[env_key]
                continue
            exc = ((exclude_names or {}).get(env_key) or set()) | _subdirs
            picked = naming_pool_manager.pick_N_unused_names(
                pool_kind, os_key(box_os), count, difficulty=diff, exclude=exc,
            )
            if picked:
                params[env_key] = " ".join(picked)

    # Payload params. For IOCs whose SCORED artifact is attacker code rather than a
    # name or a path, generate the real bytes now and record them like any other
    # param, so deploy/check/remediate all see the same value.
    #
    # Deliberately NOT best-effort: an unpooled name falls back to the script's
    # `:-<default>`, but an absent payload means the deploy plants an empty value
    # and the check correctly scores the IOC as already remediated. Let it raise.
    if getattr(ioc, "needs_stager", False):
        from fastapi_backend.core.build_all import resolve_stager_b64  # cycle-break
        params["SHELLCODE_B64"] = resolve_stager_b64()

    return params


def params_env_prefix(params: dict[str, str] | None, box_os: str) -> str:
    r"""Return a shell/PS prefix that exports ``C3R_<KEY>=<value>`` for each key.

    Linux/pfSense: ``C3R_KEY='v'; export C3R_KEY; …``. Windows: ``$env:C3R_KEY='v'; …``.
    Empty/None params → empty string (script uses its ``:-<default>`` fallback).
    Values are single-quoted with any embedded single-quotes stripped (pool dirs /
    pool names never contain quotes; defensive against a malformed value breaking
    the assignment).
    """
    if not params:
        return ""
    is_win = os_key(box_os) == "windows"
    parts: list[str] = []
    for k, v in params.items():
        vv = str(v).replace("'", "")
        if is_win:
            parts.append(f"$env:C3R_{k}='{vv}'")
        else:
            parts.append(f"C3R_{k}='{vv}'; export C3R_{k}")
    return "; ".join(parts) + "; "


def params_env_dict(params: dict[str, str] | None) -> dict[str, str]:
    """Return ``{"C3R_<KEY>": value}`` for feeding ``build_inline_command(extra_env=…)``.

    Preferred over ``with_params`` for any command produced by
    ``build_inline_command`` / ``build_inline_combined_command``:

    - ``extra_env`` is baked INSIDE the delivered bootstrap (the Windows
      ``-EncodedCommand`` blob / the Linux outer shell), so the pooled dir and
      artifact NAMES never appear on the target's command line — string-
      prepending puts them there in plaintext, defeating the name/dir pools'
      whole point.
    - it goes through the builder, so the returned command keeps its
      ``WrappedSingleCommand`` / ``WrappedCommand`` type (see ``with_params``).

    Same value sanitisation as ``params_env_prefix`` (embedded single quotes
    stripped) since both end up inside single-quoted shell/PS assignments.
    """
    if not params:
        return {}
    return {f"C3R_{k}": str(v).replace("'", "") for k, v in params.items()}


def with_params(cmd: str, params: dict[str, str] | None, box_os: str) -> str:
    """Prepend the env prefix to a single command string (deploy/remediate bootstrap).

    Type-preserving: ``build_inline_command``'s fetch mode returns a
    ``WrappedSingleCommand`` (a ``str`` SUBCLASS carrying ``.result_token_id``),
    and ``build_inline_combined_command`` a ``WrappedCommand`` (``.token_id`` too).
    Plain ``prefix + cmd`` yields a bare ``str``, silently dropping those
    attributes — which disables the result callback for that call
    (``await_callback_or_none`` keys off ``.result_token_id``, so it always
    returns None → guaranteed callback-miss → the caller falls back to exactly
    the unreliable smbexec readback the callback exists to replace) and leaks
    the registry's pending entry (registered, never awaited, never discarded).
    Re-wrap so the attributes survive.

    Prefer ``params_env_dict`` + ``build_inline_command(extra_env=…)`` for new
    call sites (keeps pooled names off the target command line); this function
    remains for callers holding an already-built command.
    """
    prefix = params_env_prefix(params, box_os)
    if not prefix:
        return cmd
    out = prefix + cmd
    token_id = getattr(cmd, "token_id", None)
    result_token_id = getattr(cmd, "result_token_id", None)
    if token_id is not None or result_token_id is not None:
        from fastapi_backend.netexec.script_wrapper import (  # cycle-break
            WrappedCommand,
            WrappedSingleCommand,
        )
        if token_id is not None:
            return WrappedCommand(out, token_id, result_token_id)
        return WrappedSingleCommand(out, result_token_id)
    return out


def _active_rows(db: Session, box_ip: str, ioc_name: str) -> list[DeployedIOCs]:
    return db.execute(
        select(DeployedIOCs).where(
            DeployedIOCs.box_ip == box_ip,
            DeployedIOCs.ioc_name == ioc_name,
            DeployedIOCs.status == "active",
        )
    ).scalars().all()


def existing_active_dirs(db: Session, box_ip: str, ioc_name: str) -> set[str]:
    """The set of DROP_DIRs already recorded on ACTIVE rows for this (box, ioc)."""
    dirs = {r.params_dict.get("DROP_DIR") for r in _active_rows(db, box_ip, ioc_name)}
    return {d for d in dirs if d}


def pick_ioc_name_params(
    ioc: Any, box_os: str, *,
    exclude_names: dict[str, set[str]] | None = None,
) -> dict[str, str]:
    """Pick pooled name keys for an IOC that has no DROP_DIR (e.g. SliverCheck).

    For each (env_key, pool_kind) in ioc.pooled_names, picks a difficulty-tiered
    name from the naming pool. Returns {} when ioc has no pooled_names or when the
    pool is exhausted — never blocks the deploy (unlike resolve_deploy_params which
    returns None on dir exhaustion). The script falls back to its ``:-<default>``.
    """
    pooled = getattr(ioc, "pooled_names", None) or []
    if not pooled:
        return {}
    from fastapi_backend.core.naming_pools import naming_pool_manager  # cycle-break
    diff = getattr(ioc, "difficulty", None)
    out: dict[str, str] = {}
    for env_key, pool_kind in pooled:
        exc = (exclude_names or {}).get(env_key) or set()
        name = naming_pool_manager.pick_unused_name(
            pool_kind, os_key(box_os), difficulty=diff, exclude=exc,
        )
        if name is not None:
            out[env_key] = name
    return out


def existing_active_names(
    db: Session, box_ip: str, ioc_name: str, env_keys: list[str],
) -> dict[str, set[str]]:
    """Per-key set of pooled NAMES already active for this (box, ioc) (§5B.3).

    Feeds ``resolve_deploy_params(exclude_names=...)`` so a redeploy picks a
    *distinct* service/cron/… name — mirrors the distinct-dir rule.
    For multi-name keys (pooled_name_lists), values are space-separated; split them.
    """
    rows = _active_rows(db, box_ip, ioc_name)
    out: dict[str, set[str]] = {k: set() for k in env_keys}
    for r in rows:
        p = r.params_dict
        for k in env_keys:
            v = p.get(k)
            if v:
                # space-separated for pooled_name_lists; single value for pooled_names
                out[k].update(v.split())
    return out


def cross_ioc_active_kind_names(
    db: Session, box_ip: str, ioc: Any,
    ioc_pooled_map: dict[str, list[tuple[str, str]]],
) -> dict[str, set[str]]:
    """Per-key exclude set covering pool values already used by OTHER IOCs on this box.

    ``existing_active_names`` only excludes values used by the SAME (box, ioc_name)
    pair (redeploy-distinct-name rule). That leaves the door open for TWO different
    IOCs on the same box to draw the same value from a shared pool kind — e.g.
    ``windows_implant_hidden_service`` and ``windows_implant_inject`` both drawing
    from ``service`` kind and both getting ``DevQueryHelper``. The first IOC's
    deploy hardens the service SD; the second's ``sc.exe start`` then fails with
    ``OpenService FAILED 5: Access is denied`` — a deterministic collision.

    Live-observed 2026-09-11 on SKELD (10.20.30.3) during deploy-all;
    ``project_pool_name_svc_collision``.

    This helper closes that gap. For each ``(env_key, pool_kind)`` in ``ioc``'s
    ``pooled_names``, it returns every value ANY OTHER active DeployedIOCs row on
    the same box has recorded for a matching pool_kind, translated back into the
    current IOC's env_key namespace (two IOCs may label the same kind under
    different env_keys — the exclusion is by KIND, not env_key). The caller unions
    this with ``existing_active_names`` before handing to
    ``resolve_deploy_params(exclude_names=…)``.

    ``ioc_pooled_map`` is ``{ioc_name: pooled_names_list}`` for EVERY registered
    IOC — the caller resolves this from ``IOCDefinitionLoader.ioc_definitions``
    once and passes in, so this function stays a pure DB-and-dict helper with no
    hidden dependency on the orchestrator singleton (testable, cheap).
    """
    cur_pooled = getattr(ioc, "pooled_names", None) or []
    if not cur_pooled:
        return {}
    kinds_wanted = {kind for _, kind in cur_pooled}
    active_rows = db.execute(
        select(DeployedIOCs).where(
            DeployedIOCs.box_ip == box_ip,
            DeployedIOCs.status == "active",
        )
    ).scalars().all()
    per_kind: dict[str, set[str]] = {k: set() for k in kinds_wanted}
    for row in active_rows:
        row_pooled = ioc_pooled_map.get(row.ioc_name) or []
        if not row_pooled:
            continue
        params = row.params_dict or {}
        for env_key, pool_kind in row_pooled:
            if pool_kind not in kinds_wanted:
                continue
            v = params.get(env_key)
            if v:
                per_kind[pool_kind].update(v.split())
    out: dict[str, set[str]] = {}
    for env_key, pool_kind in cur_pooled:
        s = per_kind.get(pool_kind) or set()
        if s:
            out.setdefault(env_key, set()).update(s)
    return out


async def probe_dir_subdirs(
    nxc: Any, box_ip: str, box_os: str, username: str, password: str,
    drop_dir: str,
) -> set[str]:
    """Return the basenames of DIRECTORY entries directly under ``drop_dir`` on ``box_ip``.

    One NXC call per (box, drop_dir); feeds ``resolve_deploy_params(dir_subdirs=…)``
    so pool picks never name an existing subdir (see the ``dir_subdirs`` param
    docstring for why that is a fatal collision). Best-effort — probe failure
    returns an empty set (the caller proceeds as if no collision, which is
    unchanged legacy behaviour). Windows callers get an empty set (NTFS also
    has this class in theory, but the currently-observed failure is Linux/BSD
    ``cp src <existing-dir>`` semantics — narrower scope until a Windows-side
    repro shows up).

    Requires the caller's ``nxc`` wrapper, box cred, and OS. Returns empty
    when os is windows OR when the shell probe returns non-zero / no output.
    """
    o = os_key(box_os)
    if o == "windows":
        return set()
    protocol = "ssh"
    # `find` handles the "exists / is a dir" filter cleanly; the ``-mindepth 1
    # -maxdepth 1`` combo restricts to direct children. Trailing basename
    # printer keeps the output the same across GNU find (Linux) and BSD find
    # (pfSense/FreeBSD) — both accept ``-type d`` and ``-print``. Escape the
    # dir path via single quotes; pool dirs never contain ``'`` (see
    # params_env_prefix comment for the same guard).
    dd = str(drop_dir).replace("'", "")
    cmd = (
        f"find '{dd}' -mindepth 1 -maxdepth 1 -type d 2>/dev/null | "
        'while IFS= read -r p; do basename "$p"; done'
    )
    try:
        r = await nxc.execute(  # nxc-wrapper-bypass: dir-collision probe
            box_ip, protocol, username, password, cmd,
            powershell=False, exec_method=None,
        )
    except Exception:
        return set()
    if not getattr(r, "success", False):
        return set()
    out = (getattr(r, "output", "") or "").strip()
    if not out:
        return set()
    # Split on newlines, strip whitespace, drop empties + nxc banners.
    entries: set[str] = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # nxc's shell-exec output on Linux carries no per-line banner (unlike
        # Windows smbexec), so lines are plain basenames. Defensive: skip any
        # that contain path separators (should never happen — basename above
        # strips them).
        if "/" in line or "\\" in line:
            continue
        entries.add(line)
    return entries


def build_ioc_pooled_map(iocs_by_name: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Extract the ``{ioc_name: pooled_names}`` map from an IOC registry.

    Uses ``pooled_names`` AND ``pooled_name_lists`` (both draw from the same
    pool_kind pools and both write into ``deploy_params[env_key]``, so both
    contribute values that must be excluded).
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for name, ioc in iocs_by_name.items():
        entries: list[tuple[str, str]] = []
        for env_key, pool_kind in getattr(ioc, "pooled_names", None) or []:
            entries.append((env_key, pool_kind))
        for env_key, pool_kind, _count in getattr(ioc, "pooled_name_lists", None) or []:
            entries.append((env_key, pool_kind))
        if entries:
            out[name] = entries
    return out


def recorded_params(db: Session, deployed_ioc_id: int) -> dict[str, str]:
    """The recorded ``deploy_params`` for one row (REUSE / check / remediate path)."""
    row = db.get(DeployedIOCs, deployed_ioc_id)
    return row.params_dict if row else {}


_C3R_VAR_RE = re.compile(r"\$\{C3R_([A-Z0-9_]+)(?::-([^}]*))?\}")


def resolve_protect_paths(paths: list[str], params: dict[str, str]) -> list[str]:
    """Substitute ``${C3R_<KEY>}`` / ``${C3R_<KEY>:-default}`` in protect_paths.

    docs/PLAN_build_check_deploy_name_pools.md §5B.5. A persistence artifact at a
    pooled dir/name must have its immutable/timestomp target resolved from the
    row's recorded ``deploy_params`` — not applied to a stale static path. A path
    with no ``${C3R_...}`` template is returned unchanged (back-compat: today's
    static protect_paths like ``/bin/kill``).
    """
    def _sub(m: re.Match) -> str:
        key, default = m.group(1), m.group(2)
        val = params.get(key)
        if val is not None:
            return val
        return default if default is not None else m.group(0)

    return [_C3R_VAR_RE.sub(_sub, p) for p in paths]
