# Inventory YAML

`config/inventory.yml` is the single source of truth for box list,
credentials, and team layout. Loaded once at backend start by
`InventoryManager`, then synced into the `Boxes` DB table.

## When to use

- Adding a new team / box pre-competition.
- Swapping a Linux flavour (Ubuntu → Rocky).
- Updating default credentials after a vault rotation.
- Switching between full-comp inventory and test-scoped inventories
  (`INVENTORY_FILE=config/test_inventory.yml`).

## Prerequisites

- Write access to `config/inventory.yml`.
- Backend restart after every edit
  (`InventoryManager.load_from_config()` runs once at boot).

## Box-fields

Each box is a YAML mapping; all top-level keys:

| Key | Type | Default | Notes |
|---|---|---|---|
| `ip` | str | required | Routable IPv4 address C3rberus connects **to** (LAN address for a firewall). Stays the box's identity everywhere. |
| `name` | str | required | Human-readable identifier; lowercased ext. For domain-joined boxes this **must** match the box's real AD computer hostname — ticket validation (`POST /c2/tickets/{id}/validate`) builds the Kerberos SPN `host/<name>.<realm>` from it, and a mismatch fails GSSAPI on Linux and `getST` on the fallback path. |
| `os` | str | required | `windows`, `linux`, `firewall`. |
| `wan_ip` | str | unset | Firewall only. The WAN address the box appears **as** when it dials out (script fetch, C2 call-home) **and** the address C3rberus dials it **at** when its LAN mgmt IP is unreachable. Used for: NXC dial (`dial_host`), token binding, Sliver correlation, and every standalone script that reaches a firewall. Identity (DB key, metrics, pivot LAN end) stays `ip`. See [Firewall WAN and LAN](#firewall-wan-and-lan). |
| `username` | str | `"admin"` | Local account. `.\Administrator` form on Windows. |
| `password` | str | `"password"` | Local account password. |
| `port` | int | OS-derived | 5985 for Windows (WinRM), 22 otherwise. |
| `connection` | str | OS-derived | `"winrm"` on Windows, otherwise unset. |
| `scoring_username` | str | unset | Per-box override for the `red_scoring` account username (see [Scoring account](#scoring-account)). |
| `scoring_password` | str | unset | Per-box override for the `red_scoring` account password. Use when a box's scoring password differs from its admin password. |

The minimum for a Windows box:

```yaml
- { ip: 192.168.1.201, name: c3-win25-1-1, os: windows,
    username: .\Administrator, password: "Password1!",
    domain_username: "TEAM1\\Administrator",
    domain_password: "Password1!", port: 5985 }
```

## Cred-fields

Windows boxes carry **two** credential pairs — local AND domain. The
local pair is used for WinRM before domain join; the domain pair takes
over post-join.

- `username` / `password` — local account.
- `domain_username` / `domain_password` — domain account. Windows uses the
  NetBIOS form `DOMAIN\User` (e.g. `TEAM1\Administrator`, `MIRA-01\blackteam`).
  **Linux** uses the UPN / SSSD login-format `user@realm` (e.g.
  `blackteam@mira-01.corp.sus`) — SSH carries it verbatim, no `-d` flag.

A **domain-joined Linux** box (`is_domain_joined: true` + `domain_username`)
seeds its operator default cred from the domain UPN, so operator SSH sessions
authenticate as the domain user. Set both keys; without `is_domain_joined:
true` the domain fields are ignored and the box stays local. The IOC/scoring
pipeline (`red_scoring`) still authenticates locally on Linux regardless.
pfSense/firewall boxes are standalone — local creds only.

Both pairs are seeded as `Credentials` rows with
`is_default=true` so they sort first in the routing helper
(`utils/nxc_routing.py:_get_best_cred`).

## Domain-flags

`InventoryManager` derives three defaults per box:

| Field | Default | Override |
|---|---|---|
| `is_domain_joined` | `True` for `os: windows`; `False` else | per-box `is_domain_joined: true/false` |
| `is_domain_controller` | `True` when `is_domain_joined` AND `name` ends in `-1` | per-box `is_domain_controller: true/false` |
| `domain` | `team<N>.c3` when joined, else `None` | per-box `domain: foo.local` |

The `-1`-suffix DC rule matches
`scripts/infra/promote_domain_controllers.py` which promotes
`c3-win25-<team>-1` as the per-team DC. The Cred Harvester's golden
ticket loop only runs against teams with at least one
`is_domain_controller=true` box.

## Firewall WAN and LAN

A firewall (pfSense) has an asymmetry a normal box does not: C3rberus reaches
it **inbound** on its LAN address, but the firewall's own implant/pivot-gateway
reaches C3rberus **outbound** over its WAN — so the source IP the server sees
from that firewall differs from the address the server dials it on.

- `ip` — the **LAN** address. **Identity** everywhere: the DB key, NXC
  metrics/semaphore/failure-tracking keys, the pfSense end of pivot sessions
  (portfwd/SOCKS bind, `resolve_nxc_target`), Sliver correlation key, scoreboard.
- `wan_ip` — the **WAN** address the firewall appears as when it dials out, and
  the address C3rberus dials it **at** when the LAN mgmt IP is unreachable from
  the host (the live_fire case: pfSense LAN `:22/:443` closed, WAN `:22` open).

Four flows use `wan_ip` — everything else keys on `ip`:

1. **NXC dial (connection target).** `script_wrapper.dial_host(box_ip)` returns
   `wan_ip or box_ip`, applied at the backend NXC choke (`netexec/wrapper.py`), so
   every IOC deploy/check/remediate, SA, cred check, put_file, and scp to a
   firewall connects to its WAN. `host` for metrics/parse stays `ip`. Seeded into
   the backend's process-global `_WAN_IP_BY_BOX` map at inventory load
   (`orchestrator._load_inventory`). Design: `docs/PLAN_firewall_wan_dial.md`.
2. **Script-wrapper token binding.** The HMAC bearer token for `/scripts/wrapper/<id>`
   binds to the fetching client's source IP. A firewall fetches from its WAN, so
   the token must bind to `wan_ip` — bind it to the LAN `ip` and the fetch 401s,
   surfacing as "No output from nxc" on every firewall IOC.
3. **Sliver session correlation.** A firewall session's `RemoteAddress` is the
   WAN IP; `sliver_manager._box_for_address` matches `ip OR wan_ip` so it
   correlates back to the LAN-keyed box for auto-register + pivot setup.
4. **Standalone scripts** that dial a firewall — `create_red_scoring.py`,
   `scrub_target_boxes.py`, `clean_stale_implants.sh`, and the diagnostics
   (`remediate_persistent_iocs.py`, `gateway_watchdog.py`, `pull_box_logs.py`,
   `check_access.py`, `check_cpu_usage.py`, `reboot_and_wait_vms.py`). These run
   without the orchestrator, so they read `wan_ip or ip` directly (or, for the
   wrapper-based `remediate_persistent_iocs.py`, populate `_WAN_IP_BY_BOX`
   themselves). Design: `docs/PLAN_firewall_wan_dial_standalone.md`.

Absent → single-IP back-compat (the box dials/binds/correlates on `ip` as before).
Design: `docs/PLAN_firewall_wan_lan.md`.

```yaml
- { ip: 192.168.111.254, name: hades, os: firewall,
    username: admin, password: "pfsense", wan_ip: 10.67.75.1 }
```

## Scoring account

`red_scoring` is the dedicated, operator-hidden credential the IOC pipeline
(check / deploy / remediate) authenticates with — **and nothing else** (see
[Pre-competition § scoring account](pre-competition) and
[Running a competition § scoring-preflight](running-competition#scoring-preflight)).
It is provisioned on the boxes by `scripts/infra/create_red_scoring.py` and
seeded as one `is_infrastructure=true` `Credentials` row per box on startup.

A top-level `scoring_account:` stanza (sibling of `teams:`) declares it:

```yaml
scoring_account:
  username: red_scoring   # account name to create/authenticate
  domain: ""              # NetBIOS prefix for domain auth on domain-joined Windows; "" = local
  enabled: true           # per-inventory gate (also gated by RED_SCORING_ENABLED in .env)
```

The **password is never stored in inventory**. Its resolution follows three
tiers (username follows the same order):

1. **Per-box override** — a box's `scoring_username` / `scoring_password`
   fields (above). Use when a box's scoring-account password differs from its
   admin password.
2. **Global default** — `RED_SCORING_USERNAME` / `RED_SCORING_PASSWORD` in
   `.env` (username also from the `scoring_account.username` stanza).
3. **Legacy fallback** (password only) — when 1 and 2 are both unset,
   red_scoring uses the box's own admin password (`domain_password` on
   domain-joined Windows, else `password`) — matching what
   `create_red_scoring.py` sets by default.

`create_red_scoring.py` reads the same three-tier precedence, so re-provisioning
never resets an explicit override back to the admin password. `RED_SCORING_ENABLED=false`
disables the whole subsystem (IOC paths fall back to legacy operator-cred selection).

> **Not to be confused with red_ops** — the `red_scoring` account is
> operator-*hidden* and IOC-pipeline-*only*. For shared red-team *operator*
> credentials, see [Red-team operator accounts](#red-team-operator-accounts).

## Red-team operator accounts

A top-level `red_ops_accounts:` stanza (a **list**, sibling of `teams:`)
declares shared red-team **operator** credentials — the "team-color" accounts
operators use for hands-on-keyboard red-team actions. They are seeded
`is_infrastructure=false` (so they appear in `GET /c2/creds` and are usable by
every operator + C3rberus red-team path) and `is_default=false` (so the per-box
baseline operator cred still wins the auto cred-pick). They are **distinct from
`red_scoring`**: the IOC check/deploy/remediate pipeline never authenticates
with them.

```yaml
red_ops_accounts:
  - { username: redteam, password: TOcpACdpCPAt }
  - { username: Red,     password: Red123! }
  - { username: Blue,    password: Blue123! }
  # …one entry per shared account
```

Each entry is seeded on **every box**, once per identity flavor:

| Box kind | Rows seeded (username, `domain` column) |
|---|---|
| Windows workgroup | `.\<user>` (local) |
| Windows domain member | `.\<user>` (local) **+** `NETBIOS\<user>` (domain) |
| Windows domain controller | `NETBIOS\<user>` (domain only — a DC has no local SAM) |
| Linux non-domain | `<user>` (local) |
| Linux domain member | `<user>` (local) **+** `<user>@<realm>` (UPN, SSSD) |
| firewall | `<user>` (local) |

`NETBIOS` is taken from the box's `domain_username` prefix (e.g. `MIRA-01`);
`<realm>` is the box's resolved `domain:` (e.g. `mira-01.corp.sus`). The Windows
local row carries the `.\` prefix so plain NXC does local-SAM auth.

**Per-entry keys:**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `username` | str | — | Required. Entries without it are logged and skipped. |
| `password` | str | `""` | Stored **plaintext** in inventory, same as the per-box `blueteam`/`blackteam` creds. |
| `domain` | bool | `true` | `false` → seed only the local row(s), no domain/UPN row. |
| `enabled` | bool | `true` | `false` → skip this entry entirely. |
| `foreign_domain` | str | `""` | Cross-forest FQDN override (see below). |

**Cross-forest override (`foreign_domain:`).** For an identity that lives in a
**different forest** and reaches team boxes through an established forest
trust (e.g. a rogue-DC user `mira.corp.sus\svc_dfsr` when
`mira-XX.corp.sus TRUSTS mira.corp.sus`), set `foreign_domain:` to the
identity's FQDN. When set, the local/NETBIOS matrix above is bypassed and a
single row is seeded **only on Windows DCs**:

| Box kind | Rows seeded when `foreign_domain: mira.corp.sus` |
|---|---|
| Windows DC | `<user>` bare + `domain=mira.corp.sus` — NXC dials `-u <user> -d mira.corp.sus` |
| Windows member / workgroup | none (auth via trust works but confers no privilege) |
| Linux / firewall | none (outside the AD trust boundary) |

```yaml
red_ops_accounts:
  - { username: redteam,  password: TOcpACdpCPAt }
  - { username: svc_dfsr, password: 'P@ssw0rd-DFSR-2026!',
      foreign_domain: mira.corp.sus }
```

**Behavior notes:**

- **Provisioning is out of scope of this stanza.** The accounts must already
  exist on the boxes (local SAM + AD); the stanza only tells C3rberus which
  creds to authenticate with. (No `create_red_ops.py` today — analogous to
  `create_red_scoring.py` if one is added later.)
- **Idempotent + self-pruning.** Re-seed / `reload_inventory` / edits re-sync
  passwords in place; removing an account from the list deletes exactly its
  seeded rows on the next load.
- **Excluded from the proactive credential-revalidation loop.** With one entry
  per box across every team, bare-authing all red_ops rows on a loop would
  flood SMB/SSH auth and trip the NETBIOS auth rate-limiter on any invalid
  account, so their `valid` flag stays untested (`null`) until an operator path
  uses them. (See `docs/PLAN_red_ops_accounts.md` for the full rationale.)

## C2 settings

An optional top-level `c2_settings:` stanza (sibling of `teams:`) lets the
inventory carry the **C2 network identity** — the three values that used to be
toggled in `.env` every time you switched between the dev and live-fire
inventories:

```yaml
c2_settings:
  script_server_url: https://10.50.50.5:4000   # → config.SCRIPT_SERVER_URL
  sliver_host: 10.50.50.5                       # → config.SLIVER_HOST
  c2_ip: 10.50.50.5                             # → config.C2_IP (implant call-home)
```

`InventoryManager.apply_c2_settings()` pushes these onto the live `config`
singleton during inventory load (before `resolve_c2_ip()` /
`refresh_script_server_url()` run), so the value the inventory names is what
implants bake in and what target boxes fetch scripts from. Because a single
inventory file now carries these, **switching inventories no longer requires
editing `.env`** — `config/inventory.yml` names `192.168.1.161`, and both
`config/live_fire_inventory.yml` and `config/testing_inventory.yml` name
`10.50.50.5`.

**Precedence — explicit env wins.** If any of `SCRIPT_SERVER_URL`,
`SLIVER_HOST`, or `C2_IP` is set in the process environment (including a line
still active in `.env`, which `load_dotenv` copies into the environment), that
value is kept and the inventory's is skipped for that key. So:

- To let the inventory drive a key, **leave it out of `.env`** (comment it out).
- To pin a key regardless of inventory, set it explicitly in `.env`.
- The test harness (`run_tests.sh`) exports its own dev-VM-reachable `C2_IP`;
  that export still wins, unchanged.

Any key omitted from (or empty in) `c2_settings:` falls through to the normal
`.env` / auto-derive path. Omit the whole stanza and behaviour is exactly as
before.

## Team-fields

Each team carries:

- `name` — display label (`"Blue Team 1"`).
- `team_num` — integer PK; **also drives derived state** like
  per-team pivot port (`PIVOT_LISTENER_PORT_BASE + team_num`).
- `boxes` — list of box mappings.

Team numbers do **not** have to be consecutive — gaps are fine.

## What you'll see

After a backend restart with a new `inventory.yml`:

- `logs/fastapi_backend_<ts>.log` carries `Loaded N teams with M
  total boxes` from `_parse_config()`.
- `DatabaseInitializer.initialize_teams()` / `initialize_accounts()`
  add new team rows + per-team accounts. Stale rows (team numbers no
  longer in inventory) are pruned.
- `Boxes` table reflects new rows; `Credentials` rows seeded with
  `is_default=true`.

## Troubleshooting

- **Backend doesn't see the new box** — file path resolution:
  - Env var `INVENTORY_FILE=<path>` takes priority.
  - Default fallback: `./config/inventory.yml`.
  - Missing → `_create_default_inventory()` (no-op).
- **Domain join unexpectedly skipped** — `is_domain_joined` derived
  from `os=='windows'` only. Linux + firewall need explicit
  `is_domain_joined: true` to participate in the golden-ticket
  loop.
- **Sliver implants connect to wrong port** — implant carries the
  C2 URL at compile; if you changed `PIVOT_LISTENER_PORT_BASE` after
  generating an implant, regenerate it via [Generation](c2-generation).
- **YAML parse error** — `yaml.safe_load` raises at start; check
  for tabs, mixed indentation, unescaped backslashes in
  `TEAM1\Administrator` strings (must be `TEAM1\\Administrator` in
  YAML).

<!-- audited 2026-06-05 — Box dataclass fields + defaults verified against fastapi_backend/core/inventory_manager.py:11-46; default_domain_joined rule verified at line 113. -->
