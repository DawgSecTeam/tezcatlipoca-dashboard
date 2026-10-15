# E2E: scrim-one-light through the dashboard — full test prompt

Copy this to a fresh agent session. Fill `TEZ_DASHBOARD_TOKEN` with the `zcode` token from the
dashboard `.env` files (never commit it).

## Mission

Fully test the `scrim-one-light` competition through the central dashboard: build the range in
one clean cycle (deploy → verify → destroy), then run one bounded agent scrim of it — judging the
scrim on interaction reality and the dashboard on what it now claims to know (a per-range board,
honest per-action stages, a whitelisted inventory, per-run evidence). Tear everything down and
leave the estate verified clean. Report findings with evidence; fix nothing by hand.

The two cycles are deliberately separate: A validates the pipeline and the dashboard's four
surfaces and leaves a known-good baseline; B is the product test (does the light comp actually
play?). Run B last, so a failure in it still has the destroy path available.

## Setup facts

- Dashboard address, bearer tokens, free vmids/IPs, host and node quirks, LLM endpoint/model:
  `../tezcatlipoca/infrastructure-status.md` (gitignored — read it, and never restate its values
  in a report, a commit message or a prompt). The dashboard `.env` holds the client tokens; MCP
  at `/mcp` (Streamable HTTP), HTTP API on the same base URL, web UI for humans.
- Repos: engine `DawgSecTeam/tezcatlipoca` (pinned per run by `engine_ref`), definitions
  `DawgSecTeam/tezcatlipoca-competitions` (`scrim-one-light` lives there), dashboard
  `DawgSecTeam/tezcatlipoca-dashboard`.
- Comp `scrim-one-light` (re-verify from its `Compfile`/`boxes.json` before starting, and use the
  catalog's id if `list_comps` names it differently): Meridian Health regional clinic (light),
  difficulty 4, `assume_breach 1` with depth 2, **remote access OFF** (`remote_access 0`), and
  **1 team** (the Compfile declares no team count — pass it on the run). Three boxes: `win02`
  (Windows FTP drop, 2 vCPU / 4 GB / 60 GB, `base-windows-server`), `web01` (Debian web front
  end, 1 / 2 GB / 15 GB, `base-debian13-lite-fix`), `db01` (Debian MySQL records DB, same specs
  and template). 6 scored services, 3 misconfigs, 4 injects, no domain controller and no in-path
  firewall. The Compfile pins red01's slot (vmid + address) explicitly: read those values there
  and do not copy them into reports.
- Skim first: engine `docs/scrim-harness.md`, `docs/usage-agents.md` § central runs, engine
  `AGENTS.md` (root-cause discipline, report rules), dashboard `README.md` § Ranges.

## Preflight (gates — never submit past a failed one)

- No twins: `list_runs` shows no `queued`/`running` run, and the ops host shows no driver process
  (`ps -eo pid,etimes,user,args | grep -E 'python.*(create-competition|run-agent-scrim|destroy-competition|verify-competition|redeploy-competition)'`,
  excluding the grep itself — never `pkill -f` a pattern that also matches your own command line).
- Node and vmids: pick the deploy node(s) from the dashboard's node catalog (`GET /api/nodes`) and
  a free `scoring_vmid` / `red_vmid` from `infrastructure-status.md`. red01's slot is
  preflight-gated: an occupied slot is a refusal naming the owner, never a silent default.
- RAM: confirm headroom for 3 boxes plus the engine before submitting. A refusal here is cheaper
  than a half-built range.
- LLM: prove the red/blue endpoint answers (a tiny completion, or bad-auto's `validate-llm`)
  before the scrim. A stalled endpoint is a FINDING; never fake cycles.
- Record the deployed dashboard ref (`ssh <ops host> 'git -C ~/tezcatlipoca-dashboard log --oneline -1'`)
  so every UI finding is anchored to a commit.

## Cycle A — deploy → verify → destroy

For every step: submit through the dashboard, `watch_run` immediately, and never build a polling
loop — stage and lifecycle events are pushed. After a disconnect, `get_run_events(run_id, since=<last seq>)`
recovers in one call, and `get_run_log` reads by byte offset.

1. `submit_run {"comp_id": "scrim-one-light", "action": "deploy", "engine_ref": "estate-v0.3.0", "teams": 1,
   "scoring_vmid": <free>, "nodes": [<catalog node>]}`.
   Assert, from the events and the API — these are the regressions this test exists for:
   - exactly 8 stage events, in order `phase 1/8 … 8/8`, each carrying `index`/`total`, no
     duplicates, and the run record's `stage` ending at the last completed phase;
   - the board (`GET /api/ranges`) gains a range for this workspace, `state: "building"` during
     and `"live"` after, and the card's `teams` is the RANGE's deploy team count;
   - `GET /api/runs/{id}/inventory` populates: 1 team; 3 boxes matching the definition by name
     with vmids, IPs and node (`win02`'s cloud-init IP may legitimately be absent — the comp's
     `last_octet` rule supplies the fallback); the team subnet and engine NIC; the networks; the
     engine's vmid and management IP; red01 at the vmid its Compfile pins;
   - `inventory.deploy.degradations` is EMPTY. The historical failure of this comp was an
     `assume_breach_deploy` degradation (`FileNotFoundError … proxmox.pub`) that left red01
     unstamped and then undeletable; if any degradation appears, capture it verbatim and classify
     it before continuing;
   - the workspace's `.badauto-config.yaml` carries `deploy.ssh_key` (the upstream pin that
     failure lacked).
2. `submit_run {"action": "verify", "comp_id": "scrim-one-light", …}`.
   Assert: exactly ONE stage event (`verify`, index 1, total 1) — a verify must never replay the
   range's `phase N/8` deploy phases — and it must NOT create a second range card (it groups into
   the deploy's range through its recorded workspace).
3. `submit_run {"action": "destroy", "comp_id": "scrim-one-light"}`.
   Assert: exactly one `teardown` stage (1/1); rc 0 and the driver's own "estate clean for this
   ownership set" verdict; `.teardown-journal.json` `done` lists every step INCLUDING `red01`;
   the workspace's `estate-state.json` records no VM in `resources` (v0.3.0's inventory — the
   teardown forgets each entry as the VM goes; a pre-v0.3 range's `terraform/terraform.tfstate`
   is the fallback); the board card leaves and a one-line
   tombstone appears naming this destroy run; `GET /api/ranges` shows `state: "retired"`,
   `retired_by` = that run, `retired_at` set.
   Then re-submit `destroy` for the same comp: it must be REFUSED, naming the run that retired the
   range, and must create no new run record. This is the guard against the six destroys one dead
   range collected on 2026-10-14.

