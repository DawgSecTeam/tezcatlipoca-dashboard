"""Bearer-token auth for /api and /mcp (tokens configured via TEZ_DASHBOARD_TOKENS)."""

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from dashboard import config

_OPEN_PATHS = ("/api/health",)


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _OPEN_PATHS:
            return await call_next(request)
        tokens = config.tokens()
        supplied = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not tokens:
            return await call_next(request)  # no tokens configured = local dev mode
        if supplied and any(secrets.compare_digest(supplied, t) for t in tokens):
            return await call_next(request)
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
