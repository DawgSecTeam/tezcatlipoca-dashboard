# C3rberus: Competition Command and Control

NetExec- and Sliver-based red-team scoring system for cyber-defense
competitions (CDE - Cyber Defense Exercise). Detects red-team compromise of blue-team boxes
via IOC checks (NXC over SMB / SSH), generates and manages Sliver
implants, runs situational-awareness collection, and surfaces a live
scoreboard to operators and competitors.

![C3rberus system map](./diagrams/mermaid/render/C3rberus_full.svg)

> Diagram source (editable Mermaid): [`diagrams/mermaid/C3rberus_full.md`](./diagrams/mermaid/C3rberus_full.md) — rebuild SVGs with `diagrams/mermaid/render.sh`.

## Quick start

```bash
git clone <repo>
cd C3rberus
scripts/setup.sh        # one-time: Python venv, npm install, pre-commit hooks
scripts/run.sh          # start backend (:3000) + frontend (:5173)
```

After `run.sh`:

- Frontend — <https://localhost:5173>
- Backend API — <https://localhost:3000>
- OpenAPI docs — <https://localhost:3000/docs>

Default admin login: `admin / phoenix-collide-surprise-oaf`.

## Documentation

| Audience | Source |
|---|---|
| **Operators** (red team, admin, blue) | The [operator wiki](wiki/Home.md) — start at `Home` or `Operator Actions`. Also rendered inside the app at `/wiki`. |
| **Developers / contributors** | `CLAUDE.md` (top-level guide), `wiki/architecture.md` (backend + frontend), `wiki/subsystems.md` (NXC, SA, Sliver, cred harvesting, timestomp), `wiki/frontend.md`, `wiki/configuration.md`, `wiki/db-schema.md`, `wiki/api-reference.md`. |
| **Test / soak operators** | `wiki/testing.md` + `tests/perf/soak_profiles/`. |

The wiki is mirrored to GitHub on every push to `main`
(`.github/workflows/wiki.yml`). Operators get the same content there.

## Common commands

```bash
scripts/run.sh                       # backend + frontend
scripts/testing/run_tests.sh --pytest --fast # pytest suite
cd react && npm run test:e2e         # Playwright (mocked)
scripts/audit/run_audit.sh                 # ruff + ESLint + A1-A9 fences
scripts/testing/run_soak.sh --profile smoke  # ~10 min soak run
scripts/diagnostic/check_access.py              # verify NXC access per inventory
```

All scripts are documented in `CLAUDE.md` § Commands.

## Configuration

Runtime config lives in `.env`. Per-key documentation is in
`CLAUDE.md` § Configuration; the wiki's
[Sliver Setup](wiki/sliver-setup.md) page covers the Sliver-specific
keys end-to-end.

## License — see `LICENSE` if present, else contact project owner.
