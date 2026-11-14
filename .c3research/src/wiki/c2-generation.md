# Generation

Generate a new Sliver implant and optionally auto-deploy it to one or more
target boxes in a single async task.

## When to use

- You have no live session / beacon on a target box and want to plant a
  fresh implant.
- You want to register the implant against an IOC ("SliverCheck") so the
  scoring pipeline picks it up automatically.

If you already have a session on the box, **don't regenerate** — use the
existing one. Regenerating churns your op-sec for nothing.

## Prerequisites

- Target boxes exist in inventory.
- Sliver mTLS listener is up (see
  [Troubleshooting § Sliver not connecting](troubleshooting)).
- For auto-deploy: at least one valid NXC cred per target box (`/c2/creds`).

## Generate

Open `/c2/generation`. The page is split into form (left) and
DeployProgressPanel (below).

Form:

1. **Protocol** — `mtls` (default, recommended), `http`, `https`, `dns`,
   `wg`, `pivot`. Use `pivot` mode for pfSense implants that should host a
   per-team TCP pivot (see [Persistence](#persistence) for tier picker).
2. **C2 IP** (`data-testid="c2-ip-input"`) — editable, pre-filled with the
   auto-detected address. Override it when the auto-detected value is
   wrong for the target's network path (e.g. multi-homed C3rberus host).
   A reset button (`data-testid="c2-ip-reset-btn"`) restores the detected
   value. In `pivot` mode, an override here also drives the pfSense leg's
   `c2_url` — it's no longer hardcoded to `config.C2_IP`.
3. **Persistence** — pick a persistence mechanism from the dropdown. The
   selection drives `ImplantBuilds.persistence_mechanism` and, when
   Sliver auto-registration fires, becomes the `DeployedIOCs.persistence_mechanism`.
4. **Persistence tier (pivot only)** — appears when protocol is `pivot`;
   drives a tier-group of paired pfSense persistence deploys.
5. **Immutable scope** — three-way radio: `none` (default), `binary_only`
   (`chattr +i` / `chflags schg` / `attrib +R` on the deployed binary
   only), `binary_and_persistence` (also locks the persistence artefacts).
   Runs *after* timestomp so the timestomp writes don't fail on EPERM.
6. **Timestomp** — set the binary's mtime / atime / btime to match a
   sibling in the drop directory (`pick_reference()` logic).
7. **Storage Mode** — selects the implant delivery and storage family
   (OS-filtered):
   - `file` (default) — binary dropped to DROP_DIR, conventional on-disk placement.
   - `self_inject` — dropper injects shellcode into a host process (svchost/journald/syslogd)
     and self-deletes; no persistent binary remains on disk. Check: active Sliver session.
   - `self_inject_persist` — inject dropper + persistence shim; choose the
     persistence type sub-picker that appears (`wmi` / `udev` / `devd`).
   - `registry` (Windows) — shellcode stored in a registry value; a scheduled task
     loader reads and executes it. Check: registry key present.
   - `pe_section` (Windows) — shellcode embedded in a custom `.c3r_sc` PE section
     on a carrier binary in DROP_DIR. Check: carrier file + section name.
   - `xattr` (Linux) — shellcode in a `user.c3r_sc` extended attribute on `/etc/hosts`.
     Check: xattr present.
   - `elf_section` (Linux/pfSense) — shellcode in a `.c3r_sc` ELF section on a
     carrier binary in DROP_DIR. Check: carrier file + section name.
   - `extattr` (pfSense) — shellcode in a `system c3r_sc` extended attribute on
     `/etc/hosts`. Check: extattr present.

   Storage modes `self_inject*` automatically disable the **Backup copy** toggle.

   **Shared-library (`.so`) format (FreeBSD/Linux only):** generating with
   `format=shared` builds a Sliver shared object instead of an executable. The
   `.so` is not itself runnable, so C3rberus deploys a native loader
   (`iocs/payloads/so_loader_{freebsd,linux}`) **beside** it and launches
   `<loader> <so> RunSliver` (FreeBSD via `daemon -f`, Linux backgrounded) — the
   loader `dlopen()`s the `.so` and calls the `RunSliver` export in a thread. The
   local library copy under `./implants/<uuid>/` keeps a self-describing `.so`
   filename; the on-target copy is extensionless like every other Linux/BSD
   implant. Windows keeps `format=exe`. See
   [IOC Catalog § Shared-Library (.so) Implant IOCs](ioc-catalog) for the four
   scored IOCs (`{firewall,linux}_implant_so_{bare,rc|systemd}`).
8. **Backup copy** — stage a second immutable copy of the deployed binary to
   BACKUP_DIR (one difficulty tier more obscure than DROP_DIR). The watchdog
   persistence script restores the primary from BACKUP_DIR when the primary is
   missing. Locked ON (shown with "Required by IOC" badge) for the three
   pivot-anchor IOCs (`firewall_implant_devd_kqueue`, `linux_implant_udev`,
   `windows_implant_inject`). Available as an opt-in toggle for other SliverCheck
   IOCs. Check scripts return `status=1` when primary OR backup is present;
   `status=0` only when both are gone and the watchdog is removed.

Click **GENERATE**. You'll get `202 {task_id}`. The page begins polling
`GET /c2/tasks/{task_id}` and the DeployProgressPanel renders progress.

## Persistence

The **Persistence** dropdown drives the `ImplantBuilds.persistence_mechanism`
field on the resulting build row. When Sliver fires `session-connected` or
`beacon-registered` for the new implant, `sliver_manager._auto_register_implant()`
upserts a `DeployedIOCs` row that copies this value — so the IOC check
pipeline can score the implant.

Pick a mechanism appropriate to the OS + tier you're going after. For
pivot-mode implants on pfSense, the **Persistence tier (pivot)** dropdown
replaces this single selector and drives a tier-group of paired
deploys.

## Auto-deploy

If you toggle **AUTO-DEPLOY**, the form expands a target-box list:

- Header checkbox **Select All** — indeterminate when partially selected.
- Each row: box name, IP, OS, team #, last-cred status badge.
- The `N/Total` count badge at the top of the list reflects the current
  selection vs total boxes shown.

`GENERATE` then chains compile → deliver → execute on every selected
box. Delivery prefers an existing live Sliver session or beacon already
on that box (uploads the new binary through it, chmod, launch — no NXC
traffic at all for that box) and only falls back to NXC put_file +
execute when neither is available. Either way it authenticates with
your own operator credentials, **not** the `red_scoring` account IOC
check/deploy/remediate uses — a broken/absent `red_scoring` cred does
not block a Generate-page deploy the way it would block IOC scoring.
Each box's progress appears as its own row in the progress panel,
regardless of which channel delivered it.

## What you'll see

Progress panel structure:

- **Task header** — `task_id`, status chip (`pending` / `running` /
  `done` / `failed`), elapsed time.
- **Phase row** — `compiling` → `deploying` (set on the OperatorTask's
  `current_phase` so the UI can colour by stage).
- **Per-box rows** — IP, status (`pending` / `running` / `done` /
  `failed` / `skipped`), error if any, decoded stdout/stderr, exec path,
  duration.

## Troubleshooting

- **400 missing_listener** — Sliver mTLS listener isn't up. Start it via
  the admin Operations page or restart the backend (auto-creates on boot).
- **502 compile_failed** — Sliver gRPC error. Tail
  `logs/fastapi_backend_<ts>.log` for the gRPC stack trace.
- **`missing_creds` in auto-deploy** — go to `/c2/creds`, register a
  cred for the box, wait for revalidation, regenerate. This applies to
  your own operator cred, not `red_scoring` — the Generate page never
  resolves that account.
- **A redeploy to an already-implanted box shows no NXC activity in the
  logs** — expected; it delivered through the box's existing session or
  beacon instead. Check the row's `exec_path` (`session`/`beacon` vs
  `nxc`) to confirm which channel was actually used.
- **`status=skipped` per box** — explicit cred miss; resubmit with
  `confirm_missing=true` if you accept the gap, or fix creds first.
- **Generation hangs at `compiling`** — Sliver gRPC unresponsive. The
  compile is serialized through an asyncio Lock so concurrent operators
  queue inside it; another operator's compile may still be running.

<!-- audited 2026-06-05 — protocol options (mtls/http/https/dns/wg/pivot) verified against C2GenerationPage.tsx ProtocolOption type; immutable_scope three-way radio reflected; data-wiki-page seeds (#generate, #auto-deploy) point at real anchors. Updated 2026-07-21 — auto-deploy cred routing note (session/beacon-first delivery, operator creds not red_scoring), docs/PLAN_operator_ux.md § C. Updated 2026-07-24 — storage mode picker (8 modes, OS-filtered) and backup copy toggle documented (PLAN_resilient_implant_iocs.md + PLAN_backup_copy_persistence.md). -->

