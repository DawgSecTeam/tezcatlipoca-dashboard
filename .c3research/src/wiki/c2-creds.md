# Credentials

Operator-facing credential store. Three tabs: passwords, NT hashes,
Kerberos tickets. Backed by the `Credentials` table; revalidated
automatically every cycle.

## When to use

- Register a freshly captured credential (pwdump, Mimikatz, manual
  capture).
- Force a hash dump on a domain-joined box.
- Forge / refresh / revoke a Kerberos golden ticket.
- Audit which boxes you have working creds for.

For IOC-auto-registered creds (from `IOC.added_credentials`), no
action required — they appear here automatically with the
**VIA `<ioc>`** badge.

## Prerequisites

- Logged in (any non-blue role).
- For DUMP_FROM_BOX: target box is `is_domain_joined=true` Windows
  with a known-good admin cred.
- For FORGE_TICKET: a captured `krbtgt` NT hash for the team's
  domain (either from a prior DCSync or manual capture).

## Passwords

Tab `data-testid="creds-tab-passwords"`. Lists every `Credentials` row
with `cred_type='password'`.

Add a new password via **ADD_CREDENTIAL**
(`data-testid="add-cred-btn"`):

- Cred type (`data-testid="cred-type-select"`) — pick `password`.
- Username, password, box / team, optional domain.
- Saved as unvalidated; the cred-revalidation loop flips `valid` on
  the next cycle (default 5 min, see
  `CRED_REVALIDATION_INTERVAL_MINUTES`).

Empty-state placeholder: `data-testid="creds-empty-passwords"`.

## Hashes

Tab `data-testid="creds-tab-hashes"`. Lists `cred_type='nthash'`
rows.

Two paths to populate this tab:

1. **Manual** — **ADD_CREDENTIAL** with type `nthash`, supply
   `<username>` + 32-char hex hash (`data-testid="nthash-input"`).
   Validation: invalid hex / wrong length → `data-testid="nthash-error"`.
2. **Auto** — **DUMP_FROM_BOX** (`data-testid="dump-from-box-btn"`):
   - Pick target Windows box (must have a known-good admin cred).
   - Fires `POST /c2/harvest/{box_ip}` → returns `202 {task_id}`.
   - Background coroutine runs
     `nxc smb <ip> -u <admin> -p <pw> --sam --lsa --local-auth` and
     upserts one `Credentials(cred_type='nthash', source='sam_dump' |
     'lsa_dump')` row per parsed `user:rid:lm:nt:::` line.
   - Toast carries a VIEW action → opens [Tasks](c2-tasks).

Concurrent dumps queue at the task-queue layer; no per-page modal
stacking.

Empty state: `data-testid="creds-empty-hashes"`.

## Tickets

Tab `data-testid="creds-tab-tickets"`. Lists `cred_type='ccache'`
rows scoped to a team (`box_ip=NULL`, `team_num=N`).

Header action: **FORGE_TICKET** (`data-testid="forge-ticket-btn"`)
opens a slide-in form. Required:

- `team_num` (`data-testid="team-num-input"`).
- Principal (default `Administrator`).
- Duration hours (default 10 — matches the default Kerberos TGT
  lifetime).

Body POSTs to `/c2/forge-ticket`. The handler calls
`forge_golden_ticket()` which shells to `impacket-ticketer`; result is
stored as a ccache blob (base64) in the new `Credentials` row.

Per-row actions:

