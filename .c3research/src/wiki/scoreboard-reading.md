# Scoreboard reading

The scoreboard reflects red-team success against each blue team in
real time. This page covers how the numbers are computed, how to
read the columns, and how the views differ by role.

## When to use

- During a competition — watch for score movement, identify which
  team red just popped.
- Post-comp — pull the history graph for the after-action review.
- Debug: a team appears to be losing points without explanation —
  what changed in the last cycle?

## Prerequisites

- Logged in. The scoreboard is one of the very few non-blue-blocked
  routes — blue teams see their own data with obscured IOC names.

## Columns

Each row is one blue team. Columns:

- **Team** — number + name.
- **Score %** (`data-testid="score-pct-{team_num}"`) — primary sort
  key. `ROUND(100.0 * total_score / total_possible_points, 1)`.
  `0.0` when no IOCs registered for the team yet (denominator zero).
- **Raw score** (`data-testid="raw-score-{team_num}"`) — secondary
  display (`total_score`).
- **Possible pts** (`data-testid="possible-pts-{team_num}"`, admin
  only) — sum of base points across active `DeployedIOCs` rows for
  the team.
- **Box column** — shows the last IP octet (`.XXX`) from
  `team.box_stats` keys.

Sort is fixed: score-pct descending. The view does not allow user
re-sort by other columns.

## Table-vs-graph

The TABLE/GRAPH toggle persists to `localStorage` under
`scoreboard_view` (`'table' | 'graph'`, default `'table'`):

- **TABLE** (`data-testid="toggle-table"`) — 7/12 column layout (table)
  + 5/12 column layout (per-team IOC chart). Default view.
- **GRAPH** (`data-testid="toggle-graph"`) — expands the score chart
  to full-width (`col-span-12`).

The toggle survives backend restarts since it's pure client state.

## History

The score history graph is driven by the SQL view `score_history`
(see `database/`):

- One row per (`check_instance_id`, `team_num`).
- Carries `total_score`, `total_possible_points`, `score_pct`,
  `created_at`.

Recharts renders a multi-team line graph; hovering exposes the per-
check-instance score per team.

`score_history` also drives the IOC chart on the right (`IOCChart`
component) which shows per-IOC contributions over time.

## Obscured-labels

When a blue-team account loads the scoreboard, the data already
flows through obscuring helpers (`assign_blue_team_label()` in
`routes/ioc_deploy.py`):

- Real IOC names (`firewall_disable_fw_rules`,
  `windows_lsa_protection_off`, …) → opaque labels
  (`FIREWALL_HARD_1`, `WINDOWS_EASY_2`, …).
- Categories (`persistence`, `users`, `remote_access`) are still
  visible — blue teams need to know *what kind* of compromise to
  look for.

Admin and red team always see real IOC names. The mapping for each
team's labels is stable per `DeployedIOCs` row lifetime — the same
deployed IOC always renders with the same label.

## Scoring math (split-scoring)

IOCs score in three parts per `IOCCheckResult` row:

- `IOC_PARTIAL_SCORE_PCT`% — effect present (default 40).
- Same percentage — persistence present (default 40).
- `IOC_COMPLETION_BONUS_PCT`% — both present (default 20).

Multiply by base points by difficulty (defaults, env-overridable via
`DIFFICULTY_EASY_POINTS` / `DIFFICULTY_MEDIUM_POINTS` /
`DIFFICULTY_HARD_POINTS` / `DIFFICULTY_ULTRA_POINTS`):

- Difficulty 1 (Easy) — 5 pts.
- Difficulty 2 (Medium) — 10 pts.
- Difficulty 3 (Hard) — 20 pts.
- Difficulty 4 (Ultra) — 40 pts.

Example: a Medium-tier IOC with effect-only detected scores
`10 * 0.4 = 4 pts`; with both effect + persistence,
`10 * (0.4 + 0.4 + 0.2) = 10 pts`.

The view `ioc_check_result_with_points` does this calculation in SQL
so the scoreboard query is one read.

## What you'll see

- Live updates — `score_history` is computed at every check cycle,
  re-rendered via SSE refresh.
- Sticky team-row colour: each team carries a deterministic hue so
  the same team has the same colour across reloads.
- IOC chart per team (right side in TABLE view): horizontal bars per
  IOC, with split colours (effect/persistence/completion).

## Troubleshooting

- **`score_pct=0.0` for every team** — no `DeployedIOCs` rows with
  `status='active'`. Register at least one IOC via
  [Deployment](c2-deployment) or generate a SliverCheck implant via
  [Generation](c2-generation).
- **Team's score plummets between cycles** — they remediated. Look at
  the IOC chart's most recent column — green bars (status=0) appear
  where there were red bars (status=1).
- **Blue team sees wrong obscured labels** — the labels are assigned
  per `DeployedIOCs.id` at deploy time and stored on the row; check
  `DeployedIOCs.blue_team_label` directly in the DB.
- **History graph empty** — no historical `check_instance` rows; the
  competition just started, or the DB was reset.

<!-- audited 2026-06-05 — column testids, split-scoring constants (IOC_PARTIAL_SCORE_PCT, IOC_COMPLETION_BONUS_PCT), and view names verified against wiki/architecture.md + react/src/components/scoreboard.tsx. Updated 2026-06-26 — base points corrected to 5/10/20/40 + Ultra (difficulty 4) tier per config.py DIFFICULTY_*_POINTS. -->
