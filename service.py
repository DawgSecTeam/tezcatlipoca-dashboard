#!/usr/bin/env python3
"""tezcatlipoca-dashboard service entrypoint."""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from dashboard import config  # noqa: E402
from dashboard.logging_setup import configure as configure_logging  # noqa: E402

if config.engine_env_file():  # TEZ_* for the templates/nodes endpoints
    load_dotenv(config.engine_env_file())

configure_logging()

import uvicorn  # noqa: E402

from dashboard.app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    # A run holds an open log/event stream; without a bounded graceful timeout a service
    # stop waits on that stream, times out, and aborts the interpreter (status=6 in the
    # journal on every restart). Ten seconds finishes in-flight requests and still fits
    # inside systemd's default stop window.
    uvicorn.run(app, host=config.host(), port=config.port(),
                timeout_graceful_shutdown=config.shutdown_grace_seconds())
