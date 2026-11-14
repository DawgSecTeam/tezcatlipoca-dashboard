#!/usr/bin/env bash
# Launch the Sliver builder container. It runs sliver-server in BUILDER
# mode — dials OUT to the host's main sliver-server via mTLS gRPC on
# 127.0.0.1:31337 (--net=host makes the loopback work), registers as
# available for shared:linux/amd64 builds, and stays idle until the main
# server delegates a build request.
#
# The host sliver-server stays running the whole time; this container
# only handles the linux .so build path. Every other implant target
# (windows exe, freebsd .so, linux ELF, native shellcode, etc.) stays on
# the main server's native build path. Zero disruption to running
# implants — none of the main server's crypto state is touched.
set -euo pipefail

IMAGE="${SLIVER_IMAGE:-c3r/sliver-builder:glibc2.27}"
BUILDER_CFG="${SLIVER_BUILDER_CFG:-/root/c3r-glibc-builder.cfg}"
NAME="${SLIVER_CONTAINER:-c3r-sliver-builder}"

if ! sudo test -f "$BUILDER_CFG"; then
    echo "[error] builder cfg not found: $BUILDER_CFG" >&2
    echo "        Generate one on the host:" >&2
    echo "          sudo sliver-server operator --name c3r-glibc-builder \\" >&2
    echo "              --lhost 127.0.0.1 --lport 31337 \\" >&2
    echo "              --permissions builder --save $BUILDER_CFG" >&2
    exit 1
fi

if command -v podman >/dev/null 2>&1; then RT=podman
elif command -v docker >/dev/null 2>&1; then RT=docker
else echo "[error] neither podman nor docker installed" >&2; exit 1
fi

if sudo $RT container inspect "$NAME" >/dev/null 2>&1; then
    sudo $RT rm -f "$NAME"
fi

# --net=host so the builder can dial the host's 127.0.0.1:31337 gRPC.
# Read-only mount of the cfg — nothing inside the container should mutate it.
# No state volume needed: builders are stateless, they compile + return.
exec sudo $RT run --name "$NAME" \
    --net=host \
    -v "$BUILDER_CFG:/etc/sliver/builder.cfg:ro" \
    -e GOMAXPROCS="${GOMAXPROCS:-8}" \
    "$IMAGE"
