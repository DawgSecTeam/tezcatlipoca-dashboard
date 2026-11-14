# Pre-Competition Setup

Checklist for the hour before kick-off. Touch every item — a missed
step here costs you the first cycle's worth of detection.

## When to use

- Day-of competition setup.
- After any environment swap (new VMs, migrated server, refreshed
  inventory).
- After every backend restart on a fresh DB.

## Prerequisites

- C3rberus host: Python 3.14, Node 22+, ruff installed via
  `scripts/setup.sh`.
- Per-team blue infrastructure: pfSense + Windows + Linux VMs
  reachable.
- Sliver server + operator config available.

## Inventory

Edit `config/inventory.yml` for the team count + box layout. See
[Inventory YAML](inventory-yaml) for schema.

```bash
$EDITOR config/inventory.yml
# Or, for a scoped test run:
export INVENTORY_FILE=config/test_inventory.yml
```

Backend restart picks up the new file. Confirm via
`logs/fastapi_backend_<ts>.log`:

```
Loaded N teams with M total boxes
```

## Certs

The backend serves HTTPS; certs live in `certs/`:

- `certs/cert.pem` — server certificate.
- `certs/key.pem` — server private key.

Self-signed for dev; replace with real CA-issued certs for prod
deployments. Frontend dev server (`react/`) also accepts these certs
via Vite's HTTPS config.

## Sliver

```bash
# Verify Sliver server is up:
sliver-server   # interactive; or check the systemd unit on prod

# Generate an operator config (once per operator):
sliver-server   # at the prompt:  new-operator --name op-rob --lhost 192.168.1.10 --save sliver_operator.cfg

# Import for the C3rberus host:
sliver-client import sliver_operator.cfg
ls ~/.sliver-client/configs/   # confirm

# Verify the mTLS listener:
sliver-server  # at the prompt:  jobs
# should list:  protocol: mtls   port: 8888   stage: implant
```

See [Sliver setup](sliver-setup) for the deeper procedure (cert chain,
PIVOT listener port allocation, DEV_MODE switches).

## DC-promotion

`scripts/infra/promote_domain_controllers.py` automates the 4-phase Windows
domain stand-up:

```bash
# All teams, all phases:
python scripts/infra/promote_domain_controllers.py --step all

# Specific team, specific phase:
python scripts/infra/promote_domain_controllers.py --step join --team 1 3

# Dry-run (print commands without executing):
python scripts/infra/promote_domain_controllers.py --step all --dry-run
```

The four phases:

1. `setup` — WinRM + firewall rules on every Windows box.
2. `promote` — AD DS install + DCPROMO on `*-1` boxes (the team DC);
   verifies post-reboot.
3. `network` — static IP + DNS on both `win1` and `win2`; verifies
   networking.
4. `join` — domain-join `win2` to `win1`'s domain; verifies the DC
   sees the computer object.

> **WinRM (5985) must stay reachable.** Phase 1 (`setup`) enables it. Beyond
> provisioning, the runtime **SMB→WinRM→SSH file-delivery fallback**
> (`NXC_ALT_DELIVERY_ENABLED`, default on) uses WinRM to deliver implants/files
> when SMB `put_file` hits NETBIOS timeouts — so keep 5985 open on every Windows
> box. The delivering tier shows on the Task Queue row's `exec_path`
> (`smb`/`winrm`/`ssh`). See `docs/PLAN_windows_delivery_fallback.md`.

## Linux-domain-join

`scripts/infra/join_linux_domain.py` joins a team's Linux boxes
(Ubuntu/Debian or RHEL/CentOS/Fedora) to that same team's domain via
`realmd`/`sssd`, using Samba (`net ads join`) as the membership backend
rather than `realm`/`adcli`'s default — `adcli`'s Kerberos password-set
step fails against a Server 2025 DC. Run after DC-promotion:

```bash
python scripts/infra/join_linux_domain.py --step all
python scripts/infra/join_linux_domain.py --step join --team 1 3
python scripts/infra/join_linux_domain.py --step all --dry-run
```

Phases: `prereqs` (distro-aware package install) → `dns` (point the
Linux box's resolver at the DC — otherwise it only resolves through
pfSense, no route to the AD zone) → `join` (idempotent) → `verify`
(three independent signals: `realm`/`sssd` state on the box, a fresh
SSH auth with domain creds, and the AD computer object on the DC).

Domain name / NetBIOS / DC identification are inventory-configurable
(`domain:`/`netbios:` per-team, `domain_pattern:`/`netbios_pattern:`
global template, `is_domain_controller: true` flag) — full schema and
a from-scratch walkthrough in `docs/DOMAIN_JOIN_GUIDE.md`.

**Still open**: pfSense (FreeBSD) domain/Kerberos join isn't covered by
this script (Linux only) and has no automation yet — not yet
implemented, tracked in the project TODO.

## Networking

Two helpers that read `config/inventory.yml`:

- **`scripts/infra/configure_networking.py`** — sets static IPs on Linux +
  pfSense boxes per inventory.
- **`scripts/infra/set_hostnames.py`** — sets every box's hostname to match
  inventory `name`.

Run order: hostnames → networking → DC promotion. Networking takes
boxes offline briefly when applied; do this before pulling operators
in.

## DC-clock-fix

Lab DCs drift badly — Kerberos fails silently when the DC clock is more
than 5 minutes off. Run this after DC promotion and before any
Kerberos-dependent activity (golden tickets, ccache NXC runs):