- **VALIDATE** (`data-testid="ticket-validate-{cred_id}"`) — calls
  `POST /c2/tickets/{cred_id}/validate` against every box in the ticket's
  team; results render in an expandable sub-row. A per-row box picker
  (`ticket-validate-box-select-{cred_id}`) validates one box. See
  [Validate-ticket](#validate-ticket).
- **REFRESH** (`data-testid="ticket-refresh-{cred_id}"`) — calls
  `POST /c2/tickets/{cred_id}/refresh`, re-forges in place.
- **REVOKE** (`data-testid="ticket-revoke-{cred_id}"`) — calls
  `DELETE /c2/tickets/{cred_id}`, also deletes the materialized ccache
  file under `<NXC_PATH>/ccache/<cred_id>.ccache`.

Status chips:

- `data-testid="ticket-status-valid"` — valid + in-window.
- `data-testid="ticket-status-expired"` — `ticket_expires_at < now`.
- `data-testid="ticket-status-invalid"` — last probe failed.
- `data-testid="ticket-status-unknown"` — never probed.

The `TicketRefreshLoop` (`TICKET_REFRESH_INTERVAL_MINUTES`, default 5)
automatically refreshes any ticket where
`ticket_expires_at - now < TICKET_REFRESH_BEFORE_EXPIRY_MIN` (default
30 min).

Empty state: `data-testid="creds-empty-tickets"`.

## Dump-from-box

Mechanism deep-dive — what happens when you click the button:

1. Frontend posts to `POST /c2/harvest/{box_ip}` (a non-blue admin /
   red-team route).
2. Backend returns `202 {task_id}` and kicks off a background
   coroutine.
3. Coroutine calls `HashHarvestLoop._dump_one()` with a forced
   `is_domain_joined=true` override (the periodic loop applies the
   inventory filter; manual dump bypasses it).
4. NXC command:
   `nxc smb <ip> -u <admin> -p <pw> --sam --lsa --local-auth`.
5. Output parser regexes for `user:rid:lm:nt:::` lines; each is
   upserted as `Credentials(box_ip=<ip>, cred_type='nthash',
   source='sam_dump'|'lsa_dump')`.
6. The cred-revalidation loop picks up new rows within one cycle and
   sets `valid`.

**Opsec note**: SAM/LSA dump triggers Windows Event 4624/4672 + Sysmon
Event 1 on the target. Avoid against monitored boxes. The
`CRED_HARVEST_ENABLED` config key is the master kill switch.

## Forge-ticket

Mechanism — what `FORGE_TICKET` does end-to-end:

1. Operator submits team_num + principal.
2. Backend POSTs `forge_golden_ticket()` which:
   - Looks up the team's domain + domain SID (`Boxes.domain` /
     `Boxes.domain_sid`).
   - Looks up the team's krbtgt NT hash (`Credentials` row matching
     `username='krbtgt'` + team).
   - Shells out to `impacket-ticketer` to mint a TGT for
     `<principal>` valid for `<duration_hours>` (default 10).
   - Stores the ccache blob (base64) in a team-scoped row
     (`box_ip=NULL`, `team_num=N`).
3. The cred is immediately available to any NXC call that picks
   `prefer_cred_type='ccache'` + the matching `team_num`.

**Opsec note**: DCSync to capture krbtgt (the upstream of FORGE) is
Event 4662 on the DC. `KRBTGT_DCSYNC_ENABLED` (default `true`) is the
kill switch.

## What you'll see

- Tab strip with PASSWORDS / HASHES / TICKETS chips.
- Per-row badges: `valid=true/false`, source (sam_dump / lsa_dump /
  manual / inventory_default), VIA-IOC badge for auto-registered.
- Add-cred slide-in panel: opens from the right; the **NTHASH** input
  validates length + hex characters before submit.

## Validate-ticket

Proves a forged golden ticket actually authenticates via Kerberos domain
auth against a team's boxes — Windows **and** domain-joined Linux.

Clicking **VALIDATE** (`ticket-validate-{cred_id}`) POSTs to
`POST /c2/tickets/{cred_id}/validate` with an empty body (whole team). The
box picker (`ticket-validate-box-select-{cred_id}`) sends `{box_ip}` to
validate a single box. Results render in a sub-row
(`ticket-validate-results-{cred_id}`), one line per box
(`ticket-validate-result-{cred_id}-{box_ip}`): ✓ / ✗ / – plus the `method`.

Per box, the backend:

- **Windows** — `nxc smb <box> -k --use-kcache --kdcHost <dc> --shares`
  using the ticket's ccache (the Administrator golden ticket).
- **Linux** (domain-joined) — forges a golden ticket for the box's own
  domain user (from inventory `domain_username`) and attempts a real
  `ssh -o GSSAPIAuthentication=yes … <user>@<fqdn> id`; on any failure it
  falls back to `impacket-getST -spn host/<fqdn>` to prove the KDC accepts
  the ticket. `nxc ssh` is **not** used — it accepts `-k` but never performs
  GSSAPI.
- **Firewall / non-joined Linux** — reported `not-applicable` (`valid=null`).

A whole-team run stamps the ticket row's `valid` to the aggregate (true if
any box authenticated); a single-box run does not. Mechanism +
Kerberos-realm-case notes: `docs/PLAN_ticket_multibox_validation.md`.

## Troubleshooting

- **`422` on save** — the `(box_ip, protocol, username)` tuple already
  exists. Edit the existing row instead.
- **`valid=false` after revalidation** — the cred really doesn't auth.
  Verify with `scripts/diagnostic/check_access.py`.
- **DUMP returns 0 hashes** — check the box really is domain-joined
  (`is_domain_joined=true` in inventory + box reachable). Some Linux
  / pfSense boxes have empty SAM by design.
- **FORGE returns `400 missing_krbtgt`** — no captured krbtgt hash
  for that team. Run a DCSync first via the [Console](c2-console)
  page (or wait for the `TicketRefreshLoop` to do it when
  `KRBTGT_DCSYNC_ENABLED=true`).
- **Refresh / revoke does nothing** — check the per-row action menu
  is rendering (`data-testid` should match); the row's `cred_id` is
  the integer Credentials PK.

<!-- audited 2026-06-05 — testids verified against C2CredsPage.tsx; harvest + forge mechanism per wiki/subsystems.md § Credential harvesting. -->
