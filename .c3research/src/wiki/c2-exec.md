# Exec

Dispatch shell commands or push files to one or many target boxes.

## When to use

- Ad-hoc reconnaissance: `whoami`, `id`, `ipconfig /all`, …
- Configuration tweaks: registry edits, service stops, simple
  cron / scheduled-task installs.
- Mass ops: same command across an OS group.

For multi-step or compiled-binary actions, use a module
(see `c2-modules`). For interactive sessions, use the shell
(see `c2-targets`).

## Prerequisites

Depends on dispatch method:

- `session` — live Sliver session on the target.
- `beacon` — live Sliver beacon on the target.
- `nxc` — valid credential for the box (SMB on Windows, SSH on Linux /
  pfSense).

## Execute

Open `/c2/exec`. The page is built around the `bulk_exec_async`
endpoint (`POST /c2/bulk/exec/async`).

Steps:

1. **Method** — pick `session` / `beacon` / `nxc`.
2. **Targets** — multi-select with search + OS + team + implant-type
   filters; matches the Targets page filter set.
3. **Command** — the line to run.
4. (NXC mode only) **Cred type** — `password` / `nthash` / `ccache`.
   Leave blank to take the best available.
5. **Confirm missing** — flip to `true` if you've already seen a
   `409 missing_*` and want to proceed anyway.
6. Click **EXECUTE_JOB**. Returns `202 {task_id}`.

Output decoding tries UTF-8, then cp437 (legacy Windows console), then
latin-1 with replacement (`decode_stdout` in `c2/bulk_exec.py`).

## Put-file

The **PUT_FILE** tab fronts the multipart staging endpoint
(`POST /c2/bulk/put-file/stage`) and the async dispatcher
(`POST /c2/bulk/put-file/async`).

1. Pick the local file on the C3rberus host.
2. Select target boxes.
3. Set the remote path. **Windows drive letters are stripped** for
   SMB (`C:\...` becomes `\...`), since the SMB share root is the drive.
4. Click **PUT_FILE**. Returns `202 {task_id}`.

## NXC mode

`method=nxc` uses the same `NXCWrapper.execute_cred()` path the IOC
check pipeline uses. Concurrency is bounded by
`min(NXC_MAX_CONCURRENT, BULK_EXEC_MAX_CONCURRENT)` — defaults make this
40 in either case.

The exec route forces `exec_method="smbexec"` for SMB targets; smbexec
is generally the most reliable across NXC versions. On smbexec failure
detected by `_EXEC_FAILURE_RE`, the wrapper retries once without
`--exec-method`.

## What you'll see

- Task header — id, type (`bulk_exec`), status, operator, target count.
- Per-box rows — `status`, `exit_code`, decoded stdout/stderr, error
  if any, attempts, exec path, duration.
- Live updates via SSE (`/c2/tasks/{task_id}/stream`) drive the panel
  without manual refresh.

## Troubleshooting

- **`409 another_bulk_exec_running`** — the **same operator** already has
  a `bulk_exec` task in flight. Wait or check `/c2/tasks` for status.
  Different operators run in parallel.
- **`409 missing_implants`** — at least one selected box has no live
  session / beacon. Retry with `confirm_missing=true` to accept the gap
  (rows show `status='skipped'`).
- **`409 missing_creds`** — same idea for NXC method. Set
  `confirm_missing_creds=true` to skip the bad boxes.
- **422 on `cred_id`** — pinned a specific cred to a box that doesn't
  match (different team or different box). Drop the pin or pick a cred
  that lines up.
- **Per-box `ImplantDiedAfterResolveError`** — the session died between
  resolve and dispatch; the row shows the explicit race-condition error.
  Regenerate the implant.

<!-- audited 2026-06-05 — EXECUTE_JOB / PUT_FILE button labels verified against C2ExecPage.tsx; 409 codes (another_bulk_exec_running, missing_implants, missing_creds) and decode_stdout fallback chain verified against fastapi_backend/c2/bulk_exec.py. data-wiki-page seeds (#execute, #put-file) resolve. -->

