# Frontend

Reference for the C3rberus frontend: tech stack, auth/routing, the page
inventory, shared components, and data flow. Backend counterparts are in
[Architecture](architecture); per-page operator instructions live in the C2
Operations pages ([Overview](c2-overview), [Targets](c2-targets), and siblings).

> **Test-update policy.** Every new page, component, modal, or actionable element MUST land with a Playwright spec covering it (or extending an existing spec). Every actionable element gets a `data-testid="..."` attribute — text-based selectors break the moment copy changes, and xterm uses a WebGL canvas (no DOM text), so testids are non-negotiable for shell modals. See [Testing](testing) for the full policy.

---

## Tech Stack

### Current
- React 19 + TypeScript + Vite
- **Tailwind CSS v4** + **shadcn/ui** (Lyra preset — sharp/boxy, 0px border-radius)
- React Router 7
- Recharts (score history charts)
- **TanStack Query v5** (`@tanstack/react-query`) — all C2 pages use `useQuery`/`useMutation`; scoring pages use plain fetch
- **xterm.js** (`@xterm/xterm@6` + `@xterm/addon-fit@0.11`) — `SessionShellModal` in sessions page
- **SSE** (`EventSource`) — single shared `EventSourceContext` connection (opened on auth token, auto-reconnects); `useSseStatus` hook in `C2Layout.tsx` registers all session/beacon/alert handlers via `subscribe(eventType, handler)`; `AdminLayout` and `OperationsPage` also subscribe to the same context connection
- **WebSocket** — per-session shell in `SessionShellModal`; token passed as query param
- **Sonner** — `<Toaster />` in `main.tsx`; `session-connected`/`beacon-registered`/`session-disconnected` SSE events fire `toast.success()`/`toast.error()` in `C2Layout.tsx`

### Design system
"Tactical Brutalism" — single dark theme, operator-facing. Key tokens:
- Background: `#131313` / Surface: `#1f2020`
- Primary: `#cdcc4e` (olive yellow) / On-primary: `#333200`
- Fonts: Space Grotesk (headings) + JetBrains Mono (labels/code)
- Icons: Material Symbols Outlined (variable font)
- Border-radius: 0px throughout

---

## Auth and Routing

JWT stored in `localStorage`. `AuthContext` exposes user role. Routes are gated by role:

| Role | Accessible routes |
|------|-------------------|
| Blue team | `/`, `/details` |
| Red team | `/`, `/details`, `/iocs`, `/c2/*` (overview, targets, creds, generation, deployment, exec, modules, scheduler, tasks, console, tickets) |
| Admin | All routes including `/admin` |

Unauthenticated users are redirected to `/login`.

### Layout structure

The app uses three layout routes in React Router 7, each rendering its own navbar and an `<Outlet />` for child pages. **No sidebar on scoring/C2 layouts** (top navbar only); the admin layout uses a left-rail sub-nav.

- **`ScoringLayout`** (`react/src/layouts/ScoringLayout.tsx`) — wraps `/`, `/details`, `/iocs`
- **`C2Layout`** (`react/src/layouts/C2Layout.tsx`) — wraps `/c2/*`
- **`AdminLayout`** (`react/src/layouts/AdminLayout.tsx`) — wraps `/admin/*`; left-rail nav splits the old single-page admin into sub-routes (`operations`, `users`, `settings`, `alerts`, `playbook`, `tasks`). `/admin` redirects to `/admin/operations`. Legacy single-page `AdminPage` is still reachable at `/admin/legacy`.

**Scoring navbar** (left side): "C2" link to `/c2` — visible to red team and admin only, hidden for blue team. The Admin link in the scoring navbar points at `/admin/operations`.

**C2 navbar** (left side): "Scoreboard" link back to `/`.

The right side of each navbar contains role-appropriate page links and the current user/logout control. Neither navbar renders inside the other — navigating between the two sections fully swaps the layout.

---

## Current Pages

### `/login` — Login
Simple username/password form. On success, stores JWT and redirects to `/`.

