"""Service logging: one timestamped stream (and optional rotating file) for the dashboard.

The service is driven by background threads (run workers, the scoring poll, MCP event pumps)
whose exceptions never reach uvicorn's access log. Before this module those paths either
`pass`ed or stored a run's `error` with nothing written anywhere an operator could read, so
a "failed" run on the board had no explanation in the journal. `configure()` installs the
handler once; modules log under `dashboard.*`.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_configured = False


def configure():
    """Install the dashboard logger once. Safe to call from service.py and tests."""
    global _configured
    logger = logging.getLogger("dashboard")
    if _configured:
        return logger
    raw = (os.environ.get("TEZ_DASHBOARD_LOG_LEVEL") or "INFO").upper()
    level = logging.getLevelName(raw)
    if not isinstance(level, int):
        level = logging.INFO
    logger.setLevel(level)
    # Do not propagate: FastAPI/MCP install a plain root stream handler at app construction,
    # and a propagating dashboard record is then printed twice (once formatted, once bare).
    logger.propagate = False
    fmt = logging.Formatter(_FORMAT)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    path = os.environ.get("TEZ_DASHBOARD_LOG_FILE")
    if path:
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            file_handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3,
                                               encoding="utf-8")
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except OSError as exc:  # logging must never stop the service from starting
            logger.warning("cannot open log file %s: %s", path, exc)
    _configured = True
    logger.debug("dashboard logging configured (level=%s)", logging.getLevelName(level))
    return logger


def get_logger(name):
    return logging.getLogger(name)
