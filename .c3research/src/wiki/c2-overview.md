# Overview

The C2 landing page: per-team network map, live alert stream, and
high-level competition state at a glance.

## When to use

- At competition start — confirm every box is up, every team is
  reachable, sessions/beacons start coming in.
- Any time the scoreboard moves unexpectedly — the network map shows
  which team had something happen.
- After every backend restart — confirm Sliver, SA, and the alert
  pipeline came back up clean.

For per-box deep-dives, jump to [Targets](c2-targets). For score
mechanics, see [Scoreboard reading](scoreboard-reading).

## Prerequisites

- Logged in (any non-blue role).
- Inventory loaded; otherwise the map renders empty.

## Network-map

`NetworkMapView` (`react/src/components/NetworkMapView.tsx`) renders
each team as a column anchored on its pfSense firewall, with internal
boxes branching off. Per-box info:

- OS icon (`OsIconSvg`).
- Live session / beacon counts.
- NXC-degraded flag (orange badge, sourced from
  `nxc_failure_tracker.degraded_box_ips()`).
- Alert badges (`data-testid="box-alert-badge"`) — click for a
  popover (`data-testid="box-alert-popover"`) listing the last few
  alerts.

The map polls `/c2/pivot-sessions` every 60s; SSE refresh on
`session-connected` / `session-disconnected` / `beacon-registered` /
`beacon-disconnected` events triggers an immediate re-fetch.

## Pivot-highlight

Connection lines between a box and its team's pfSense are highlighted
**purple** when that team has an active pivot (per
`pivotBoxIPs: Set<string>` in `C2OverviewPage.tsx`). Two sources feed
`pivotBoxIPs`:

1. DB-stored `PivotSessions` rows via `GET /c2/pivot-sessions`.
2. Live Sliver sessions whose `remote_address` contains `"->"` (TCP
   pivot indicator).

Once any team has an active pivot, every internal (non-firewall) box
for that team gets highlighted as reachable via the pivot.

## Alerts

Bottom-right floating `AlertButton` shows the unread count. Clicking
it opens `AlertHistoryPanel` (slide-in from the right) with the alert
history table.

Alert sources:

- Session / beacon disconnect (Sliver event handlers).
- Cred validation failures
  (`orchestrator._revalidate_all_creds`).
- Beacon stale detection
  (`beacon_watcher.py`).
- Redeploy failures.
- Box unreachable via NXC failure tracker (`nxc_failure_tracker.py`) or
  nmap ping sweep (`nmap_health.py`).
- Pivot session drops (`sliver_manager.py` teardown).

Severity mapping (from `c2/alerts.py`):

- **info** — informational (no toast).
- **warn** — orange toast.
- **critical** — red toast.

Toasts fire from the SSE `alert-created` frame on `/c2/events`. Only
`warn` and `critical` trigger toasts; `info` is queued silently into
the history panel.

Admin-only deeper view: `/admin/alerts` (`AlertsPage.tsx`) renders
the same alerts as a filterable table with severity / kind / team
filters.

## What you'll see

- Top-of-page summary chips: live sessions, live beacons, reachable
  boxes, alerts (last hour), pivot sessions.
- Network map columns — one per team, sorted by team number.
- Alert button (bottom-right) — pulses red when an unread `critical`
  arrived in the last 10s.
- SSE "LIVE" indicator in the left nav glows green when the
  `/c2/events` stream is connected.

## Troubleshooting

- **Map renders empty** — `getBoxes()` returned `[]`; check inventory
  loaded and orchestrator is past the **Init** state.
- **No purple pivot lines** despite a live pivot — confirm the
  `PivotSessions` row exists (`GET /c2/pivot-sessions`) AND the
  Sliver session's `remote_address` carries `"->"` for TCP pivots.
- **Alerts never appear** — the SSE indicator in the nav should be
  green. If red / orange, the `/c2/events` connection dropped;
  reload the page.
- **Old alerts stick around** — the background sweep purges older
  than 24h every 10 minutes; manual cleanup via DB if needed.
- **Sessions flap rapidly** — Sliver server is reconnecting; tail
  `~/.sliver/logs/` for the gRPC client state.

<!-- audited 2026-06-05 — pivot highlighting + SSE alert flow verified against C2OverviewPage.tsx pivotBoxIPs derivation and c2/alerts.py severity mapping. -->
