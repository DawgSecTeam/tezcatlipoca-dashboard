"""Deploy path rotation for implant delivery.

Maintains a pool of candidate deploy directories per OS and selects the
next one using either random or round-robin strategy.  Defaults to the
classic paths when no pool is configured.

Difficulty tiering (docs/PLAN_difficulty_pools_extensionless.md): each pool
dir carries a difficulty tier (config.DEPLOY_PATH_TIER_{OS}); a higher IOC
difficulty draws only from higher-or-equal tiers so hard IOCs land in
less-obvious directories.  ``difficulty=None`` (or a caller that doesn't pass
one) selects from the whole pool, preserving pre-tiering behaviour.
"""

from __future__ import annotations

import random

from fastapi_backend.core.config import config

# round-robin index keyed by (os_name, difficulty) so each band rotates
# independently.
_rr_index: dict[tuple[str, int | None], int] = {}

# /dev/shm is tmpfs on every Linux system, no exceptions — chattr +i always
# fails there. Unlike /tmp (tmpfs on some distros via systemd's tmp.mount,
# real disk on others), this one is safe to exclude statically rather than
# needing a runtime probe.
_ALWAYS_TMPFS = {
    "linux": frozenset({"/dev/shm"}),
}


def select_deploy_path(
    os_name: str, *, difficulty: int | None = None, avoid_tmpfs: bool = False
) -> str:
    """Return the next deploy directory for the given OS.

    Parameters
    ----------
    os_name:
        "windows", "linux", or "freebsd" (any other value falls through to
        the Linux default).
    difficulty:
        IOC difficulty (1-4).  Higher difficulty draws only from higher-tier
        (less-obvious) directories.  ``None`` selects from the whole pool
        (pre-tiering behaviour).
    avoid_tmpfs:
        Exclude candidates known to always be tmpfs (see ``_ALWAYS_TMPFS``).
        Set when the caller will apply immutability (chattr +i) afterward —
        tmpfs doesn't support the immutable attribute. Distro-inconsistent
        candidates like /tmp aren't covered here (whether they're tmpfs
        varies per box); callers that care should also runtime-probe before
        committing to a candidate — see ``deploy_path_candidates``'s caller
        in routes/c2.py's ``_deploy_one_implant``.
    """
    pool = _get_pool(os_name, difficulty=difficulty, avoid_tmpfs=avoid_tmpfs)
    if not pool:
        return _default(os_name)
    if config.DEPLOY_PATH_STRATEGY == "round_robin":
        key = (os_name, difficulty)
        idx = _rr_index.get(key, 0)
        path = pool[idx % len(pool)]
        _rr_index[key] = idx + 1
        return path
    return random.choice(pool)


def deploy_path_candidates(
    os_name: str, *, difficulty: int | None = None, avoid_tmpfs: bool = False
) -> list[str]:
    """Return an ordered list of candidate deploy directories to try in turn.

    The strategy-selected directory (``select_deploy_path``) comes first so the
    rotation/randomisation behaviour is preserved, followed by the remaining
    (difficulty-eligible) pool entries as fallbacks.  A caller deploying a
    binary should try each in order until a write succeeds — a single path can
    fail with EACCES (Permission denied), ETXTBSY (Text file busy: a prior
    same-named binary is still executing) or EROFS on a given box, and the
    deploy must not give up while other writable directories remain.  Falls
    back to the single OS default when no pool is configured.

    ``difficulty`` / ``avoid_tmpfs`` — see ``select_deploy_path``.
    """
    pool = _get_pool(os_name, difficulty=difficulty, avoid_tmpfs=avoid_tmpfs)
    if not pool:
        return [_default(os_name)]
    first = select_deploy_path(os_name, difficulty=difficulty, avoid_tmpfs=avoid_tmpfs)
    ordered = [first]
    ordered.extend(p for p in pool if p != first)
    return ordered


def pick_unused_deploy_path(
    os_name: str,
    *,
    difficulty: int | None = None,
    avoid_tmpfs: bool = False,
    exclude: set[str] | None = None,
) -> str | None:
    """Pick a difficulty-eligible pool dir NOT in ``exclude``, or None if none left.

    Used by the ADD-LOCATION deploy path (docs/PLAN_recorded_deploy_dirs.md): a
    redeploy that adds a new hiding location must land in a dir not already
    recorded active for that (box, ioc). ``exclude`` = the set of dirs already
    in use. Returns None when every eligible dir is excluded (pool exhausted) —
    the caller then refuses the add-location deploy (per-box skip). Random within
    the remaining set. The reuse path never calls this.
    """
    pool = _get_pool(os_name, difficulty=difficulty, avoid_tmpfs=avoid_tmpfs)
    remaining = [p for p in pool if p not in (exclude or set())]
    if not remaining:
        return None
    return random.choice(remaining)


def all_pool_dirs(os_name: str) -> list[str]:
    """Every deploy dir for an OS across ALL tiers (no difficulty filtering).

    Used by implant_scrub — cleanup must sweep every directory an implant could
    have landed in, regardless of which difficulty tier deployed it.  Returns
    the full configured pool (or the single default when none is configured).
    """
    pool = _get_full_pool(os_name)
    return pool if pool else [_default(os_name)]


def _get_full_pool(os_name: str) -> list[str]:
    if os_name == "windows":
        return list(config.DEPLOY_PATH_POOL_WINDOWS)
    if os_name == "linux":
        return list(config.DEPLOY_PATH_POOL_LINUX)
    if os_name == "freebsd":
        return list(config.DEPLOY_PATH_POOL_FREEBSD)
    return []


def _tier_map(os_name: str) -> dict[str, int]:
    if os_name == "windows":
        return config.DEPLOY_PATH_TIER_WINDOWS or {}
    if os_name == "linux":
        return config.DEPLOY_PATH_TIER_LINUX or {}
    if os_name == "freebsd":
        return config.DEPLOY_PATH_TIER_FREEBSD or {}
    return {}


def _min_tier_for(difficulty: int | None, max_tier: int) -> int:
    """Lowest eligible tier for an IOC difficulty, clamped to what the pool has.

    difficulty 1-2 -> tier >= 1 (whole pool); 3 -> tier >= 2; 4 -> tier >= 3.
    Clamped to ``max_tier`` so a pool without deep tiers never filters to empty.
    """
    if difficulty is None:
        return 1
    mapping = {1: 1, 2: 1, 3: 2, 4: 3}
    return min(mapping.get(difficulty, 1), max_tier)


def _get_pool(
    os_name: str, *, difficulty: int | None = None, avoid_tmpfs: bool = False
) -> list[str]:
    pool = _get_full_pool(os_name)
    if not pool:
        return []
    if difficulty is not None:
        tiers = _tier_map(os_name)
        max_tier = max((tiers.get(p, 1) for p in pool), default=1)
        min_tier = _min_tier_for(difficulty, max_tier)
        eligible = [p for p in pool if tiers.get(p, 1) >= min_tier]
        # Never return empty just because the tier filter excluded everything —
        # fall back to the whole pool rather than the single OS default.
        if eligible:
            pool = eligible
    if avoid_tmpfs:
        excluded = _ALWAYS_TMPFS.get(os_name, frozenset())
        pool = [p for p in pool if p not in excluded]
    return pool


def _default(os_name: str) -> str:
    return r"C:\Windows\Temp" if os_name == "windows" else "/tmp"
