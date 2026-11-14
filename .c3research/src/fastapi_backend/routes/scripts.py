"""Script-serving route — serves IOC check/deploy/remediate scripts from the.

iocs/ directory to remote boxes via irm|iex (Windows) or curl|bash (Linux).

Access is gated by HMAC-signed bearer tokens (see core/script_server.py).
Tokens bind to (client_ip, script_path, exp_unix) so a leaked token can only
pull the specific script its box was authorised for, only from that box's IP,
and only until the per-request expiry. Path traversal outside IOC_SCRIPTS_DIR
is rejected with 403.

In addition to the static-script catchall, this module exposes
``GET /scripts/wrapper/<token_id>`` which serves dynamic bodies registered
by ``netexec.script_wrapper.build_inline_combined_command``.  Wrapper
entries are single-use, IP-bound, and TTL-expired.

``POST /scripts/wrapper/<token_id>/result`` (docs/PLAN_script_result_callback.md)
receives a script's check/deploy/remediate result and resolves the pending
``netexec.script_result.ResultRegistry`` future for that token — callers
await this instead of trusting NXC's own output readback, which has no
completion check on the remote process and can read an empty/partial
redirect file as success.
"""

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from fastapi_backend.core.config import config
from fastapi_backend.core.script_server import script_token_manager
from fastapi_backend.netexec.script_wrapper import get_wrapper_cache

router = APIRouter(prefix="/scripts", tags=["scripts"])
logger = logging.getLogger(__name__)


@router.get("/wrapper/{token_id}", response_class=PlainTextResponse)
async def serve_wrapper(
    token_id: str,
    request: Request,
    authorization: str = Header(...),
) -> str:
    """Serve a dynamic wrapper body registered for one-shot fetch.

    1. Validate the HMAC bearer token (binds to client_ip + ``wrapper/<id>`` + exp).
    2. Look up the entry in the wrapper cache.
    3. Compare ``request.client.host`` to ``entry.allowed_client_ip``.
    4. Return body and evict the entry on success.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[len("Bearer "):]
    client_ip = request.client.host if request.client else ""

    ok, reason = script_token_manager.validate(token, client_ip, f"wrapper/{token_id}")
    if not ok:
        logger.warning(
            "/scripts/wrapper/%s: 401 (client=%s reason=%s)",
            token_id, client_ip, reason,
        )
        raise HTTPException(status_code=401, detail=f"Invalid token: {reason}")

    body, fetch_reason = get_wrapper_cache().fetch(token_id, client_ip)
    if body is None:
        if fetch_reason == "ip-mismatch":
            logger.warning(
                "/scripts/wrapper/%s: 403 client_ip mismatch (client=%s)",
                token_id, client_ip,
            )
            raise HTTPException(status_code=403, detail="client ip mismatch")
        # Plan §1.3 specifies 404/401/403 only; expired entries surface as 404
        # since the body is gone, not as 410 Gone (which would imply the
        # endpoint itself was retired).
        if fetch_reason == "expired":
            raise HTTPException(status_code=404, detail="expired")
        raise HTTPException(status_code=404, detail="not found")
    return body


@router.get("/file/{token_id}")
async def serve_file(
    token_id: str,
    request: Request,
    authorization: str = Header(...),
) -> Response:
    """Serve a staged binary file registered for one-shot fetch.

    Same HMAC/IP-binding/single-use semantics as ``serve_wrapper`` but binds the
    token to ``file/<id>`` and returns raw bytes as ``application/octet-stream``.
    Used by the WinRM/SSH file-delivery fallback so the target box pulls the
    payload over HTTP (one round-trip) instead of receiving it base64-chunked
    (docs/PLAN_windows_delivery_fallback.md).
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[len("Bearer "):]
    client_ip = request.client.host if request.client else ""

    ok, reason = script_token_manager.validate(token, client_ip, f"file/{token_id}")
    if not ok:
        logger.warning("/scripts/file/%s: 401 (client=%s reason=%s)", token_id, client_ip, reason)
        raise HTTPException(status_code=401, detail=f"Invalid token: {reason}")

    body, fetch_reason = get_wrapper_cache().fetch(token_id, client_ip)
    if body is None:
        if fetch_reason == "ip-mismatch":
            logger.warning("/scripts/file/%s: 403 client_ip mismatch (client=%s)", token_id, client_ip)
            raise HTTPException(status_code=403, detail="client ip mismatch")
        raise HTTPException(status_code=404, detail=fetch_reason)
    data = body if isinstance(body, bytes) else str(body).encode()
    return Response(content=data, media_type="application/octet-stream")


@router.post("/wrapper/{token_id}/result")
async def post_wrapper_result(
    token_id: str,
    request: Request,
    authorization: str = Header(...),
) -> Response:
    """Receive a script's POSTed check/deploy/remediate result.

    Resolves the pending ``ResultRegistry`` future for ``token_id`` so the
    caller awaiting it (see docs/PLAN_script_result_callback.md) stops
    depending on NXC's own (unreliable, see that plan) output readback.

    Mirrors ``serve_wrapper``'s auth shape, bound to a distinct path scope
    (``wrapper/<token_id>/result``) so a fetch token can't be replayed here
    and vice versa.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[len("Bearer "):]
    client_ip = request.client.host if request.client else ""

    ok, reason = script_token_manager.validate(token, client_ip, f"wrapper/{token_id}/result")
    if not ok:
        logger.warning(
            "/scripts/wrapper/%s/result: 401 (client=%s reason=%s)",
            token_id, client_ip, reason,
        )
        raise HTTPException(status_code=401, detail=f"Invalid token: {reason}")

    body = (await request.body()).decode("utf-8", errors="replace")
    from fastapi_backend.netexec.script_result import get_result_registry  # cycle-break
    get_result_registry().resolve(token_id, body)
    return Response(status_code=204)


@router.get("/{path:path}", response_class=PlainTextResponse)
async def serve_script(
    path: str,
    request: Request,
    authorization: str = Header(...),
) -> str:
    """Serve an IOC script file by its path relative to IOC_SCRIPTS_DIR."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization[len("Bearer "):]
    client_ip = request.client.host if request.client else ""
    # Token signature binds to whatever's in the URL — hash for the
    # obfuscated path, or the real path for legacy callers. Validate first.
    ok, reason = script_token_manager.validate(token, client_ip, path)
    if not ok:
        logger.warning(
            "/scripts/%s: 403 (client=%s reason=%s)",
            path, client_ip, reason,
        )
        raise HTTPException(status_code=403, detail=f"Invalid token: {reason}")

    # Resolve URL → on-disk path. If the URL looks like a 16-hex obfuscated
    # hash, reverse it via the script_token_manager map. Otherwise treat as a
    # real relative path (legacy / direct-debug invocations).
    real_rel: str | None = None
    if len(path) == 16 and all(c in "0123456789abcdef" for c in path):
        real_rel = script_token_manager.lookup(path)
        if real_rel is None:
            logger.warning("/scripts/%s: 404 — obfuscated hash unknown", path)
            raise HTTPException(status_code=404, detail="Script not found")
    else:
        real_rel = path

    scripts_root = config.IOC_SCRIPTS_DIR.resolve()
    target = (scripts_root / real_rel).resolve()

    # Reject path traversal
    if not str(target).startswith(str(scripts_root)):
        logger.warning(f"Path traversal attempt: {path!r}")
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Script not found")

    return await asyncio.to_thread(target.read_text, encoding="utf-8")
