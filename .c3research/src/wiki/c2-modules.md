# Modules

Bring-your-own binary or script as a reusable C2 "module": upload once,
dispatch many times.

## When to use

- Multi-step PowerShell scripts that don't fit a one-liner.
- Compiled tools (Mimikatz, SharpHound, Rubeus) you want to push and
  run on demand.
- Linux-only payloads (`.sh`, ELF binaries).

For a one-shot shell command, [Exec](c2-exec) is faster. For a fresh
implant, use [Generation](c2-generation).

## Prerequisites

- Logged in (any non-blue role).
- Module file on your local workstation (it gets uploaded to the
  C3rberus host).
- For dispatch: valid creds for the target boxes (NXC mode) OR live
  Sliver session/beacon.

## Upload

The header **UPLOAD** button (`data-testid="upload-btn"`) opens a file
picker (`data-testid="upload-input"`). Select your module — the file
streams to the backend and lands as a `modules/` row.

Recurring usage:

1. Click **UPLOAD**.
2. Pick file. Backend infers OS from extension (`.ps1`, `.exe`, `.sh`,
   etc.).
3. Edit metadata (label, OS, parameter schema if needed).
4. Save. Module appears in the list (`data-testid="module-row-{id}"`).

The **RESCAN** button (`data-testid="rescan-btn"`) re-walks the
`modules/` directory — useful when you dropped a file in via SSH
instead of the upload form. The green banner
(`data-testid="rescan-banner"`) signals a rescan in progress.

## Run

Select a module from the list, fill the parameter form on the right,
click **RUN** (`data-testid="run-module-btn"`).

Each dispatch returns `202 {task_id}`. The task lives on
[Tasks](c2-tasks) as `task_type='module'`; per-box progress streams via
SSE and renders inline (`data-testid="exec-path-{box_ip}"`).

Source toggle: **SOURCE** (`data-testid="module-source-btn"`) flips
between dispatch form and raw module body view (read-only for ops).

Delete: **DELETE** (`data-testid="module-delete-btn"`) removes the
module from `modules/` after a confirm dialog.

## Parameters

A module's parameter form is rendered from its declared schema (think
TOML / JSON config alongside the binary). Common shapes:

- Free-text inputs (string params).
- Numeric inputs.
- Box / team pickers (when the module is target-aware).
- File pickers for secondary upload (e.g. `--ticket-file`).

The parameter form's "Params used" appear on the resulting
`OperatorTask.metadata_json.params_used` so you can re-run with the
same input.

## What you'll see

- Module list (`data-testid="module-list"`) on the left, filtered by OS
  (`data-testid="filter-os"`).
- "No modules" placeholder (`data-testid="no-modules-msg"`) until you
  upload your first one.
- Per-box progress table on the right with `status`, `exit_code`,
  decoded stdout/stderr, `error`, attempts, `exec_path`, duration.

## Service-interruption modules

Five built-in modules stop critical services on target boxes. They are
**synthetic** — registered from `config/services.yml` at startup, not
uploaded from disk. They appear in the module list with `target_os = any`
and the purple **ANY** OS badge.

### Module list

| Module name | Effect |
|---|---|
| `stop_dns` | Stops the DNS server (DC: DNS; Linux: named/bind9; pfSense: named) |
| `disable_web_server` | Stops + disables the web server (IIS / nginx+apache2 / pfSense nginx) |
| `disable_smb` | Stops + disables LanmanServer. **Windows-only** (no Linux command). |
| `disable_ad` | Stops ADWS, kdc, Netlogon. **DC-only**; silently fails on members. |
| `disable_ntp` | Stops time sync (W32Time / timesyncd+chronyd / ntpd) |

> **Warning:** `disable_web_server` on pfSense stops nginx, which also
> kills the pfSense web UI. The box is still SSH-accessible but the
> admin panel goes dark.

### Running a service module

1. Select the module from the list (filter by **ANY OS** to find them quickly).
2. The **Service** dropdown (`data-testid="service-picker"`) is pre-selected
   to the module's own service key. Change it if you want to run a different
   command from `services.yml`.
3. Pick your target boxes — all box OSes are shown since commands are OS-aware.
4. Click **RUN**.

The command is resolved at run time from `config/services.yml` via the
box's OS. If a service has no command for an OS (e.g. `disable_smb` on
Linux), the module runner returns a clear error in the task output.

### Configuring services.yml

Copy `config/services.example.yml` to `config/services.yml` and adjust
command strings for your environment. The file is gitignored.

```yaml
stop_dns:
  windows: "Stop-Service DNS -Force -ErrorAction SilentlyContinue"
  linux: "systemctl stop named 2>/dev/null || service bind9 stop 2>/dev/null || true"
  firewall: "service named stop 2>/dev/null || true"
```

See `config/services.example.yml` for the full schema.

**Operator override:** the optional `service` parameter in the run form
accepts a raw command string that overrides `services.yml` for that run.

`GET /c2/modules/services` returns the currently-loaded command map.

## Troubleshooting

- **Module not appearing after upload** — hit **RESCAN**; the watcher
  is rescan-on-demand, not polling.
- **Linux module returns `127`** — missing exec bit on the target.
  Wrap the body so the dispatcher does `chmod +x` before running, or
  re-upload as a binary file (not a text payload).
- **PowerShell ExecutionPolicy block** — the dispatch wrapper already
  passes `-ExecutionPolicy Bypass`. If you still hit policy errors,
  check Constrained Language Mode is not engaged on the target.
- **Task stuck at `pending`** — see
  [Tasks § stuck](c2-tasks#troubleshooting); module dispatch shares the
  bulk-exec semaphore pool.

<!-- audited 2026-06-05 — testids verified against ModulesPage.tsx; task_type='module' progress contract per wiki/architecture.md OperatorTask. -->
