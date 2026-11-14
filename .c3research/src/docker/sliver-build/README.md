# Sliver server — Ubuntu 18.04 (glibc 2.27) container

Runs the Sliver server inside an Ubuntu 18.04 container so every runtime-
generated `.so` implant links against the container's `libc.so.6@GLIBC_2.27`.
That version is old enough for airship (CentOS Stream 8 / glibc 2.28), polus
(Ubuntu 20.04 / glibc 2.31), and any modern Linux target — the .so IOCs
(`linux_implant_so_bare`, `linux_implant_so_systemd`) stop failing with
`GLIBC_2.32 not found` on those boxes.

## Why not just build the binary and run native

Sliver builds `.so` implants at REQUEST TIME by spawning `go build -buildmode=
c-shared` for each new implant. The cgo runtime that build produces links
against whatever `libc.so.6` sits on the running host — not the one that was
there when `sliver-server` itself was compiled. So the whole server has to
run inside old-glibc, not just get built once.

## Pinning to your Sliver revision

The currently-running Sliver reports `devel` (source build). Before rebuilding,
capture the exact commit so the container matches production 1:1:

```bash
# Whichever git checkout produced /usr/local/bin/sliver-server:
cd <sliver-src-checkout>
SLIVER_SHA=$(git rev-parse HEAD)
echo "$SLIVER_SHA"

# Then build the image with that SHA (or a tag):
podman build --build-arg SLIVER_GIT_REF="$SLIVER_SHA" \
             -t c3r/sliver-server:glibc2.27 \
             docker/sliver-build/
```

## Everyday ops

Build once (~5-10 min):
```bash
podman build -t c3r/sliver-server:glibc2.27 docker/sliver-build/
```

Cut over from the systemd unit to the container:
```bash
bash docker/sliver-build/run.sh
```

The wrapper stops `sliver.service`, removes any stale container of the same
name, then launches with `--net=host --pid=host` so the mTLS listener + operator
gRPC bind on the same host IPs the C3rberus backend already targets — no
`.env` changes, no NAT remap.

State is bind-mounted from `/root/.sliver` (host) to `/root/.sliver` (container):
`implant_builds`, `implant_configs`, per-implant binaries under `builds/`,
operator configs, and logs all persist across container restarts and are
still readable by the backend's `implant_scrub` + `remediate_sweep`.

## Coexisting with the systemd unit

Long-term you likely want a systemd unit that launches the container instead
of the native binary. Minimal drop-in:

```ini
# /etc/systemd/system/sliver-container.service
[Unit]
Description=Sliver (glibc-2.27 container)
Requires=network-online.target
After=network-online.target

[Service]
ExecStart=/root/source/repos/C3rberus/docker/sliver-build/run.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then `systemctl disable --now sliver.service && systemctl enable --now sliver-container.service`.

## Verifying the fix

After first-time build + `run.sh`, generate a fresh linux .so implant via the
C3rberus admin UI (or `POST /admin/deploy-all-iocs` scoped to `linux_implant_so_bare`),
grab the built `.so` from `/root/.sliver/builds/<uuid>/`, and check:

```bash
objdump -T /root/.sliver/builds/<uuid>/*.so | grep GLIBC_ | sort -u
# Expected: everything at @GLIBC_2.27 or older — no 2.32/2.33/2.34.
```

Then deploy to polus and confirm the systemd unit doesn't restart-storm
(previous failure looked like `GLIBC_2.32 not found`, thousands of restarts).
Cross-reference `bulk_exec._effectively_dead` — a stale beacon may still
show alive per Sliver's flag; the fresh build should establish a real
session within one beacon interval.

## Not this container's job

- Not the loader binary (`iocs/payloads/so_loader_linux`). That's built by
  `scripts/build/build_so_loader.sh` on the C3rberus repo host — commit
  `0ecb72e` already pins it via `glibc_compat.h` + a post-link Verneed
  patch, so the loader loads on pre-2.34 glibc regardless of build host.
- Not other implant formats (`.exe`, ELF exe, FreeBSD .so). Windows uses
  mingw-w64 which pins to a stable Windows ABI; FreeBSD goes through the
  zig wrapper which pins to a FreeBSD 15 sysroot. Neither has the host-
  glibc problem. This container just fixes the linux `.so` path.
