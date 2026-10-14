"""Bearer-token auth for the data APIs (tokens configured via TEZ_DASHBOARD_TOKENS).

Only /api and /mcp require a token: the SPA shell and its assets are public so the app can
load and ask for one — its data calls are the protected surface."""

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from dashboard import config


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/api/health" or not (path.startswith("/api") or path.startswith("/mcp")):
            return await call_next(request)
        tokens = config.tokens()
        supplied = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not tokens:
            return await call_next(request)  # no tokens configured = local dev mode
        if supplied and any(secrets.compare_digest(supplied, t) for t in tokens):
            return await call_next(request)
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
