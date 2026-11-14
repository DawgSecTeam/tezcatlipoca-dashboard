# Targets

Per-box command surface: sessions, beacons, situational-awareness data,
and per-box actions (exec, upload, download, shell, kill) — all grouped
by box IP.

## When to use

- Pull up everything you know about one box.
- Open an interactive shell.
- Force a fresh SA sweep.
- Audit live implant state across the fleet.

For ad-hoc commands across many boxes, [Exec](c2-exec) is faster.
For interactive Sliver CLI, [Console](c2-console) drops you into a full
`sliver-client` PTY.

## Prerequisites

- Logged in as red / white / black / admin (blue team blocked).
- Inventory populated.
- For SA expand: at least one prior collection cycle, OR click
  **Run SA Now** (manual trigger).

## Filters

The header row carries every filter the page supports:

- **Search** (`data-testid="implants-search-input"`) — matches box IP or
  hostname substring.
- **OS** — `ALL OS / WINDOWS / LINUX / FIREWALL`.
- **Team filter** (`data-testid="team-filter"`) — restrict to one team.
- **PIVOT ONLY** — only boxes with an active pivot session.
- **Implant filter** (`data-testid="implant-filter-{value}"`) — toggle
  `ALL / SESS / BCN / NONE`.
- **Clear filters** (`data-testid="clear-filters"`).

Filter state lives in the URL query string so the view is shareable.

## Group actions

Each box collapses into a row (`data-testid="group-row-{box_ip}"`). The
row carries group-level action buttons that auto-pick the best live
implant per the resolution rule **pivot session > regular session >
beacon, most-recently-checked-in first**.

Buttons exposed at the group row:

- **EXEC** (`data-testid="targets-exec-{box_ip}"`) — opens the
  TargetsExecDrawer (pre-fills box, asks for command + dispatch
  method).
- **PUT_FILE** (`data-testid="targets-putfile-{box_ip}"`) — same drawer
  in put-file mode.