```bash
python scripts/infra/fix_dc_clocks.py
# Reads all DCs from inventory.yml, checks NTP offset via NXC,
# corrects any DC drifting > 270s via Set-Date (timezone-aware).
# Exit 0 = all DCs within tolerance.
```

Re-run after a DC reboot. The SA collection loop will emit `clock_skew`
alerts (warn) during competition if a DC re-drifts — see
[Troubleshooting § Kerberos clock skew](troubleshooting#kerberos-clock-skew).

## Scoring-account

The IOC scoring pipeline authenticates with one dedicated account —
`red_scoring` — and nothing else (see
[Running a competition § scoring-preflight](running-competition#scoring-preflight)).
Provision it on every box before kick-off:

```bash
# Declare it once in config/inventory.yml:
#   scoring_account: { username: red_scoring, domain: "", enabled: true }

# Create the account on the boxes (per-team or all):
python scripts/infra/create_red_scoring.py            # all teams
python scripts/infra/create_red_scoring.py --team 1 3 # subset
python scripts/infra/create_red_scoring.py --dry-run  # print, don't apply
```

On the next backend start the account is seeded as one
`Credentials` row per box (`is_infrastructure=true`,
`source='red_scoring'`). Validate auth **and** privilege with the
**VALIDATE SCORING ACCT** button on `/admin` (or
`POST /admin/scoring-cred-preflight`) — every box must report
`pass` before START.

## Access-check

`scripts/diagnostic/check_access.py` is the single sanity check that ties
inventory + DB credentials together:

```bash
python scripts/diagnostic/check_access.py
# Per-box result, colour-coded:
#   ✓ 192.168.1.201 (windows, TEAM1\Administrator) ok
#   ✗ 192.168.1.220 (firewall, admin)              FAIL: connection refused
# Exit 0 if all OK, 1 if any failed.
```

Reads creds from `inventory.yml`, runs NXC SMB (Windows, per-team
domain creds) and SSH (Linux root, pfSense admin), parses NXC
`[+]/[-]/absent` output. Fix every red before kick-off — a missed
cred = a box that scores zero for the entire competition.

## Service-interruption modules

Service-stop modules (`stop_dns`, `disable_web_server`, etc.) are loaded
from `config/services.yml` at backend startup. Create this file from the
example before kick-off:

```bash
cp config/services.example.yml config/services.yml
$EDITOR config/services.yml   # adjust per-environment service names if needed
```

`config/services.yml` is gitignored. If absent, the modules still appear
in the C2 → Modules list but running one against any box returns:
`"No command configured for <service> on os='<os>'."` — a clear loud
error, not a silent no-op. See [Modules § service-interruption](c2-modules#service-interruption-modules)
for the full command list and parameter schema.

## What you'll see

End-to-end check (run from a fresh DB):

1. Restart backend; verify the log carries the inventory count.
2. Run `scripts/diagnostic/check_access.py` — every line green.
3. Click **VALIDATE SCORING ACCT** on `/admin` — every box `pass`.
4. Open `/admin` — every component on the status page shows green:
   `CheckScheduler`, `PlaybookScheduler`, `Sliver client`,
   `DatabaseWriter`, `nmap health`, cred harvester loops.
5. Hit START. The first `CHECK_INTERVAL_MINUTES` cycle fires and
   begins populating `IOCCheckResult`.
6. Open `/c2/overview` — every team box renders, no
   `box_unreachable` alerts.

## Troubleshooting

- **`check_access.py` red on every Windows box** — domain join didn't
  land. Re-run `promote_domain_controllers.py --step join`.
- **`scripts/infra/configure_networking.py` hangs on pfSense** — pfSense
  webGUI auth changed; the script reads the legacy admin cred from
  inventory.
- **Backend log: `Loaded 0 teams with 0 total boxes`** — wrong
  `INVENTORY_FILE` env var or empty YAML. Tail the log for the
  resolved path.
- **Sliver `jobs` shows no mTLS listener** — backend startup
  creates one if missing; check `logs/fastapi_backend_<ts>.log` for
  `Created Sliver mTLS listener on port ...`.
- **DEV_MODE block on Console** — `.env` carries `DEV_MODE=True`;
  flip to `False` and restart.
- **A box is wedged / an in-guest reboot did nothing** — hard
  power-cycle it via the Proxmox API, see
  [Troubleshooting § stuck or unresponsive VM](troubleshooting#stuck-or-unresponsive-vm).
- **Need the box's own side of a failure** — pull its OS/service logs
  post-hoc, see
  [Troubleshooting § pulling box logs](troubleshooting#pulling-box-logs-after-a-failure).

<!-- audited 2026-06-13 — added DC-clock-fix section (62a4094). scripts/infra/promote_domain_controllers.py phase names + scripts/diagnostic/check_access.py output format verified against CLAUDE.md § Pre-Competition Setup Scripts. Updated 2026-06-26 — added Scoring-account section (scripts/infra/create_red_scoring.py, inventory scoring_account block) + VALIDATE SCORING ACCT kick-off step. Updated 2026-07-30 — CLAUDE.md § Pre-Competition Setup Scripts folded into the wiki; this page + inventory-yaml.md already covered promote_domain_controllers/configure_networking/set_hostnames/check_access and the cred + wan_ip fields, so only the two diagnostics (pull_box_logs, Proxmox reset) were new — they live in troubleshooting.md, linked below. -->
