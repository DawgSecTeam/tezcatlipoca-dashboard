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
