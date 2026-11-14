# Operator Actions

Day-1 cheatsheet covering every supported red-team action. Each section
has the operator-action template: when to use, prerequisites, steps,
what you'll see, troubleshooting. `See also:` lines link to deeper
pages where they exist (some land in the second-wave content batch).

## overview

Look at general competition info — what sessions / beacons are up, what
boxes you have creds for, what boxes are reachable.

- **When to use**: at competition start, after every restart, and any
  time the scoreboard moves unexpectedly.
- **Prerequisites**: logged in as red / white / black / admin.
- **Steps**:
  1. Navigate to `/c2` (the Overview page).
  2. Use the Network Map for a per-team view of boxes, sessions, beacons.
  3. Use the Alert button (bottom-right) for critical / warn events.
- **What you'll see**: per-team box grid coloured by status, live
  session / beacon counts, NXC degraded flags, pivot lines (purple).
- **Troubleshooting**: if the map looks empty, check the SSE indicator
  in the left nav — it should glow green ("LIVE").
- *See also*: [c2-overview](c2-overview).

## exec-command

Run a command on one or many boxes.

- **When to use**: ad-hoc reconnaissance, configuration tweaks, mass
  ops across an OS group.
- **Prerequisites**: at least one of — a live Sliver session, a live
  Sliver beacon, or a valid NXC credential for the target boxes.
- **Steps**:
  1. Open `/c2/exec`.
  2. Pick dispatch method (`session` / `beacon` / `nxc`).
  3. Select target boxes (search / OS filter / team filter).
  4. Type the command and click **EXECUTE_JOB**. You'll get a `202` with a
     `task_id`; the progress panel polls the task row.
- **What you'll see**: per-box rows in the progress panel with `status`,
  `exit_code`, decoded stdout/stderr.
