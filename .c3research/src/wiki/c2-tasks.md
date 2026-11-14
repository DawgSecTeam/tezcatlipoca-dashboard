# Tasks

The Task Queue: every async operator action's lifecycle, plus the
scheduler for fire-at-T+offset jobs.

## When to use

- Inspect what a prior `generate` / `deploy` / `module` / `exec` /
  `bulk_exec` / `harvest` job actually did.
- Schedule a command or module for a future time.
- Cancel / re-run a scheduled task.

For real-time per-box progress while a task is mid-flight, follow the
SSE stream from the task row (`GET /c2/tasks/{id}/stream`); the row
auto-expands when a task is `running`.

## Prerequisites

- Logged in (any non-blue role).
- The relevant async endpoint already accepted the job (returned
  `202 {task_id}`).

## Filters

Header row carries the filter set:

- **Status** (`data-testid="task-status-filter"`) — `all` / `pending` /
  `running` / `done` / `failed` / `cancelled` / `scheduled`.
- **Type** (`data-testid="task-type-filter"`) — `generate` /
  `generate_deploy` / `generate_pivot` / `deploy` / `deploy_all` /
  `implant_deploy` / `module` / `exec` / `bulk_exec` / `harvest` /
  `scheduled` / `scoring_preflight`.
- **My tasks** (`data-testid="my-tasks-btn"`) — hides rows owned by
  other operators.

Polling runs every 5s.

## Scheduled-task

The **NEW SCHEDULED TASK** button (`data-testid="new-scheduled-task-btn"`)
opens a modal (`data-testid="new-scheduled-task-modal"`):

- **Name** (`data-testid="task-name-input"`) — free-text label.
- **Command** (`data-testid="task-command-input"`) — the shell line.
- **Target type** (`data-testid="task-target-type"`) — `box` / `team` /
  `os`.
- **Target** (`data-testid="task-target-input"`) — IP / team number /
  OS depending on type.
- **Run at** (`data-testid="task-run-at"`) — `datetime-local` absolute
  fire time.
- **Repeat secs** (`data-testid="task-repeat-secs"`) — integer; `0` =
  one-shot.

Click **SCHEDULE** (`data-testid="task-schedule-btn"`). The row appears
at the top of the queue with `status=scheduled` and a countdown chip.

## My-tasks

Toggle the **MY TASKS** button to hide rows belonging to other
operators. Useful when several red-team operators share the page —
the queue table can fill quickly during a competition.

The button is a stateful toggle: filter persists until you tab away
or click again.

## View-progress

Click a row's **VIEW OUTPUT** button (`data-testid="task-view-{id}"`) to
open a progress modal. The modal subscribes to
`GET /c2/tasks/{task_id}/stream?token=` (SSE) and renders per-box rows
keyed by IP. Each row shows:

- `status` chip — `pending` / `running` / `done` / `failed` /
  `skipped` / `cancelled`.
- `exit_code`.
- Decoded stdout + stderr (UTF-8 → cp437 → latin-1 fallback per
  `decode_stdout` in `c2/bulk_exec.py`), shown by clicking a box row to
  expand it — this is populated whenever a dispatcher's `details` field
  is JSON shaped as `{stdout, stderr, exit_code}` (`bulk_exec`,
  `exec_single`, `module` all do). Rows without that shape (e.g. a raw
  metadata dict) show their summary fields only, with no expand arrow.
- `error` (when set).
- `attempts`, `exec_path`, `duration_s`.

The same modal renders for `module`, `deploy`, `harvest`, and
`bulk_exec` types — they all share the same `OperatorTask.progress_json`
structure.

**`deploy_all` tasks** are different: the parent task's `progress_json`
is keyed by **`ioc_name`** (not box IP) and each value is
`{child_task_id, deployed, failed}` (or `{reason}` for skips). Drill
into the listed child task IDs for per-box progress on each IOC.

**Pivot-set IOCs share one child task.** The gateway, linux, and windows
members of a pivot set are generated as a unit, so all three parent
entries name the *same* `child_task_id` and the `deployed`/`failed`
counts in their details are **fleet-wide across the whole set** — read
them as diagnostics, not as that IOC's verdict. Those three entries
carry three extra keys that *are* IOC-scoped:

| Key | Meaning |
|---|---|
| `expected` | how many boxes this IOC owns |
| `own_done` | how many of ITS OWN boxes reported `done` |
| `missing_boxes` | up to 10 own boxes that are unaccounted for (present only on failure) |

