# C3rberus Operator Wiki

C3rberus is a red-team scoring system for cyber-defense competitions (CDEs). It
auto-detects red-team compromise of blue-team systems via IOC checks against
target boxes (NetExec over SMB / SSH) and produces a live scoreboard.

This wiki is the reference for both audiences: operator-facing pages (how to
drive the C2 surface, what each page does, how to recover from common failures)
and the developer-facing set below ([Architecture](architecture),
[Subsystems](subsystems), [Frontend](frontend), [Testing](testing),
[Configuration](configuration)). The repo's `CLAUDE.md` stays the top-level
working guide for anyone changing the code.

## Quick start

```bash
git clone <repo>
cd C3rberus
scripts/setup.sh        # install Python + Node + pre-commit hooks
scripts/run.sh          # start backend (:3000) + frontend (:5173)
```

API docs: <https://localhost:3000/docs> · UI: <https://localhost:5173>

## Where to go

**Start here if you're competing:**

- **[Operator Actions](operator-actions)** — every supported red-team
  action with step-by-step recipes.
- **[Keyboard Shortcuts](keyboard-shortcuts)** — every global and
  page-scoped keybind, including terminal split-pane and copy/paste.
- **[Troubleshooting](troubleshooting)** — recovery procedures when things
  go wrong (NXC auth, Sliver, SA collection, restart playbook).

**Pre-competition:**

- **[Pre-Competition Setup](pre-competition)** — kick-off checklist.
- **[Inventory YAML](inventory-yaml)** — `config/inventory.yml` schema.
- **[Sliver Setup](sliver-setup)** — server install + operator certs +
  listener verification.

**Running a competition:**

- **[Admin Control](running-competition)** — start / pause / resume /
  stop / reset + runtime settings + playbook scheduler.
- **[Scoreboard](scoreboard-reading)** — column meanings, TABLE/GRAPH
  toggle, split-scoring math, obscured labels for blue.

**C2 operations:**

- **[Overview](c2-overview)** — network map + alerts + pivot
  highlighting.
- **[Generation](c2-generation)** — generate + auto-deploy implants.
- **[Deployment](c2-deployment)** — register IOCs against deployed
  implants.
- **[Exec](c2-exec)** — run commands or push files to one or many
  targets.
- **[Targets](c2-targets)** — per-box SA, group actions, shells.
- **[Modules](c2-modules)** — bring-your-own binary / script.
- **[Tasks](c2-tasks)** — task queue + scheduled tasks.
- **[Console](c2-console)** — full Sliver operator console PTY.
- **[Credentials](c2-creds)** — passwords / hashes / Kerberos tickets.

**Developer:**

- **[Architecture](architecture)** — backend orchestration, routes, DB,
  frontend structure, IOC + alert systems.
- **[Subsystems](subsystems)** — NXC execution, SA collection, Sliver C2,
  cred harvesting, script wrapper, bulk exec, timestomping.
- **[Frontend](frontend)** — tech stack, routing, page inventory, shared
  components, data flow.
- **[Testing](testing)** — test-update policy, suite wiring, known
  environmental flakes.
- **[Configuration](configuration)** — every `.env` knob and which ones
  are runtime-mutable.
- **[IOC Authoring](ioc-authoring)** — dataclass + script anatomy +
  soak eligibility.
- **[API Reference](api-reference)** — operator-subset HTTP + WS
  endpoints.
- **[DB Schema](db-schema)** — tables + views relevant to scoring,
  C2 state, ops history.
- **[Architecture Diagram](architecture-diagram)** — regenerating the
  README system map from its Mermaid source.

## Live-`main` semantics

The wiki tracks `main`. Edits land via PR like any code change. The in-app
wiki reflects whatever is on disk at backend start — **restart the backend to
pick up edits**. No hot reload, no manifest invalidation.

## How wiki mode works

Hit **Ctrl+Shift+/** anywhere in the app to enter *wiki mode*. The cursor
changes to `help` and an amber banner appears across the top. Click any UI
element to jump to the matching wiki page (or, when no specific docs exist
yet, the page covering that route). Esc / banner X / Ctrl+Shift+/ again
exits the mode.

<!-- audited 2026-06-05 — wiki phase 8 Phase A audit pass; quick-start commands + page links verified against scripts/setup.sh, scripts/run.sh, and post-Phase-B page slugs. Updated 2026-07-30 — developer set expanded with architecture/subsystems/frontend/testing/configuration/architecture-diagram, moved in from docs/ + CLAUDE.md; intro reworded (the wiki now holds the dev reference, not just operator pages). -->