- **Troubleshooting**:
  - `409 another_bulk_exec_running` — the same operator already has a
    bulk exec in flight. Wait or cancel.
  - `409 missing_implants` — set "Confirm missing" and resubmit, or
    swap dispatch method.
  - `409 missing_creds` — see [add-creds](#add-creds).
- *See also*: [c2-exec](c2-exec).

## put-file

Push a file to one or many boxes.

- **When to use**: drop a binary, config, script, or payload onto a
  target before running it.
- **Prerequisites**: valid NXC cred for SMB (Windows) or SSH
  (Linux / pfSense); your local file at a known path on the C3rberus
  host.
- **Steps**:
  1. Open `/c2/exec` → **PUT_FILE** tab.
  2. Select target boxes and pick the local file (multipart stage).
  3. Set the remote path (Windows drive letters are stripped for SMB).
  4. Click **PUT_FILE**. You'll get `202` + `task_id`.
- **What you'll see**: per-box upload result with byte count.
- **Troubleshooting**: SMB share misconfiguration → choose a path under
  `C:\Windows\Temp` (always present + writable).
- *See also*: [c2-exec § put-file](c2-exec#put-file).

## deploy-module

Bring your own binary or script, register it as a module, and run it on
targets.

- **When to use**: when the action isn't a single shell line — e.g. a
  Mimikatz-style binary, a multi-step PowerShell module, a Linux-only
  payload.
- **Prerequisites**: a module file you've staged under `modules/`, OR
  uploaded via the Modules page.
- **Steps**:
  1. Open `/c2/modules`.
  2. Upload your script / binary, label it, pick OS.
  3. From the module's row, click **RUN** and pick targets.
  4. Watch the task on `/c2/tasks`.
- **What you'll see**: per-box module output + exit code.
- **Troubleshooting**: missing exec permissions → re-upload as binary
  (not text); for PS scripts, the wrapper handles execution policy.
- *See also*: [c2-modules](c2-modules).

## generate-implant

Generate a new Sliver implant and (optionally) auto-deploy it to a list
of target boxes.

- **When to use**: you don't yet have a session / beacon on the box(es)
  you want to control.
- **Prerequisites**: target boxes present in inventory; Sliver mTLS
  listener up (see [Troubleshooting § Sliver not connecting](troubleshooting)).
- **Steps**:
  1. Open `/c2/generation`.
  2. Pick protocol (mtls / wg / http / dns), persistence, immutability
     toggle, timestomp toggle.
  3. If you want auto-deploy: toggle **AUTO-DEPLOY**, select target boxes
     (Select All header checkbox available, indeterminate when partial).
  4. Click **GENERATE**. You'll get `202` + `task_id`.
- **What you'll see**: `DeployProgressPanel` below the button — first
  compile progress, then per-box deploy results.
- **Troubleshooting**:
  - `400 missing_listener` — start the listener first.
  - `502 compile_failed` — Sliver gRPC error; check the backend log.
  - Auto-deploy shows `missing_creds` — fall back to NXC + push the
    binary manually, or register creds first.
- *See also*: [c2-generation](c2-generation).

## schedule-task

Schedule a command exec or module run for a future absolute time, or
relative to competition start.

- **When to use**: phased plays — drop X at T+10m, dump hashes at
  T+30m, etc.
- **Prerequisites**: same as exec / deploy-module depending on
  scheduled type.
- **Steps**:
  1. Open `/c2/tasks` → **NEW SCHEDULED TASK** button.
  2. Fill name, fire time, action type, payload.
  3. Click **CREATE**. Task lands in the queue with `status=scheduled`.
- **What you'll see**: the task appears at the top of the queue with a
  countdown chip; when fire time hits, dispatch starts.
- **Troubleshooting**: scheduler not firing → confirm the orchestrator
  is in `Running` state (`/admin/operations`).
- *See also*: [c2-tasks](c2-tasks).

## open-shell

Open an interactive Sliver shell or OS shell on a target and type into
it like a normal terminal.

- **When to use**: anything you can't do via a one-shot exec —
  interactive sudo prompt, paging output, manual recon.
- **Prerequisites**: live Sliver session on the target.
- **Steps**:
  1. From `/c2/targets`, expand a box.
  2. Click the **SHELL** action — pick Sliver shell or OS shell.
  3. Type commands; output stays in xterm. Resize via the pane drag
     handles; use the `[L]` / `[R]` tab buttons to send the shell to
     the left or right split pane.
- **What you'll see**: an xterm pane in the bottom-of-screen terminal
  tab strip; multiple operators can join the same shell (fan-out hub).
- **Troubleshooting**: blank terminal → check session is still alive
  (Targets row shows green dot); reconnect by closing and reopening
  the tab.
- *See also*: [c2-targets](c2-targets).

## task-history

Look at the output of a previously executed task.

- **When to use**: confirm what shipped, grab the stdout for a write-up,
  re-check a failure.
- **Prerequisites**: none.
- **Steps**:
  1. Open `/c2/tasks`.
  2. Find the row (filter by status / type / operator; "My Tasks" filter
     hides other operators).
  3. Click the row to expand per-box details.
- **What you'll see**: per-box rows with `status`, `exit_code`, error,
  decoded stdout/stderr. Live tasks update via SSE.
- **Troubleshooting**: missing decoded output → the target may have
  produced binary; raw bytes still in the row.
- *See also*: [c2-tasks](c2-tasks).

## sa-inspect

Inspect collected situational-awareness data (users, processes, ports,
shell / auth history, etc.) per box.

- **When to use**: figure out what's running on a box, which accounts
  are present, what's listening, recent privileged actions.
- **Prerequisites**: SA loop running (default — runs every
  `SA_COLLECTION_INTERVAL_MINUTES`); creds for the box.
- **Steps**:
  1. Open `/c2/targets` → expand a box.
  2. Expand **SITUATIONAL AWARENESS**; first expand triggers a lazy fetch.
  3. Switch tabs (users / processes / ports / history / auth_log / …).
  4. Click **Run SA Now** to force a fresh collection.
- **What you'll see**: tab-specific tables; "CollectedAt" timestamp.
- **Troubleshooting**: stale-looking SA → `Run SA Now`; if that fails,
  check NXC reachability on the box.
- *See also*: [c2-targets](c2-targets).

## sliver-console

Drop into the full Sliver operator console (the same CLI you'd run
locally) inside the app.

- **When to use**: anything that needs raw Sliver commands —
  `socks5 start`, `portfwd add`, `armory install`, etc.
- **Prerequisites**: `sliver-client` binary on the C3rberus host and a
  pre-imported operator config under `~/.sliver-client/configs/`.
- **Steps**:
  1. Open `/c2/console`.
  2. Click **OPEN CONSOLE**. Per-WS PTY subprocess spins up.
- **What you'll see**: the standard Sliver prompt in an xterm pane;
  resize via drag handles works.
- **Troubleshooting**: in `DEV_MODE` the console is stubbed; switch off
  dev mode to use it for real.
- *See also*: [c2-console](c2-console).

## deploy-all-iocs

Deploy and register the entire IOC catalog across all boxes in one
action. Intended for competition kick-off once VMs are confirmed
reachable.

- **When to use**: at competition start after `check_access.py` passes
  — instead of deploying each IOC individually via the Deployment page.
- **Prerequisites**: admin role; backend in `Running` state (or use
  `confirm=true` to override); valid NXC creds for each box type
  (deploy-and-register needs them); Sliver listener up for implant IOCs.
- **Steps**:
  1. From `/admin`, click **DEPLOY ALL IOCS**
     (`data-testid="btn-deploy-all-iocs"`).
  2. The button lives next to the preflight controls (only visible in
     `stopped` / `not_started` / `preflight_on` states, or if
     `confirm=true` while Running).
  3. A `deploy_all` parent task is created (`202 {task_id}`); watch
     progress in the `DeployAllProgressPanel` that expands below the
     button — per-IOC rows with child task drill-down.
- **What you'll see**: one row per IOC showing `deploying` → `done` /
  `failed` / `skipped`. Skipped rows carry a `{reason}` — e.g.
  `inject_ioc` (inject-type IOCs are skipped; use Generation page for
  those), `no_deploy_script`, or `already_active`.
- **Troubleshooting**:
  - `409 another_deploy_all_running` — concurrent submit blocked;
    wait for the in-flight parent task to complete.
  - `400 confirm_required` — competition is in `Running` state; add
    `confirm=true` to acknowledge.
  - Child task shows `502` — the IOC's deploy script ran but NXC
    returned a failure. Check the child task's per-box details for
    NXC stdout/stderr.
- *See also*: [running-competition § deploy-all](running-competition#deploy-all-iocs),
  [c2-tasks](c2-tasks).

## add-creds

Add new credentials for a box (per-box) or team (team-wide).

- **When to use**: any time you've pulled a fresh credential — pwdump,
  Mimikatz, golden ticket, captured cleartext, etc.
- **Prerequisites**: the credential material (password, NT hash, or
  ccache blob).
- **Steps**:
  1. Open `/c2/creds`.
  2. Pick the **PASSWORDS** / **HASHES** / **TICKETS** tab matching the
     cred type.
  3. Click **ADD CREDENTIAL** (or **FORGE_TICKET** on the Tickets tab),
     fill in box / team / user / secret, save.
  4. The cred-revalidation loop validates within one cycle (default 5
     minutes) — manual revalidation via the row's action menu.
- **What you'll see**: row shows `valid=true` after revalidation.
- **Troubleshooting**:
  - 422 on save — the (box, protocol, user) tuple already exists; edit
    the existing row.
  - `valid=false` after revalidation — the credential really doesn't
    auth; re-verify with `scripts/diagnostic/check_access.py`.
- *See also*: [c2-creds](c2-creds).

<!-- audited 2026-06-13 — added deploy-all-iocs section (072e12a). 12 action sections total. -->