## Cycle B — one bounded agent scrim

4. `submit_run {"comp_id": "scrim-one-light", "action": "scrim", "engine_ref": "estate-v0.3.0", "teams": 1,
   "params": {"duration_min": 30, "red_vmid": <free>, "red_ip": <free>, "red_model": <current>,
   "blue_model": <current>, "llm_base_url": <current>, "extra_args": [...]}}`.
   Extra harness flags ride in `params.extra_args` (anything `run-agent-scrim.py --help` lists that
   the run API does not map directly). This comp consumes no remote-access slots — do not add
   remote-access flags.
   Assert: the 8 scrim stages (`author, deploy, verify, blues, red, event, capture, teardown`)
   arrive in harness order, with no deploy-phase events and no phases replayed from an earlier
   run's `run.json`; the board shows the range while it is up; while it is up, the inventory's
   boxes and IPs match what the harness built (cross-check at least one vmid and one IP against
   the harness's own logs); evidence lands under `.automated-tests/<run-id>/` and the Evidence tab
   groups it under THIS run with Report/Blue/Red/Harness categories; teardown removes the harness's
   own range, its journal records every step including `red01`, and no `NOT CLEAN` line appears.
   Known current behaviour to RECORD rather than rediscover: a succeeded scrim tore down its own
   range, so there is no dashboard `destroy` run and its range reads `state: "failed"` with no
   tombstone even though nothing failed. Confirm it still behaves that way.

## Optional Cycle C — two concurrent deploys of one comp

Only with the user's explicit OK, and only after confirming RAM and distinct free vmids/team
identifiers for two light ranges (2 engines + 6 boxes; the engine preflight refuses provable
collisions). Submit both deploys before either finishes, then assert: both reach 8/8;
`/api/ranges` lists them as TWO ranges with distinct keys, each with its own `teams`; destroying
one retires only its own range (the other stays `live` and keeps its card); and the survivor's
inventory matches its pre-destroy snapshot — no cross-range teardown.

## Success criteria — interaction reality first

Lead every judgement with these, in order:

1. **Blue presence**: the team's agents actually ran cycles (cycle logs, `feed.log` rc lines). A
   red-vs-empty-room run is a FAILED run no matter how good red's log looks.
2. **Red-blue interaction counts**: red actions landed on live boxes and defenders reacted
   (service restores, rotations, lockouts). Counts, not impressions.
3. Pipeline: deploy reached 8/8, verify's own gates passed, the 4 injects were delivered, and
   teardown left the estate verified clean.
4. Dashboard claims matched reality at every step (the assertions above).

## Failure discipline

Root-cause from evidence before retrying; "flaky" means broken. No workarounds: no manual SSH
repairs, hand-edited VMs, hand-cleared flags, `--force`, or ad-hoc destroy scripts — if a
workaround feels necessary, the thing that made it necessary is the bug. For anything that fails,
capture the run id, log tail, events, teardown journal and the workspace's state document
(`estate-state.json`, or the legacy `.deploy_state.json` + tfstate for a v0.2 range), classify
it (product vs
dashboard vs estate), and file it where it belongs: dashboard/engine code gets fixed at the root
with a regression test (architectural changes go to the user first); infrastructure findings go
into the report and `docs/known-issues.md` where the engine owns them. Never re-dispatch an
identical failing step.

## Report

- Artifacts: the run ids for all four runs, each one's `/api/runs/{id}`, `/events`, `/log`,
  `/inventory` and `/artifacts` payloads, the pre/post `/api/ranges` snapshots, the teardown
  journal, and the driver's own `REPORT.md` from the workspace's `.automated-tests/<run-id>/`.
- Structure: **interaction counts first**, then the dashboard assertions (naming the exact payload
  fields checked and any mismatch), then the per-stage timeline, then findings with root causes
  and what a fix would have to prove. For UI findings name the page, the control and the payload
  field; screenshots if a browser is available (the board card → tombstone transition, a run
  page's General/Teams/Infrastructure/Evidence tabs, and a collapsed destroy card).
- End state: no run records left `queued`/`running`, no driver processes, no VMs owned by these
  runs, and `/api/ranges` showing the ranges retired. Say plainly if any of that is not true.
