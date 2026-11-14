#!/usr/bin/env bash
# One-time: add the DYNAMIC-link bits (shared libc/libthr + dynamic crt objects)
# to the FreeBSD 15 sysroot so build_so_loader.sh can produce a *dynamically*
# linked so_loader_freebsd (dlopen requires the runtime linker, absent from a
# static binary).
#
# The base static sysroot (build_self_inject_freebsd.sh) only extracts *.a
# archives + static crt objects; this adds the .so + dynamic crt objects.
# Idempotent; safe to re-run. Sysroot dir is gitignored.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

SYSROOT="${1:-scripts/build/fbsd15-sysroot}"
URL="https://download.freebsd.org/ftp/releases/amd64/15.0-RELEASE/base.txz"

mkdir -p "$SYSROOT"

if [[ -f "$SYSROOT/lib/libc.so.7" \
   && -f "$SYSROOT/usr/lib/crtbegin.o" \
   && -f "$SYSROOT/usr/lib/crtendS.o" ]]; then
    echo "[ok] dynamic sysroot bits already present in $SYSROOT"
    exit 0
fi

for tool in curl xz tar; do
    command -v "$tool" &>/dev/null || { echo "[error] $tool not found" >&2; exit 1; }
done

echo "[fetch] streaming dynamic bits from $URL"
# Targeted extract — stream the .txz, decompress, pull only what a dynamic
# freebsd15 link needs. FreeBSD layout preserved so the /usr/lib/libc.so and
# /usr/lib/libthr.so ld-scripts' GROUP() absolute paths resolve under --sysroot.
# Both the non-S crt objects (executable: so_loader) and the S-variant crt
# objects (shared object: ld_preload shim) are needed.
curl -fL "$URL" | xz -d | tar -x -C "$SYSROOT" \
    './lib/libc.so.7' \
    './lib/libthr.so.3' \
    './usr/lib/libc.so' \
    './usr/lib/libthr.so' \
    './usr/lib/libc_nonshared.a' \
    './usr/lib/libssp_nonshared.a' \
    './usr/lib/crt1.o' \
    './usr/lib/Scrt1.o' \
    './usr/lib/crti.o' \
    './usr/lib/crtn.o' \
    './usr/lib/crtbegin.o' \
    './usr/lib/crtend.o' \
    './usr/lib/crtbeginS.o' \
    './usr/lib/crtendS.o' \
    './libexec/ld-elf.so.1'

if [[ -f "$SYSROOT/lib/libc.so.7" \
   && -f "$SYSROOT/usr/lib/crtbegin.o" \
   && -f "$SYSROOT/usr/lib/crtendS.o" ]]; then
    echo "[ok] dynamic sysroot provisioned in $SYSROOT"
else
    echo "[error] extract completed but expected files missing — check base.txz layout" >&2
    exit 1
fi