### `/` — Dashboard (Scoreboard)
- **Scoreboard table**: ranks all blue teams by `score_pct` (percentage of possible points earned, `data-testid="score-pct-{team_num}"`). Columns: rank, team name, score % (primary), raw score / possible points (`data-testid="possible-pts-{team_num}"`), last check time. Teams with no active IOCs (zero denominator) show `—` for score %.
- **Score history chart** (`ScoreChart.tsx`): Recharts line chart showing score over time per team.

Visible to all roles. Blue team users see all teams on the scoreboard (it's a competition).

### `/details` — IOC Details
Table of IOC check results per box. Columns: team, box IP, IOC name (obscured for blue team users as `<OS>_<DIFFICULTY>_<NUM>`), difficulty, status (remediated/compromised/error), points. Filter input (`data-testid="details-filter-input"`) narrows by IOC name or box IP. CSV download button exports visible rows.

Red team and admin see a **team selector dropdown** in the page header to filter by team (or view all). Blue team always sees their own team only with no filter control shown. Blue team also sees a **scoring summary panel** at the top (`data-testid="own-score-pct"`, `data-testid="own-rank"`) showing their current score percentage and rank.

### `/iocs` — IOC Definitions
Table of all IOC definitions. Columns: name, description, OS, difficulty, check script, deploy script. Red team and admin only.

Includes an **IOC deploy dialog** (`IOCDeployDialog.tsx`) for selective IOC deployment — choose IOC names and optionally target specific boxes.

### `/admin` — Admin Panel
Admin only. Renders under `AdminLayout` with a left-rail sub-nav splitting the original single-page admin into discrete routes:

- `/admin/operations` (default) — Competition control: start, pause, unpause, stop. Run checks / SA collection manually. Reset all competition data. Status display: state, last check time, worker count. Destructive actions require a confirm dialog.
- `/admin/users` — Table of all users with role badges. Actions: add, change password, remove. Remove requires confirm dialog.
- `/admin/settings` — Runtime-configurable values wired to `PATCH /admin/settings` (allowlist enforced by `_MUTABLE_SETTINGS` in `routes/admin.py`).
- `/admin/alerts` — Alert history viewer.
- `/admin/playbook` — Playbook YAML preview + register/cancel controls.
- `/admin/tasks` — Scheduled-task / operator-task admin view.
- `/admin/legacy` — original single-page admin retained for fallback.

---

## C2 Pages

All C2 routes (`/c2/*`) are accessible to red team and admin only.

### `/c2` — C2 Overview
At-a-glance health of all teams. Displays a grid of team cards — one card per blue team.

**Each team card contains:**
- Team name/number
- **Color health stripe**: green (all beacons live + all creds valid + no flags) / amber (partial issues) / red (no beacons or majority of creds broken)
- **Current score** — lets operators prioritize teams that are remediating quickly
- **Beacon count**, **Cred count**, **Active flags badge** — each clickable for detail dialog
- **Mini box strip**: colored squares per box encoding combined beacon + cred status
- **Last SA collection** timestamp

**Live updates**: SSE (`GET /c2/events`) pushes beacon/session events. Data sources: `GET /c2/boxes`, `GET /c2/sa`, `GET /scoreboard`.

Network map (`NetworkMapView.tsx`) highlights pivot connections — purple lines when a box has an active pivot session. `pivotBoxIPs` computed from `GET /c2/pivot-sessions` rows and live sessions with `->` in `remote_address`.

### `/c2/targets` — Targets (merged Implants / Sessions / Beacons / SA)
Merged live-implant + SA view grouping sessions, beacons, and situational awareness by box IP. All of `/c2/implants`, `/c2/boxes`, `/c2/sessions`, `/c2/beacons`, and `/c2/sa` redirect here.

**Box group row**: session count, beacon count, OS icon, team number, PIVOT badge when pivot session active, **TERMS** column with a live count of operators currently attached to that box via a WebSocket OS shell or Sliver shell. Cell testids: `terminal-presence-{box_ip}` (count > 0, hover reveals `operator (session_type, HH:MM)` per line) and `terminal-presence-empty-{box_ip}` (no active terminals). Live via the SSE event `terminal_presence_update`; backend gates both the SSE and `GET /c2/terminal-presence` on `require_non_blue`. In-memory registry cleared on backend restart (docs/PLAN_targets_terminal_presence.md). Expanding a box reveals two sub-sections:
- **IMPLANTS** (open by default): sortable/filterable implant table with per-implant action buttons (exec, upload, download, shell, kill). Group-level action buttons auto-select best implant: pivot session > regular session > beacon, most-recently-checked-in first.
- **SITUATIONAL AWARENESS** (closed by default): full SA tab bar (users, processes, ports, history, auth_log, etc.) lazily fetched on first expand, polled every 60s. "Run SA Now" button per tab calls `POST /c2/sa/{box_ip}/collect`.

Filters: search by IP/name, OS (`ALL OS / WINDOWS / LINUX / FIREWALL`), `PIVOT ONLY`, team selector (`data-testid="team-filter"`), implant-type toggle `ALL / SESS / BCN / NONE` (`data-testid="implant-filter-{value}"`). Offline boxes render at `opacity-40` with `data-offline="1"`.

### `/c2/generation` — Implant Generation
Generate Sliver implants and manage the implant library.

**Generate section:**
- OS, architecture, format, beacon/session toggle, beacon interval, C2 URL, persistence selector. Persistence-mechanism dropdown labels are the hand-written `PersistenceMechanism.label` field (audited by `tests/test_persistence_labels.py`), not the Python attribute name.
- Auto-deploy targets: box selector filtered to the selected OS. "Select All" header checkbox with indeterminate state, `N/Total` count badge.
- **Auto-difficulty badge** (`data-testid="difficulty-badge"`): computed from the selected mechanism's `tier` plus storage-mode and protection weights (`react/src/lib/difficultyFormula.ts` mirrors `fastapi_backend/core/difficulty_formula.py`; parity locked by `tests/test_difficulty_formula.py`). The **Override** button (`difficulty-override-toggle`) exposes a manual selector (`difficulty-override-select`); the override is a *one-shot* — any change to mechanism, storage mode, or a protection toggle clears it and re-shows the computed value, and the auto value is echoed as `(Auto: X)` (`difficulty-override-hint`). Only consumed on the INSERT path for auto-registered `DeployedIOCs` rows — existing rows keep their scored difficulty (blue-team scoring correctness). See docs/PLAN_generate_page_labels_autodifficulty.md.
- GENERATE button returns 202 + task_id; `DeployProgressPanel` polls `GET /c2/tasks/{task_id}` and renders per-box results + summary.

**Implant library**: previous builds table with download links and metadata.

### `/c2/deployment` — IOC Deployment
Register and manage deployed IOCs (`DeployedIOCs` rows).

- **DEPLOY & REGISTER** (`data-testid="deploy-and-register-button"`): calls `POST /c2/iocs/deploy-and-register` (async 202); runs IOC's effect deploy script via NXC and creates row on `status=1`. Disabled for SliverCheck IOCs or IOCs without effect deploy script.
- **REGISTER** (without running deploy): calls `POST /c2/iocs/deploy` (async 202); creates row without running any script.
- Both accept `box_ips: list[str]` and surface progress via `DeployProgressModal` polling `GET /c2/tasks/{task_id}`.
- **Active registrations table**: view and deregister via `DELETE /c2/iocs/deployed/{id}`.
- **Failed Deploys section** (`data-testid="failed-deploys-section"`): IOCs that never fully landed (`status='deploy_failed'`). **Hidden entirely when there are none** — it only renders once `GET /c2/iocs/deployed?status=deploy_failed` returns rows. These rows are *unscoreable*, so this section is the operator's queue of outstanding work, not a list of deployed IOCs.
  - Click a row (`failed-deploy-toggle-{id}`) to expand the recorded failure message (`failed-deploy-error-{id}`) plus the task number to open in the task queue for per-box progress.
  - **REDEPLOY** (`redeploy-button-{id}`) / **REDEPLOY ALL** (`redeploy-all-button`) call `POST /c2/iocs/deployed/{id}/redeploy`, which retries the whole IOC under its **original pooled names** — a redeploy that re-picked from the pool would orphan artifacts the failed attempt already planted. Result surfaces in `redeploy-message` and the shared deploy progress panel.
  - Deploys auto-retry `DEPLOY_MAX_ATTEMPTS` times (default 3) before landing here, and raise one `redeploy_failed` alert per dead deploy. See `docs/PLAN_failed_deploy_redeploy.md`.

### `/c2/creds` — Credential Management (tabbed)
Three tabs cover every credential surface: **PASSWORDS**, **HASHES**, **TICKETS**. Tab state persists to `localStorage.creds_active_tab`; defensive parse falls through to `'passwords'` on corrupted/unknown value. Tab testids: `creds-tab-passwords`, `creds-tab-hashes`, `creds-tab-tickets`. Each header shows a row count.

- **PASSWORDS** — `cred_type === 'password'` (or null legacy) rows. Top action `[+ ADD_CREDENTIAL]` (`add-cred-btn`) opens slide-in unified ADD form. Per-row: test (`network_check`), show/hide password (eye), delete.
- **HASHES** — `cred_type === 'nthash'` rows. Top action `[DUMP_FROM_BOX]` (`dump-from-box-btn`) opens dropdown of Windows boxes; submit fires async `POST /c2/harvest/{box_ip}` (202 → `task_id`) and surfaces a sonner toast with VIEW action linking to `/c2/tasks`. No per-page modal — concurrent dumps stack in the Tasks page. Per-row: test, delete. **No eye-icon** (no plaintext to reveal).
- **TICKETS** — Kerberos golden-ticket rows from `GET /c2/tickets`. Top action `[FORGE_TICKET]` (`forge-ticket-btn`) opens inline form `{team_num, principal}`. Per-row: VALIDATE (`ticket-validate-{id}`, validates the ticket against all team boxes — Windows SMB + domain-joined Linux SSH-GSSAPI — via `POST /c2/tickets/{id}/validate`; single-box picker `ticket-validate-box-select-{id}`; results sub-row `ticket-validate-results-{id}`), REFRESH (`ticket-refresh-{id}`), REVOKE (`ticket-revoke-{id}`).

**Unified ADD form** (PASSWORDS tab): TYPE dropdown (`cred-type-select`) toggles between `password` (password input) and `nthash` (nthash hex input + domain + team_num). nthash validated by `^[0-9a-fA-F]{32}$` regex client-side (`nthash-input`, error chip `nthash-error`); same regex enforced backend-side via Pydantic `model_validator` on `CredentialCreate`. Auto-tab-switch on add success: `cred_type='nthash'` → HASHES tab, otherwise PASSWORDS. FORGE success switches to TICKETS.

Shared filter bar above the table; empty-state copy per tab (`creds-empty-{tab}`). Common buttons in header: TEST_ALL, PURGE_INVALID. VIA-IOC badge (`cred-via-ioc-badge`) marks auto-registered rows.

### `/c2/exec` — Bulk Command Execution (netexec)
Credential-based bulk execution without an implant. Two tabs: **Execute** (command + target + cred selection) and **Put-file** (file upload → push to targets). Two-stage progress for put-file.

### `/c2/modules` — Module Runner
Module execution via Sliver sessions. Module list from `GET /c2/modules`. Per-module run form; results via task poll.

### `/c2/tasks` — Tasks (sectioned: QUEUED / RUNNING / COMPLETED)
Single page consolidating the legacy Task Queue + Scheduler. Three collapsible sections, each pinned to a status subset:

- **QUEUED** (`section-queued`) — merges `OperatorTask(status='pending')`, `GET /c2/scheduled?status=pending`, and admin-only `GET /admin/scheduled/events` rows into one list sorted by `fire_at ASC`. `fire_at` = `run_at` for scheduled-tasks / events, `created_at` for OperatorTasks. Unified row testid `queued-row-{kind}-{id}`. Non-admin operators see no events block (count differs).
- **RUNNING** (`section-running`) — `OperatorTask(status='running')`. Expanded by default. Table testid `task-queue-table`, row testid `task-row-{id}`.
- **COMPLETED** (`section-completed`) — `OperatorTask(status IN done|failed)`, page-0 polled every 5s and merged with accumulated LOAD MORE pages by id-dedupe. LOAD MORE button (`load-more-completed-btn`) when last page returned 500 rows.

**Top action**: `[+ NEW SCHEDULED TASK]` (`new-scheduled-task-btn`) opens an overlay modal (`new-scheduled-task-modal`) carrying the create-task form (`task-name-input`, `task-command-input`, `task-target-type`, `task-run-at`, `task-repeat-secs`, `task-schedule-btn`). Backdrop click and Escape both close.

**Section expand/collapse** persists to `localStorage.tasks_section_expanded` JSON. Defensive parse: malformed JSON or wrong shape falls through to `{queued:false, running:true, completed:false}`.

**Filters** (above sections): `task-status-filter` (PENDING/RUNNING/DONE/FAILED), `task-type-filter` (includes `harvest`, `exec_single`, `bulk_exec`, `bulk_put_file`, `scheduled`, plus the legacy generate/deploy/module options), and `my-tasks-btn`. Hide-on-mismatch: selecting `status=done` hides QUEUED + RUNNING; `status=running` hides QUEUED + COMPLETED. Counts and rendered sections collapse to the subset that matches.

Backend `GET /c2/tasks` accepts repeated-query `status` and `task_type` plus `offset` for pagination — single-value still works (`?status=done` binds to `['done']`).

### `/c2/console` — Sliver Console
Operator interactive `sliver-client` terminal. "OPEN CONSOLE" button (`data-testid="open-console-btn"`) opens `SliverConsoleModal` via `TerminalContext.openTerminal()`. Button label: OPEN CONSOLE / RESTORE CONSOLE (minimized) / CONSOLE ACTIVE (open). Badge `data-testid="console-active-badge"` when active.

---

## Shared Components

### ScoringLayout + scoring navbar
Wraps scoring pages (`/`, `/details`, `/iocs`). Navbar contents:
- Left: "C2" link to `/c2` — red team and admin only, not rendered for blue team
- Right (all roles): Scoreboard, Details
- Right (red team + admin): IOCs
- Right (admin only): Admin (`/admin/operations`)
- Far right: current username + logout

### C2Layout + C2 navbar
Wraps all C2 pages (`/c2/*`). Navbar contents:
- Left: "Scoreboard" link to `/`
- Right (in order): TARGETS, EXEC, GENERATION, MODULES, TASKS, CONSOLE, IOC MGMT, CREDS. The C2 overview (index route `/c2`) is reachable via the OVERVIEW link rendered above the flat nav block (testid `nav-link-overview`).
- **SSE live indicator**: pulsing green dot + "Live" label; turns grey ("Disconnected") when SSE drops; auto-reconnects.
- Far right: current username + logout

**Terminal tab bar** (fixed bottom-0): persistent across page navigation. Each open terminal = one tab. Tab icons: `terminal` (OS shell), `webhook` (Sliver shell), `terminal_settings` (Sliver console). Tab labels: `{box_ip}` (OS), `[S] {box_ip}` (Sliver), `[C] Console`.

**Split pane**: two terminals can be pinned to left/right panes via `[L]`/`[R]` tab buttons. Horizontal split stacks terminal area + page `<Outlet>` as top/bottom halves; swap button inverts position. Resize via drag handles (`data-testid="split-handle-vertical"` / `"split-handle-horizontal"`). Ratios persisted to `sessionStorage`.

### ProtectedRoute
Wraps routes with role check. Redirects to `/login` if unauthenticated, `/` if insufficient role.

### Toast / notification system ✅ DONE
Sonner integrated. `<Toaster position="bottom-right" theme="dark" richColors />` in `main.tsx`. `session-connected` → `toast.success("New session from {ip}")`, `beacon-registered` → `toast.success("New beacon from {ip}")`, `session-disconnected` → `toast.error("Session lost: {ip}")` in `C2Layout.tsx` `es.onmessage` handler.

### Confirm dialog
Modal confirmation for destructive actions: reset competition data, stop competition, remove user, delete credential. Shared component to avoid reimplementing per page.

### Status badge
Consistent pill-shaped badge with color coding. Two variants:
- **Cred status**: green (valid) / red (invalid) / grey (unknown)
- **Beacon status**: green with pulsing dot (live) / amber (dead) / grey (none)

Used on the C2 overview, creds page, beacons page, and box selector.

### SA timestamp staleness rule
Used in three places: overview card footer, boxes table Last SA column, SA page panel header. Consistent color rules:
- **zinc-400** (muted) — collected within the last 30 minutes
- **amber** — 30–60 minutes ago
- **red** — over 1 hour ago

Display format: relative time ("3m ago", "47m ago", "2h ago").

### Command output display
Reusable component for showing per-host command results. Displays hostname, success/failure indicator, and output text in a monospace block. Used on bulk exec results, beacon task results, and session exec output.

### Session shell modal ✅ DONE
xterm.js terminal mounted in a full-screen overlay modal (`SessionShellModal` in `C2SessionsPage.tsx`). Connects via WebSocket on open, handles 4003 (unauthorized) and 4009 (session in use) close codes, disposes terminal and closes socket on close.

### Box selector
Reusable component for selecting target boxes (used in bulk exec, implant generation, etc.). Supports select-all, filter by team, filter by OS. Accepts an optional `osFilter` prop to restrict shown boxes to a specific OS (used by implant generation to match implant OS to target boxes).

---

## Data Flow

### React Query setup
- All API calls wrapped in React Query hooks (`useQuery`, `useMutation`)
- Auth token injected via a shared query client default header
- Polling intervals: scoreboard (30s), boxes overview (15s), beacon tasks (3s until complete)

### SSE event feed
A single `EventSource` connection to `GET /c2/events` is shared across the app via `EventSourceContext` (one connection per browser tab). `C2Layout`'s `useSseStatus` hook subscribes the session/beacon/alert handlers onto this shared connection via `subscribe(eventType, handler)` (returns unsubscribe, called on unmount). Events are named (`event: session-connected`, `event: alert-created`, etc.); the context registers a native `addEventListener` per event type and also keeps an `onmessage` fallback for unnamed-but-typed frames (mocked tests). Event types and their effects:
- `session-connected` → invalidate `sessions` query, fire toast
- `beacon-registered` → invalidate `beacons` + `boxes` queries, fire toast
- `session-disconnected` / `session-removed` → same
- `alert-created` → invalidate `alerts` query, severity-gated toast

**Auth note**: `EventSource` does not support custom headers — the JWT cannot be sent as `Authorization: Bearer ...`. The token must be passed as a query parameter: `GET /c2/events?token=<jwt>`. The backend endpoint must accept and validate the token from the query param instead of the header. This affects the FastAPI route implementation.

### WebSocket shell
Opened per-session on demand. `useWebSocket` hook manages connection lifecycle. Disconnects automatically when the shell drawer closes.

<!-- audited 2026-07-30 — created from docs/FRONTEND.md lines 1-250 (tech stack, auth/routing, page inventory, shared components, data flow), verbatim. The trailing 10-item UX improvements backlog was dropped as stale: it critiqued a BOXES/SESSIONS/BEACONS/SA nav that no longer exists (those merged into /c2/targets) while stating "no changes have been made yet". Recoverable from git history; live UX work tracked in docs/PLAN_frontend_ux.md + docs/PLAN_operator_ux.md. -->
