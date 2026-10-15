"""Service configuration from the environment (.env loaded by service.py)."""

import os
from pathlib import Path


def data_dir():
    return Path(os.environ.get("TEZ_DASHBOARD_DATA") or (Path(__file__).resolve().parents[1] / "data"))


def engine_repo_url():
    return os.environ.get("TEZ_ENGINE_REPO_URL") or "git@github.com:DawgSecTeam/tezcatlipoca.git"


def competitions_repo_url():
    return os.environ.get("TEZ_COMPETITIONS_REPO_URL") or "git@github.com:DawgSecTeam/tezcatlipoca-competitions.git"


def badauto_path():
    raw = os.environ.get("TEZ_BADAUTO_PATH")
    return Path(raw) if raw else None


def competitions_checkout():
    return data_dir() / "competitions"


def engine_checkout():
    return data_dir() / "engine"


def nakon_dir():
    return nakon_path() or (engine_checkout() / "vendor" / "nakon")


def nakon_path():
    raw = os.environ.get("TEZ_NAKON_PATH")
    return Path(raw) if raw else None


def engine_env_file():
    raw = os.environ.get("TEZ_ENGINE_ENV_FILE")
    return Path(raw) if raw else None


def engine_proxmox_file():
    raw = os.environ.get("TEZ_ENGINE_PROXMOX_FILE")
    return Path(raw) if raw else None


def engine_config_file():
    """The engine's native profile (v0.3.0's `tezcatlipoca.toml`), copied into every worktree.

    Optional: `TEZ_ENGINE_ENV_FILE`'s `.env` already carries every key under its legacy
    name, which v0.3.0's estate/config.py still resolves. A profile is the new surface (and
    the place a per-node `TEZ_CONFIG` split lands), so give the operator a way to use it
    without hand-copying into a throwaway checkout."""
    raw = os.environ.get("TEZ_ENGINE_CONFIG_FILE")
    return Path(raw) if raw else None


def default_engine_ref():
    """The tezcatlipoca revision a run pins when the submit names no ref.

    `main` is the integration branch: the v0.3.0 estate release is fully contained in it
    (terraform is gone, a range's state is `estate-state.json`) and the fixes landed since
    are not. Pin a SHA for an event, or another branch, with TEZ_ENGINE_REF."""
    return os.environ.get("TEZ_ENGINE_REF") or "main"


def tokens():
    # client-name=token pairs; the token is what authenticates, the name labels it.
    out = {}
    for pair in (os.environ.get("TEZ_DASHBOARD_TOKENS") or "").split(","):
        if "=" in pair:
            name, token = pair.split("=", 1)
            out[token.strip()] = name.strip()
    return out


def host():
    return os.environ.get("TEZ_DASHBOARD_HOST") or "127.0.0.1"


def port():
    return int(os.environ.get("TEZ_DASHBOARD_PORT") or "8787")


def public_url():
    """The base URL a DEPLOYED PORTAL (on an engine VM) reaches this service at.

    Only what TEZ_PUBLIC_URL says is passed to the portal (TEZ_OPS_URL): a defaulted
    127.0.0.1 or 0.0.0.0 would wire a channel the portal can never deliver on."""
    return os.environ.get("TEZ_PUBLIC_URL") or ""


def opencode_bin():
    """The directory holding `opencode`, prepended to the driver child's PATH.

    The drivers run as children of this service, so the harness's blue cycles inherit the
    SERVICE's PATH — and a systemd unit's PATH is `/usr/local/bin:/usr/bin`, where a
    user-installed `opencode` (default `~/.opencode/bin`, installed by opencode's own
    installer, not by a package manager) simply is not. Absent it, every blue cycle execs
    `opencode`, fails rc=127, and the run silently degrades to watchdog-only defense
    (measured: all 23 cycles of one run). `TEZ_OPENCODE_BIN` overrides; empty means the
    lookup is left alone.
    """
    raw = os.environ.get("TEZ_OPENCODE_BIN")
    if raw is not None:
        return raw
    default = Path.home() / ".opencode" / "bin"
    return str(default) if default.is_dir() else ""


def user_bin():
    """The user's ~/.local/bin, when it exists: where user-space tool installs live on a run
    host with no sudo (sshpass here — Windows boxes are reached over password auth and it is
    not a package the service can install for itself)."""
    default = Path.home() / ".local" / "bin"
    return str(default) if default.is_dir() else ""


def driver_path():
    """PATH for a driver child process: user-space tool dirs prepended to the service's own."""
    base = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    parts = [p for p in (opencode_bin(), user_bin()) if p and p not in base.split(":")]
    # a duplicate between the two prepends would be harmless but noisy in a run's provenance
    seen, prepend = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            prepend.append(p)
    return ":".join(prepend + [base]) if prepend else base