- **SHELL** (group action) — see [#shell](#shell).
- **KILL** — terminate the auto-picked session/beacon (inserts a
  `PendingKills` row first so the disconnect handler distinguishes
  intentional kill from organic loss).

Offline boxes render at `opacity-40` with `data-offline="1"`; group
actions are disabled until a live implant returns.

## SA-tabs

Expand a box to reveal **SITUATIONAL AWARENESS**. First expand triggers
a lazy fetch of all SA tabs; subsequent expands re-use cached frames
(re-fetched every 60s). Available tabs depend on OS but cover at
minimum:

- **users**, **processes**, **ports**, **history**, **auth_log**,
  **config_changes**, **sudo_rules**, **auth_keys**, **nmap**.
- **SELinux/AppArmor** (`mac_status`, Linux only) — SELinux mode
  (`getenforce`/`sestatus`) and AppArmor status (`aa-status`), each
  presence-guarded so a distro with neither shows "not installed"
  instead of an error.
- **sshguard** (firewall only) — whether pfSense's `sshguard` is
  running plus its current pf block-table entries. A
  `data-testid="sshguard-host-blocked-badge"` appears when the
  C3rberus host's own IP (`config.C2_IP`) is in the table — see
  [troubleshooting.md § pfSense sshguard](troubleshooting.md#pfsense-sshguard-blocking-the-c3rberus-host).
- Per-tab search field (`data-testid="sa-search-{tab}"`).
- **Processes** tab has a `HIDE SYSTEM` toggle
  (`data-testid="sa-hide-system-toggle"`, default **on**,
  persisted in `localStorage`) that filters out processes whose
  owner isn't one of the box's currently logged-in interactive
  users (cross-referenced against the **users** tab of the same SA
  cycle). Windows processes owned by `SYSTEM`/`NETWORK SERVICE`/
  `LOCAL SERVICE` are always treated as system.

Each tab carries a header **CollectedAt** timestamp, a delta-badge
(`data-testid="sa-changes-badge-{box_ip}"`) showing additions /
removals against the prior cycle, and a **Run SA Now** button (see
[#run-sa-now](#run-sa-now)).

## Run-sa-now

The header **Run SA Now** button (`data-testid="run-sa-now-btn"`) on
each SA section fires `POST /c2/sa/{box_ip}/collect`. Concurrent
operator triggers are **coalesced per box** — two clicks on the same
box at the same instant share one NXC subprocess and one DB batch via
the `_sa_in_flight` table in `routes/c2.py`. Different boxes run in
parallel.

The cycle:

1. Click **Run SA Now**.
2. The button greys until the SA cycle finishes (usually 5–30s).
3. New "CollectedAt" timestamp appears; the SA tabs re-render with
   fresh data; the changes badge updates.

## Shell

The group **SHELL** button opens an xterm pane in the bottom-of-screen
terminal tab strip. Two flavours:

- **OS shell** (`SessionHub` — `c2/session_hub.py`) — wraps non-`.exe`
  Windows commands in `powershell.exe -NonInteractive -Command <cmd>`
  so cmdlets work; concrete executables called directly.
- **Sliver shell** (`SliverHub` — `c2/sliver_hub.py`) — direct
  text-dispatch protocol via Sliver gRPC; supports `ps`, `ls`, `pwd`,
  `cat`, `cd`, `whoami`, `ifconfig`, `netstat`, `getsystem`, `migrate`,
  `socks5`, `portfwd`, `upload`, `download`, `execute`,
  `execute-assembly`, `mv`, `current-token-owner`. Anything else falls
  back to `interact.execute()`.

Both hubs fan out — multiple operators can join the same shell. New
joiners get a rolling 200-line history replay. `client_count` frames
fired on join/leave. `exit` disconnects only the sender. Hubs torn
down 5 minutes after the last client leaves.

The terminal tab strip carries `[L]` / `[R]` buttons (appear at 2+
open terminals) to assign a tab to the left/right split pane — or
drag a tab/header onto either half of the screen, or use the
keyboard chord (`Ctrl+Shift+L`/`R` then a digit). Full mechanics,
including horizontal split and copy/paste, on
[Keyboard Shortcuts § terminal-split-panes](keyboard-shortcuts#terminal-split-panes).

## Beacon-interactive

Each beacon row in the expanded **IMPLANTS** table carries an
**interactive** button (`data-testid="beacon-interactive-{beacon_id}"`,
`sensors` icon) alongside its task/kill controls. It issues Sliver's
`OpenSession` beacon task — a raw gRPC call, since no dedicated
sliver-py wrapper exists for "spawn a session from this beacon" — which
queues on the beacon and only actually connects on its **next
check-in**, not immediately. A toast confirms the request was accepted,
with a **VIEW** action linking to the Task Queue filtered to
`task_type=interactive`.

The resulting session shows up the same way any organic session
connect does — in the box's session list once the beacon checks in and
the connection completes, auto-registered as a scored `DeployedIOCs`
row exactly like a fresh implant deploy. There's no separate "linked
session" UI concept to learn; it's just a normal session that happened
to originate from an existing beacon instead of a new binary.

## What you'll see

- Group rows for every box that matches the filter set.
- Per-box counts: sessions, beacons, PIVOT badge when a pivot session
  is active.
- OS icon next to the name (`OsIconSvg` — windows/linux/firewall).
- Expanded view: collapsible **IMPLANTS** table (open by default) +
  **SITUATIONAL AWARENESS** section (closed by default).
- Live SSE refresh on `session-connected` / `session-disconnected` /
  `beacon-registered` / `beacon-disconnected` events.

## Troubleshooting

- **No groups shown** — clear filters; the URL may still encode a stale
  filter from a previous session.
- **"Run SA Now" greys forever** — check `logs/fastapi_backend_<ts>.log`
  for the box IP; NXC reachability is the usual culprit.
- **Group action buttons missing** — box is offline (`data-offline="1"`,
  `opacity-40`). Restore network reachability or generate a fresh
  implant first.
- **Shell terminal blank** — session likely died between open and
  first frame. Close the tab and re-open from the live group row.
- **`group_count` keeps flipping** — Sliver client is reconnecting;
  check `/admin/status` for the gRPC client state.
- **Interactive button click does nothing visible** — this is expected
  for a few seconds to a full beacon check-in interval; the RPC only
  queues the task. Watch the Task Queue (`task_type=interactive`) or
  the box's session list rather than the button itself.

<!-- audited 2026-06-05 — testids verified against C2ImplantsListPage.tsx (group-row-, targets-exec-, targets-putfile-, sa-search-, run-sa-now-btn, implant-filter-, team-filter, clear-filters). SA coalescing claim verified against routes/c2.py `_sa_in_flight`. Updated 2026-07-21 — beacon-interactive section, docs/PLAN_operator_ux.md § D. -->