An IOC is scored on its own boxes alone: a sibling member's success is
never its success (`own_done < expected` → `failed` even when the set's
`deployed` count is high), and a sibling's failure is never its failure
(`own_done == expected` → `done` even when `failed` is non-zero). Own
boxes marked `skipped` are neutral; anything non-terminal counts as
unaccounted. The shared child task itself takes the **worst** of the
three verdicts, so one member missing leaves the child `failed` even
though the other two entries read `done`.

## Kill-a-task

A **KILL** button (`data-testid="task-kill-{id}"`) appears next to VIEW
OUTPUT on any `pending`/`running` row you're allowed to cancel — admins
see it on every such row, other operators only on their own. Clicking it
shows CONFIRM/CANCEL (`task-kill-confirm-{id}` / `task-kill-cancel-{id}`);
CONFIRM calls `POST /c2/tasks/{id}/kill`.

This is a real cancellation, not just a status flip: the backend cancels
the task's underlying `asyncio.Task`, which cascades to every per-box
operation still in flight (including terminating an in-progress NXC
subprocess where feasible) — not merely a UI-side abandonment. A result
that was already in flight when you killed the task is discarded, not
applied — the row stays `cancelled` for good. Killing a `generate`/
`generate_deploy` task that hasn't started compiling yet drops it from
the queue cleanly; one that's already mid-compile is best-effort (Sliver
has no compile-abort RPC), so the row may still take a few seconds to
settle.

## What you'll see

The table (`data-testid="task-queue-table"`) is paginated with a
**LOAD MORE** button at the bottom
(`data-testid="load-more-completed-btn"`). Each row carries:

- ID — autoincrement int (`task-row-{id}`).
- Type — `bulk_exec`, `deploy`, `generate`, `interactive`, …
- Status chip. While a task waits on the global Sliver compile mutex it
  shows `running (queued) · position N`
  (`data-testid="task-queue-position-{id}"`) — N is how many compiles are
  ahead of it. That is normal, not a stall: implant compiles are
  serialized, so a queued task has simply not reached the front of the
  line yet. Once it does, the phase becomes `compiling` / `deploying` and
  the position disappears. See
  [Troubleshooting § implant deploy hangs](troubleshooting).
- Operator — username, or `playbook` for playbook-fired tasks (see
  [Running a competition § playbook](running-competition#playbook)).
- Description (`task-description-{id}`) — a short human-readable
  summary computed server-side at creation time (e.g. "Deploy IOC
  'wmi_event_subscription' to 3 box(es)"); shows `—` for task types
  without a specific template.
- Boxes — count or list, depending on `target_boxes` length.
- Started — relative timestamp.

Scheduled rows (`queued-row-scheduled-{id}`) render above completed
rows and carry an extra countdown chip.

## Troubleshooting

- **Scheduled task doesn't fire** — confirm orchestrator is in
  `Running` state at `/admin` (see [Running a competition](running-competition));
  scheduled tasks created while paused still queue but won't fire
  until resume.
- **Task stuck in `pending`** — check the relevant background pool
  isn't saturated (`BULK_EXEC_MAX_CONCURRENT`,
  `HASH_HARVEST_MAX_CONCURRENT`).
- **Progress modal shows empty per-box list** — `progress_json` is
  per-box; tasks without target boxes (e.g. `generate` with no
  auto-deploy) carry the result in `metadata_json` instead — look at
  the row's tooltip or the raw task JSON via
  `GET /c2/tasks/{task_id}`.
- **`my-tasks` filter hides everything** — your operator username
  doesn't match the `operator` column for any current row.
- **KILL button missing on a task you expect to cancel** — you're not
  the owning operator and not an admin, or the task already left
  `pending`/`running` (it's terminal — nothing left to cancel).

<!-- Updated 2026-07-30 — implant_deploy + generate_pivot task types; queued phase + compile queue-position indicator (docs/PLAN_async_implant_deploy.md). -->
<!-- audited 2026-06-13 — added deploy_all task type + progress_json key-by-ioc_name note (072e12a). testids verified against C2TaskQueuePage.tsx; `task_type` enum verified against database/models.py OperatorTask. Updated 2026-06-26 — added scoring_preflight to type filter. Updated 2026-07-21 — kill button, description column, cancelled status, VIEW renamed to VIEW OUTPUT (docs/PLAN_operator_ux.md § A). Updated 2026-08-10 — pivot-set shared-child scoping: expected/own_done/missing_boxes keys + worst-verdict child status (core/deploy_all.py::_record). -->
