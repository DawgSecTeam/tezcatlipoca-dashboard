# Deployment

Register IOCs against deployed implants so the scoring pipeline picks
them up. **An IOC that isn't registered here scores zero, regardless of
what's actually on the target box.**

## When to use

- You manually planted an IOC (cron entry, registry key, file drop)
  and want the check pipeline to score it.
- You want C3rberus to drop *and* register the IOC's effect in one
  step ("DEPLOY & REGISTER").
- Auditing what IOCs are currently registered for each team.
- Removing an IOC you no longer want scored (blue team beat you to
  it, or you're cleaning up before recompete).

For Sliver-implant IOCs (`SliverCheck` IOCs — auto-registered on
session/beacon connect), don't use this page. Use
[Generation](c2-generation) instead.

## Prerequisites

- Logged in (any non-blue role; blue blocked).
- Target box in inventory.
- For **DEPLOY & REGISTER**: valid NXC cred for the box AND the IOC
  has an effect `deploy_script`.

## Deploy

The **DEPLOY** button registers an active `DeployedIOCs` row without
running any deploy script. Use this when you've already planted the
IOC out-of-band.

1. Pick an IOC from the left list (`data-testid="ioc-item-{name}"`).
2. Select one or more target boxes
   (`data-testid="deploy-box-{box_ip}"`; **Select All** at
   `data-testid="deploy-select-all"`).
3. Confirm. Returns `202 {task_id}`; per-box progress renders in the
   inline `DeployProgressPanel`.
4. Each successful box gets a `DeployedIOCs` row with
   `source='manual'`, `status='active'`,
   `blue_team_label='<OS>_<DIFFICULTY>_<N>'` (obscured per
   `assign_blue_team_label()`).

## Deploy-and-register

The **DEPLOY & REGISTER** button
(`data-testid="deploy-and-register-button"`) runs the IOC's effect
`deploy_script` via NXC, then registers on success.

Disabled when:

- The IOC's `IOC.check` is `SliverCheck` (use Generation page; 400
  returned if you bypass).
- The IOC has no `deploy_script`.
- No cred available for the target box (`409 missing_creds` unless
  `confirm_missing` flips it).

The optional **timestomp** checkbox
(`data-testid="timestomp-checkbox-deploy-modal"`) fires the timestomp
mechanism against `IOC.timestomp_paths` after the deploy script lands,
**before** any immutability step (timestomp on an immutable file =
EPERM).

## Auto-creds

Each `CredentialSpec` in `IOC.added_credentials` becomes an unvalidated
`Credentials` row on successful deploy, with `source_deployed_ioc_id`
set to the new `DeployedIOCs.id`.

The `/c2/creds` table renders these with a **"VIA <ioc>"** badge
(`data-testid="cred-via-ioc-badge"`) so you can tell auto-registered
creds apart from operator-added or inventory-default rows.

Existing rows matching `(box_ip, protocol, username)` are **left
alone** — auto-cred registration is upsert-by-key, never overwrites
operator input.

## Deregister

The trash-icon button (`data-testid="deregister-{id}"`) calls
`DELETE /c2/iocs/deployed/{id}`. This:

- Flips `DeployedIOCs.status` to `failed`.
- **Deletes** any `Credentials` rows whose `source_deployed_ioc_id`
  matches — cascading cleanup. Inventory-default and operator-added
  creds are never touched (their `source_deployed_ioc_id` is NULL).
- Returns `{credentials_removed: N}` so the UI can toast the count.

The next check cycle stops scoring this IOC because the check pipeline
only queues IOCs with an active row.

**Virtual scoring IOCs are protected.** The orchestrator auto-seeds one
`default_cred_<os>` IOC per box (`source='virtual'`) that scores whether
blue hardened the default Administrator / root account (see
[IOC Authoring § check-types](ioc-authoring#check-types)). `DELETE`
rejects these with `422 cannot deregister a virtual
scoring-infrastructure IOC`, and the bulk reset sweep skips them — they
live for the whole competition.

## What you'll see

- Left list — IOC catalog filtered by OS / category. Each item shows
  base points, soak-eligibility badge (both deploy + remediate scripts
  present), Sliver-check badge.
- Right pane — deployed IOCs table
  (`data-testid="deployed-iocs-table"`); two views: **GROUPED** by
  IOC name (`data-testid="ioc-view-toggle-grouped"`, default) or
  **FLAT** (`data-testid="ioc-view-toggle-flat"`).
- Source badges — `MANUAL` vs `AUTO` (Sliver-registered)
  (`data-testid="source-badge-{id}"`).
- Status badges — `LOST` (`data-testid="status-lost-{id}"`) for
  Sliver-auto IOCs whose implant was intentionally killed.
- File-protection toggle (`data-testid="file-protection-toggle"`) +
  per-row PROTECT / UNPROTECT buttons
  (`data-testid="file-protect-btn"` / `"file-unprotect-btn"`).

## Troubleshooting

- **Button is greyed** — the IOC is a `SliverCheck` (use Generation),
  has no `deploy_script` (DEPLOY-only), or the selected box has no
  valid cred.
- **`400 not_sliver_check_ioc`** — you hit DEPLOY & REGISTER on a
  Sliver-check IOC. Use the [Generation](c2-generation) page.
- **`409 missing_creds`** — register a cred via [Credentials](c2-creds)
  first, or set `confirm_missing=true` and accept skipped boxes.
- **`502 deploy_script_failed`** — the IOC's deploy script ran but the
  output didn't parse as `status=1`. Tail
  `logs/fastapi_backend_<ts>.log` for the NXC stdout/stderr.
- **Cred still present after deregister** — that cred has
  `source_deployed_ioc_id=NULL`; it was inventory-default or
  operator-added, not auto-registered. Delete it manually from
  `/c2/creds`.

<!-- audited 2026-06-05 — testids verified against C2DeploymentPage.tsx; endpoint contract verified against routes/ioc_deploy.py; auto-cred badge testid `cred-via-ioc-badge` cited per wiki/architecture.md. Updated 2026-06-26 — added virtual scoring-IOC deregister-protection note (ioc_deploy.py:757 422; bulk sweep skips source='virtual'). -->
