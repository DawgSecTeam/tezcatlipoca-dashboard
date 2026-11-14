# Sliver EXTERNAL BUILDER, built on Ubuntu 18.04 (glibc 2.27) so every
# LINUX .so implant it spawns via `go build -buildmode=c-shared` links
# against the container's libc. Covers airship (CentOS Stream 8 / glibc
# 2.28) + polus (Ubuntu 20.04 / glibc 2.31) + every modern target.
#
# Runs sliver-server in BUILDER mode — not server mode. Connects OUT to the
# host's main sliver-server via mTLS gRPC (127.0.0.1:31337, --net=host)
# using an operator config carrying the `builder` permission. The main
# server delegates only the enabled build targets to this builder
# (--enable-target shared:linux/amd64); everything else stays on the main
# server's build path. The main server also owns all crypto state — the
# builder ships back binary bytes only, so pre-existing implants keep
# their signature keys and nothing on the range is disturbed.
#
# This replaces an earlier abandoned design that ran the whole sliver-
# server inside the container as a drop-in for the systemd unit — that
# cutover killed 36 pre-existing implants with "unknown implant signature
# key" errors even with the host's sliver.db bind-mounted. The builder
# sidecar path is non-destructive.

FROM ubuntu:18.04

ARG DEBIAN_FRONTEND=noninteractive
# Match the host Sliver's Go version — same source commit gets the same
# build outputs. Bump both together.
ARG GO_VERSION=1.25.6
# Pin to the exact Sliver release the host runs. Extracted from the host
# binary's Go build info: `go version -m /root/sliver-server` → v1.7.3.
ARG SLIVER_GIT_REF=v1.7.3

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git make build-essential pkg-config \
        gcc-mingw-w64 gcc-mingw-w64-x86-64 g++-mingw-w64-x86-64 \
        libc6-dev libc6-dev-i386 \
        xz-utils tar bash \
    && rm -rf /var/lib/apt/lists/*

# Go toolchain — Ubuntu 18.04's apt has go1.10; pull the official tarball.
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
        -o /tmp/go.tar.gz \
    && tar -C /usr/local -xzf /tmp/go.tar.gz \
    && rm /tmp/go.tar.gz
ENV PATH=/usr/local/go/bin:/root/go/bin:$PATH \
    GOPATH=/root/go \
    GO111MODULE=on

# Sliver build — pinned tag matches the host's running server (v1.7.3).
WORKDIR /src
RUN git clone --depth 1 --branch "${SLIVER_GIT_REF}" https://github.com/BishopFox/sliver.git \
    || (git clone https://github.com/BishopFox/sliver.git \
        && cd sliver && git checkout "${SLIVER_GIT_REF}")

WORKDIR /src/sliver
RUN make linux-amd64 && install -m 0755 sliver-server /usr/local/bin/sliver-server

# Operator config with the builder permission is bind-mounted at runtime
# (see run.sh) so the same image works for any host's cfg. GOMAXPROCS
# matches the host's systemd drop-in.
ENV GOMAXPROCS=8

# Builders dial OUT to the main server — no listening ports to expose.
# --enable-target so:linux/amd64 restricts this builder to producing
# ONLY linux/amd64 SHARED_LIB (.so) implants; every other target stays
# on the main server's native build path. `so` is Sliver's parse alias
# for OutputFormat_SHARED_LIB (also accepts shared-lib / sharedlib /
# dll / dylib; do NOT use `shared_lib` with underscore — Sliver treats
# that as unknown and defaults to EXECUTABLE).
ENTRYPOINT ["/usr/local/bin/sliver-server", "builder", \
            "-c", "/etc/sliver/builder.cfg", \
            "--enable-target", "so:linux/amd64", \
            "--name", "c3r-glibc2.27-builder"]
