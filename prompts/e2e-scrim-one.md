# E2E: scrim-one through the dashboard — agent run prompt

Copy this to a fresh agent session. Fill `TEZ_DASHBOARD_TOKEN` with the `zcode` token from
the dashboard `.env` files (never commit it).

## Mission

Run one full end-to-end scrim of the `scrim-one` competition through the central dashboard
(the new path: MCP → run queue → engine worktree → scrim harness), judge it on interaction
reality, tear it down clean, and report. This validates BOTH the range pipeline and the new
run infrastructure (stage pushes, event cursor, log/artifact access, teardown through the
queue).

## Setup facts

- Dashboard address and bearer tokens: `../tezcatlipoca/infrastructure-status.md` (gitignored;
  the dashboard's own `.env` holds the client tokens). Web UI for humans; MCP at `/mcp`
  (Streamable HTTP, `Authorization: Bearer $TEZ_DASHBOARD_TOKEN`).
- Repos: engine `DawgSecTeam/tezcatlipoca` (pinned per run), definitions
  `DawgSecTeam/tezcatlipoca-competitions` (scrim-one lives there), dashboard
  `DawgSecTeam/tezcatlipoca-dashboard`.
- Comp `scrim-one`: 7 boxes (dc01 AD DC, win02 FTP, web01, mail01, db01, splunk01, fw01
  in-path pfSense), 13 scored services, 68 misconfigs, 12 injects.
- Skim first: engine `docs/scrim-harness.md`, `docs/usage-agents.md` § central runs,
  `../tezcatlipoca/infrastructure-status.md` (hosts, free vmids/IPs, node quirks),
  `AGENTS.md` (root-cause discipline, report rules).

## How to run it

1. Connect an MCP client; `list_comps` as a sanity check.
2. `submit_run` (record the returned `run_id`):

   ```json
   {"comp_id": "scrim-one", "action": "scrim", "engine_ref": "estate-v0.3.0", "teams": 2,
    "params": {"duration_min": 45,
      "red_vmid": "<free>", "red_ip": "<free cluster IP>",
      "red_model": "<current working model>", "blue_model": "<current working model>",
      "llm_base_url": "<current working endpoint>",
      "extra_args": ["--red-storage", "<pool>", "--red-mode", "routed",
                     "--red-subnet", "10.200.0.0/24", "--red-seg-ip", "10.200.0.11"]}}
   ```

   Extra scrim flags ride in `params.extra_args` — anything `run-agent-scrim.py --help`
   lists that the run API doesn't map directly (`--red-gw`, `--red-tunnel`, `--seed-depth`,
   `--no-seed-red`, `--reasoning-effort`, `--blue2-*`, `--from-phase`, …). Do NOT pass
   `new`/`from_template`: this e2e runs the existing scrim-one definition.
3. Immediately `watch_run(run_id)`. From then on stage/lifecycle events arrive as MCP
   notifications — never build a polling loop. To catch up after a disconnect, call
   `get_run_events(run_id, since=<last seq>)` once. `get_run_log(run_id, since=N)` reads
   the log by byte offset when you need text.
4. Cheap second view: the web UI shows the same live stage timeline.
5. The run is done when its state is terminal (`succeeded`/`failed`/`cancelled`); the
   harness tears the range down itself unless `keep_range` was set.

## Preflight (before submitting)

- Check twins: `pgrep -af 'run-agent-scrim|create-competition'` plus `list_runs`. Never
  overlap another session's range — fail-fast refuses a second scrim-one run, but another
  comp's run still contends for estate resources.
- LLM: verify the red/blue endpoint actually answers (a tiny chat completion or bad-auto's
  `validate-llm`). If blues stall on a slow endpoint that is a FINDING to report, not a
  reason to fake results.
- Resources: pick a free `--red-vmid` and `--red-ip` (red01 slots are preflight-gated;
  an occupied slot is refused naming the owner). Scrim runs share the engine's scoring
  vmid, so an engine-lock refusal means another deploy is live — wait or coordinate.

## Success criteria (interaction reality first)

Lead every judgement with these, in order:

1. **Blue presence**: both teams' agents actually ran cycles (cycle logs + `feed.log` rc
   lines). A red-vs-empty-room run is a FAILED run no matter how good red's log looks.
2. **Red-blue interaction counts**: red actions landed on live boxes; defenders reacted
   (service restores, rotations, lockouts).
3. Pipeline: deploy reached 8/8 phases, verify gates passed, injects delivered, teardown
   left the estate verified clean.

## New-setup criteria (what this e2e is really testing)

- Every stage boundary (deploy `[N/8]` phases and scrim stages) arrived as a push
  notification on the watched session; the UI stage timeline matched.
- `get_run_events` cursor recovered missed events after a reconnect.
- `get_run_log` tracked the log incrementally; `get_artifacts` listed the collected
  evidence after capture/teardown.
- The run's engine worktree is gone afterwards (the run dir keeps the comp workspace,
  logs and evidence only).
- A second `submit_run` for scrim-one while the first is active is refused with a clear
  message.

## Failure discipline

Root-cause from evidence before retrying; "flaky" means broken. No workarounds (no manual
SSH repairs, hand-edited VMs, `--force`) — if one feels necessary, the thing that made it
necessary is the bug. A bug in dashboard/engine code gets fixed at the root with a
regression test and the run re-launched; infrastructure findings go into the report (and
`docs/known-issues.md` where the engine owns them). Never re-dispatch an identical failing
step.

## Cleanup + report

- If the run died before its own teardown, submit `action=destroy` (the destroy path
  collects evidence first and requires the run's ownership set; `params: {"keep_red": true}`
  only if red evidence still needs pulling).
- Report (run `REPORT.md` + your summary): **interaction counts first**, then the
  new-setup delivery (pushes, cursor, artifacts), then the per-stage timeline, then
  findings with root causes. Describe the run as it happened — never dress up an
  empty-room run.
