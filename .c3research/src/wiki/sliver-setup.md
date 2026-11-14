# Sliver setup

Server install, operator-cert lifecycle, listener allocation. Read
once when standing up a new C3rberus host; reference periodically when
debugging a stuck Sliver pipeline.

## When to use

- First-time install of the Sliver server on the C3rberus host.
- Adding a new operator account.
- Diagnosing "implants compile but no session ever appears".
- Migrating to a new server host (cert rotation).

## Prerequisites

- C3rberus host with sudo.
- Networking such that targets can reach the host on
  `MTLS_LISTENER_PORT` (default 8888) — direct, or through a per-team
  pivot (see [#pivot-ports](#pivot-ports)).

## Install

```bash
# Latest release; pinned in CLAUDE.md and ARCHITECTURE.md context:
curl https://sliver.sh/install | sudo bash
# Provides:
#   /usr/local/bin/sliver-server   (runtime)
#   /usr/local/bin/sliver-client   (operator CLI)
```

Run once interactively to seed the server data dir:

```bash
sudo sliver-server
sliver > exit
ls /root/.sliver/    # confirm builds, configs, etc.
```

Optional but recommended: systemd unit `/etc/systemd/system/sliver.service`
so the server lives independently of the operator session.

## Operator-cert

Each red-team operator needs their own client config + cert:

```bash
sudo sliver-server
sliver > new-operator --name op-rob --lhost 192.168.1.10 --save /tmp/op-rob.cfg
sliver > exit

# Copy the cfg to wherever the operator runs sliver-client:
scp /tmp/op-rob.cfg <operator-host>:~/

# On the operator host (this includes the C3rberus host itself
# because the backend shells out to sliver-client for the console):
sliver-client import ~/op-rob.cfg
ls ~/.sliver-client/configs/
```

The C3rberus backend reads `SLIVER_CFG_PATH` from `core/config.py`
(default `"./sliver_operator.cfg"`); the `sliver-client` subprocess
loads from `~/.sliver-client/configs/` directly.

## Listener-verification

The backend verifies / creates the mTLS listener on every startup:

```python
# fastapi_backend/lifespan_helpers.py (approximate)
jobs = await sliver_client.jobs()
if not any(j.Protocol == "mtls" and j.Port == MTLS_LISTENER_PORT for j in jobs):
    await sliver_client.start_mtls_listener(port=MTLS_LISTENER_PORT)
```

Verify by hand:

```bash
sliver-client                  # interactive
sliver > jobs
# id   protocol   port   stage
# 1    mtls       8888   implant
```

Implants compiled against this server must dial
`mtls://<host>:<MTLS_LISTENER_PORT>`. Soak `sliver_c2_url` (in
`tests/perf/soak_backend.yml`) must match.

## Pivot-ports

Each team's pfSense pivot binds a unique TCP port on `127.0.0.1` to
avoid collisions. Allocation rule:

```python
effective_pivot_port = body.tcp_pivot_port or (PIVOT_LISTENER_PORT_BASE + team_num)
# Default PIVOT_LISTENER_PORT_BASE = 9900
# Team 1 → 9901, team 2 → 9902, ..., team 16 → 9916.
```

`POST /c2/implants/generate` writes `effective_pivot_port` to the
`ImplantBuilds` row. `_do_setup_pivot()` reads `row.tcp_pivot_port`;
if the row carries `NULL` (legacy), it falls back to the same
formula using the firewall box's `team_num`.

Per-team pivot ports also drive the network-map "purple line" in
[Overview](c2-overview#pivot-highlight).

## Config keys (env)

| Key | Default | Notes |
|---|---|---|
| `SLIVER_ENABLED` | `False` | Master toggle. |
| `SLIVER_BIN` | `"sliver-server"` | Server binary path. |
| `SLIVER_CLIENT_BIN` | `"sliver-client"` | Operator client binary. |
| `SLIVER_CFG_PATH` | `"./sliver_operator.cfg"` | Backend-side cfg path. |
| `SLIVER_HOST` | `"localhost"` | Operator gRPC host (also the C2_IP fallback for implant call-home). |
| `SLIVER_PORT` | `31337` | Operator gRPC port. |
| `C2_IP` | auto-derived | **Implant call-home address** baked into every implant's `c2_url` (`mtls://<C2_IP>:<MTLS_LISTENER_PORT>`). Takes precedence over `SLIVER_HOST`. Must be the address the **target boxes route back to this host on** — see the callout below. |
| `MTLS_LISTENER_PORT` | `8888` | Implant dial port. |
| `PIVOT_LISTENER_PORT` | `9898` | Legacy fallback. |
| `PIVOT_LISTENER_PORT_BASE` | `9900` | Per-team allocation base. |

> **C2_IP must be the interface the target boxes reach this host on.** When
> `C2_IP` is unset, the backend auto-derives it from the host's *internet
> default route* (`socket.connect(("8.8.8.8",80))`) and logs a WARN. On a
> **multi-homed host** that is usually the wrong interface: e.g. a competition
> host on a management LAN (`192.168.1.161`, its default route) reachable by the
> target boxes only over a VPN/second NIC (`10.50.50.5`) — implants baked with
> the LAN IP dial an address they can never reach and **never check in**, while
> everything else (NXC, the host-side of pivots) still works. **Set `C2_IP`
> explicitly** to the VPN/reachable address (and keep `SLIVER_HOST` +
> `SCRIPT_SERVER_URL` on the same interface). The pytest gate targets a
> *different* network (dev VMs); `run_tests.sh` overrides `C2_IP` with the host
> default-route IP for tests, so the operator `.env` value can stay set to the
> live/competition address.

## What you'll see

When everything's wired:

- `sliver-server` running (or systemd service active).
- Backend log: `Sliver client connected to ... gRPC port 31337`.
- Backend log: `Sliver mTLS listener present on port 8888`.
- `/admin/status` → `components.sliver` is green with the listener
  job count.
- Implants generated via [Generation](c2-generation) compile in <30s
  and check in within `CHECK_INTERVAL_MINUTES` of deploy.

## Troubleshooting

- **"sliver-client not found"** — `SLIVER_CLIENT_BIN` is unset and
  `sliver-client` not on `PATH`. Either install per `Install` above
  or set the env var.
- **`jobs` shows no mTLS listener** — verify
  `MTLS_LISTENER_PORT` matches what the backend tried to create
  (`logs/fastapi_backend_<ts>.log`).
- **Implant compiles + deploys but never checks in** — most common:
  implant baked with a `c2_url` the target box can't reach. Check
  `ImplantBuilds.c2_url` in DB (or `/c2/server-info` → `c2_ip`); it should be
  `mtls://<reachable-host>:<MTLS_LISTENER_PORT>` where `<reachable-host>` is the
  interface **that box** routes back to this host on. On a multi-homed host the
  auto-derived `C2_IP` is usually the default-route LAN IP, not the VPN — set
  `C2_IP` explicitly (see the callout above the config table). Tell-tale sign:
  the box deploys fine (NXC works) but no session/beacon ever appears and
  `Boxes.session_id`/`beacon_id` stay null. Quick check from the box:
  `nc -vz <C2_IP> <MTLS_LISTENER_PORT>` (or `bash -c 'echo > /dev/tcp/<C2_IP>/<port>'`).
- **Pivot port collision** — two implants compiled with the same
  `tcp_pivot_port`. Regenerate one with the auto-derived port.
- **`DEV_MODE=True` blocks Console** — see
  [Console § dev-mode](c2-console#dev-mode).

<!-- audited 2026-06-05 — env-key defaults verified against fastapi_backend/core/config.py; pivot port allocation rule per wiki/subsystems.md § Per-team pivot port allocation. -->
