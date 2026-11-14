"""HMACTokenManager — stateless HMAC-signed bearer tokens for /scripts.

The previous rotation-based design produced cross-backend 403s when multiple
backend processes shared the SCRIPT_SERVER_URL (e.g. operator workflow on
port 3000, test backend on port 3001) — each process had its own in-memory
rotation state, so a token signed by backend A would 403 when validated by
backend B. The HMAC design replaces in-memory rotation state with a shared
secret (`SCRIPT_HMAC_SECRET` in `.env`), so any backend can validate any
token computed from the same secret.

Token format on the wire:
    Authorization: Bearer {b64_hmac}.{exp_unix}

`b64_hmac` is the urlsafe-base64 of HMAC_SHA256(secret, msg). `msg` is:
    f"{box_ip}|{path}|{exp_unix}"

box_ip binds the token to the box that's allowed to use it (server reads
`request.client.host` and refuses tokens from a different IP). path binds
it to the specific script. exp_unix bounds the replay window. Constant-time
compare on the recomputed HMAC prevents leak via timing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

logger = logging.getLogger(__name__)


class HMACTokenManager:
    """Stateless HMAC-signed bearer tokens for the /scripts endpoint.

    Path obfuscation: real script paths (which reveal IOC names, e.g.
    `check_scripts/windows/persistence/svc_privuser.ps1`) are replaced on the
    wire by a 16-hex HMAC hash (`HMAC_SHA256(secret, real_path)[:16]`). The
    server maintains an in-memory reverse map `hash → real_path` populated at
    first lookup by scanning IOC_SCRIPTS_DIR. Wrappers carry the obfuscated
    URL only; blue teams observing wire traffic / log lines see opaque hashes
    instead of IOC names. Token signature binds to the HASH (not the real
    path), so the validation path doesn't need to deobfuscate before the
    crypto check.
    """

    def __init__(self, secret: str | None = None) -> None:
        # Read from config on first use; allow override for tests.
        self._explicit_secret = secret
        self._hash_to_path: dict[str, str] | None = None  # lazy-loaded

    @property
    def _secret(self) -> bytes:
        if self._explicit_secret is not None:
            return self._explicit_secret.encode()
        # Late import to avoid module-import-time circular dep on config
        from fastapi_backend.core.config import config  # cycle-break
        return config.SCRIPT_HMAC_SECRET.encode()

    @staticmethod
    def _msg(box_ip: str, path: str, exp_unix: int) -> bytes:
        return f"{box_ip}|{path}|{exp_unix}".encode()

    def path_hash(self, real_path: str) -> str:
        """Compute the 16-hex obfuscated hash for a real script path.

        Hash is deterministic given the same secret. Stable across restarts
        with the same SCRIPT_HMAC_SECRET. Wrappers stamp this hash into the
        /scripts/{hash} URL; the server reverses via _hash_to_path map.
        """
        return hmac.new(self._secret, real_path.encode(), hashlib.sha256).hexdigest()[:16]

    def _ensure_path_map(self) -> dict[str, str]:
        """Lazy-build the hash → real_path map by scanning IOC_SCRIPTS_DIR."""
        if self._hash_to_path is not None:
            return self._hash_to_path
        from fastapi_backend.core.config import config  # cycle-break
        root = config.IOC_SCRIPTS_DIR.resolve()
        m: dict[str, str] = {}
        for f in root.rglob("*"):
            if f.is_file():
                rel = f.relative_to(root).as_posix()
                m[self.path_hash(rel)] = rel
        self._hash_to_path = m
        logger.info("HMACTokenManager: loaded %d obfuscated script paths", len(m))
        return m

    def lookup(self, hash_str: str) -> str | None:
        """Reverse-lookup a real path from its obfuscated hash."""
        return self._ensure_path_map().get(hash_str)

    def sign(self, box_ip: str, path: str, ttl_seconds: int = 3600) -> str:
        """Generate a token for (box_ip, path) valid for `ttl_seconds`.

        Returns the full `Authorization: Bearer` value (without the
        "Bearer " prefix). Callers stamp this directly into the wrapper's
        Authorization header.
        """
        exp_unix = int(time.time()) + int(ttl_seconds)
        mac = hmac.new(self._secret, self._msg(box_ip, path, exp_unix), hashlib.sha256).digest()
        b64 = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
        return f"{b64}.{exp_unix}"

    def validate(self, token: str, client_ip: str, path: str) -> tuple[bool, str]:
        """Recompute HMAC from (client_ip, path, exp) and verify the token.

        Returns (ok, reason). On failure, `reason` is a short tag suitable
        for the 403 log line (token age, IP mismatch, signature mismatch).
        Never echoes the token bytes themselves.
        """
        if "." not in token:
            return False, "malformed"
        b64_mac, _, exp_str = token.rpartition(".")
        try:
            exp_unix = int(exp_str)
        except ValueError:
            return False, "malformed-exp"
        now = int(time.time())
        if now >= exp_unix:
            return False, f"expired-{now - exp_unix}s"
        try:
            # Re-pad base64 if needed
            padded = b64_mac + "=" * (-len(b64_mac) % 4)
            mac_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        except (ValueError, base64.binascii.Error):
            return False, "malformed-b64"
        expected = hmac.new(self._secret, self._msg(client_ip, path, exp_unix), hashlib.sha256).digest()
        if not hmac.compare_digest(mac_bytes, expected):
            return False, "signature-mismatch"
        return True, "ok"


# Module-level singleton — imported by routes/scripts.py, check_runner.py, deploy_manager.py.
script_token_manager = HMACTokenManager()
