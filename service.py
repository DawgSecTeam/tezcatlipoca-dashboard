#!/usr/bin/env python3
"""tezcatlipoca-dashboard service entrypoint."""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from dashboard import config  # noqa: E402

if config.engine_env_file():  # TEZ_* for the templates/nodes endpoints
    load_dotenv(config.engine_env_file())

import uvicorn  # noqa: E402

from dashboard.app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host=config.host(), port=config.port())
