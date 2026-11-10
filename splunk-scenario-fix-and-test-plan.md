# splunk-scenario — Plan A (fixes) & Plan B (verification)

> **STATUS 2026-11-08 — Plan A: DONE (A1–A7 implemented + tested offline). B0: GREEN**
> (engine `1282 passed`, the one failure pre-existing/environmental — fails identically on
> origin/main: missing `.gcp-sa.json`; dashboard `267 passed/28 pre-existing fails unchanged,
> +2 new`; comps repo lint green incl. splunk-scenario validated against the real loaders).
> Engine: 11 commits on top of the co-tenant `e46c56b` series (post shared-tree rebase):
> A2 inject-create, A1 event.conf drift, A3 telemetry gate + gating-FAIL abort + strict
> services + 90s retry, A4 hold-start sleeps red + `telemetry.ensure.sh` hook, A5
> `red.pacing.*` profiles + hung-loop watchdog **thread wired into stage_run** (the first
> wiring commit shipped the loop unstarted — the new integration test proves it runs and
> stays unsupervised), A6 evidence flush, docs, suite-decoupling. Dashboard `8119388`
> (pushed): TEZ_EVIDENCE_DIR + artifacts_root fallback. Comps `8d5b105` → `splunk-scenario`
> reconstructed (telemetry.json, 5 injects incl. 2 SIEM-only, ensure script, 1/3/4 pacing).
> **B1–B3 (live) remain — they are the point of the next window.**
> Deviations from plan text: telemetry expectations live in `telemetry.json` (separate file,
> like box_services.json) not a Compfile block; UF-ensure is comp-side `telemetry.ensure.sh`
> run by the engine hook (`scrim/telemetry_ensure.py`) rather than an engine module, keeping
> splunk specifics out of the engine; watchdog detects hung-but-active via world.json age
> (systemd Restart= already covers crashes).

Basis: scrim run-bd70a985 postmortem + repos pulled 2026-11-08
(engine `c685750`, dashboard `7681dd7`, competitions `50b54bb`).
Verified against current code: `scrim/inject_sync.py` warns-only on missing
injects; `quotient/setup.py` has `build_event_conf()` but nothing regenerates a
baked engine `event.conf`; `scrim/red_stage.py:44` hardcodes pacing caps 2/4/6;
`artifacts_lib/verdict.py` has the red-reachability hard gate but no telemetry
gate; `docs/known-issues.md` already files "verify passes with a scored service
down" — Fix A3 subsumes that defect entry.

All engine work lands in `~/dev/tezcatlipoca` on branches off current main;
comp content in the competitions repo; nothing hacks the live gateway (10.0.0.143).

---

## Plan A — fixes

### A1. Engine re-seed on definition drift (phantom-pin bug)
- Stamp the engine with a hash of the scoring-relevant comp inputs
  (`box_services.json`, Compfile service/pin blocks, `injects/`) at the phase
  that bakes `event.conf`.
- At phase-8 seed **and** scrim verify: if the live engine's stamp ≠ repo hash,
  regenerate `event.conf` via `build_event_conf()`, push it, and reload scoring
  (restart the `quotient_server` container only — DB untouched).
- If reload proves unsafe mid-lifecycle, degrade to a **hard pre-T0 fail**
  ("stale engine — reseed or destroy range") — never score against drifted pins.
- Tests: stamp mismatch detection; regeneration idempotence; team accounts and
  credlists survive the reload.

### A2. Injects must reach the engine (zero-inject bug)
- Root-cause first: reproduce the silent phase-8/7 no-op (prime suspects:
  create-against-unstarted-event ordering, and `create_injects()` title-dedup
  short-circuiting on an empty DB — read the phase log against current code).
- Fix at the seeding phase (create after `competition/start`+unpause, verify by
  GET, non-zero exit if `/api/injects` count ≠ expected).
- Belt: `scrim/inject_sync.py` — escalate `missing` from WARNING to create via
  the existing `POST /api/injects/create` path, then reanchor times.
- Tests: empty engine → 5 created; partial → missing created, duplicates left
  alone; times re-anchored to real T0.

### A3. Hard pre-T0 gates (telemetry + scored-service-down)
- New Compfile key `telemetry.expected` per comp:
  `{index: windows_evtlog|min, sources: [host-prefixes], window: 5m}`.
- Scrim verify gate: for every team, POST a REST search against the team's own
  head; any miss = **refuse to fire** (this run launched minutes from zero
  Windows ingest — the single scariest gap).
- Same change makes existing informational service verdicts FAILING at verify
  (implements the known-issues entry; keep `informational` only for services
  explicitly marked unscored).
- Tests: fixture with one empty head → gate red; slow-ingest race → one retry
  window (~90s) before fail.

### A4. Forwarder lifecycle inside scrim; tlaloc held until T0
- Port `provision_forwarders.py` (dashboard) into an engine module
  (`scrim/telemetry_stage.py`): idempotent UF ensure (install-or-repair,
  outputs.conf → team head, WinEventLog inputs, ScriptBlockLogging), run as a
  staging phase **after** red staging, **before** the fire test; its success is
  A3's precondition.
- Pre-T0 tlaloc: seed red01 with the service disabled (systemd `disable`,
  timer not armed) and have `red_stage` enable it as the last action before T0.
  (This run needed a manual `systemctl stop tlaloc` because the seeded loop ate
  fresh forwarders.)
- Preferred end-state: **bake the UF into the splunk-scenario goldens** (nachon
  inject step) so staging is verify-only; module remains as repair path.
- Tests: ensure-phase convergence from (missing / half-installed / running)
  states; tlaloc inactive between deploy-end and T0 on a timed probe.

### A5. Red pacing profiles + stall recovery (the two gameplay FAIL gates)
- Replace hardcoded `red_pacing_caps()` with profile resolution:
  Compfile `red.pacing` > scrim default. Give 3-box comps a realistic profile
  (e.g. start 1 / end 3 / endgame 4) so `max_simultaneous_down` gates measure
  what the range can actually do; report already judges against manifest caps.
- Stalls (2 in 120 min): red LLM timeout exists (300s local) — add supervisor
  detection: tlaloc cycle heartbeat file; N missed cycles → log STALL event,
  restart `tlaloc.service`, continue; stall count feeds the verdict as before
  but recovery is automatic. Reduce contention: red gets endpoint priority (or
  its own model slot) when blues share the gateway — the sliver-practice
  co-tenant run made this real.
- Tests: profile resolution unit tests; heartbeat restart against a faked dead
  loop; verdict reads manifest caps (not the old constant).

### A6. Evidence out before the wrecking ball
- `teardown_stage`: copy `.automated-tests/<run>` → `runs/<run-id>/evidence/`
  (and dashboard serves it from there). The comp workspace is `--destroy`-bound;
  finalization order saved us this time by minutes.
- Tests: mocked destroy ordering; artifact tree parity check post-copy.

### A7. Comp content (competitions repo)
- Verify `win01-smb` renders (A1 makes this visible); keep the
  "note what is *missing*" endpoint-protection wording.
- Add 2 **SIEM-only injects** (answers derivable only from their Splunk:
  "which host beaconed every ~45s — submit the search and EventCode";
  "when were System logs cleared — cite the event"), scored — this converts
  proven telemetry into used telemetry.
- Declare `telemetry.expected` (consumed by A3) in the Compfile.

**Definition of done (Plan A):** every item merged with its tests green on the
repo's offline suite, plus one A2-style live check that the fix changed the
specific observed failure (not just that new code exists).

---

## Plan B — prove everything was fixed

Cheapest-to-dearest, each stage has hard exit criteria; stop on first red.

### B0. Offline suite (minutes)
Run engine tests for A1–A6 + competitions-repo lint. Gate: green.

### B1. Golden probe (once, ~20 min)
Rebuild splunk-scenario goldens with baked UF (A4 end-state); one-shot-boot
probe asserts: UF installed, outputs.conf templating to `192.168.<id>.2:9997`,
inputs.conf present, tlaloc unit disabled on red01 seed. Gate: probe green.

### B2. RED-ONLY dry run — no blues, ~30 min, 2 teams (the main cheap proof)
Fresh deploy (new identifiers, e.g. 134–135) → scrim staging → verify →
20-min red-only event → teardown. Assert, from artifacts:
- **A1**: engine event.conf hash == repo hash; scoreboard shows `win01-smb`,
  no `win01-iis` anywhere (blue prompt + snapshot pins).
- **A2**: `/api/injects` count == 5 at T0 (scrim verify prints it).
- **A3**: telemetry gate ran; deliberately sabotage one head once (drop 9997)
  and confirm the run **refuses to fire** — negative test, then restore.
- **A4**: zero manual steps between deploy and T0; tlaloc probe: inactive
  pre-T0, active post-T0; UFs connected without intervention.
- **A5**: report judges simultaneous-down against the 2-team profile; heartbeat
  log present.
- **A6**: `runs/<id>/evidence/` complete BEFORE estate-sweep goes clean.
- **Teardown**: estate sweep (VMs/bridges/IPs) clean as in run-14ac7f14.
Gate: all assertions pass in the run's own artifacts, not operator memory.

### B3. Full scrim validation — 4 teams, mixed models, 120–150 min
Same as run-bd70a985 with 2× `qwen3.8-27b` + 2× alternate model; co-tenant
load left running on purpose (gateway contention is the A5 test condition).
Success criteria (all from verdict + evidence, not vibes):
- Injects: ≥5 published; ≥1 submission per team; the 2 SIEM-only injects'
  answers cite real searches/EventCodes matching red ground truth.
- Telemetry used: team REPORT.md/LOG.md reference their Splunk (grep
  `index=`/`4104`) — target ≥2/4 teams doing real SIEM hunts.
- Red gates: PASS on comp-profile threshold; `stalls == 0` or auto-recovered
  (STALL event + resume visible in log).
- Blues: 0 timeouts; cycles ≥ 2/team/model.
- Verdict: READY, or NOT-READY only with every remaining gap a filed issue.
- Post-run: estate sweep clean; evidence readable from `run_dir`.

### B4. Regression & coexistence
One standard comp (sliver-practice-class) deployed + torn down on the same
node/day: proves event.conf stamping, pacing-profile plumbing, and evidence
flush are comp-neutral, and the two runs don't cross-contaminate (gateway lock,
collisions probe — note the known over-scoping issue in known-issues.md; if B4
trips it, that's a finding, not a blocker).

### Traceability
Each Plan-A item maps to ≥1 B-stage assertion (A1→B2, A2→B2/B3, A3→B2 negative
test, A4→B1/B2, A5→B3, A6→B2, A7→B3). Plan B is only "done" when the table is
fully green with linked evidence paths.

## Order & effort
A1→A2 (same quotient/seeding surface, root-cause first) ⇒ A3 ⇒ A4 ⇒ A6 (small)
⇒ A5 ⇒ A7 (content, parallel) ⇒ B0 throughout ⇒ B1–B2 in one window (~1 h)
⇒ B3 in a reserved 3-h window with quiet GPU ⇒ B4 same day as B3 if capacity.

## Verdict-62 — Plan B execution results (2026-11-10)

### B2 — 2-team dry run + negative test: **PASS**
- Happy path (run-11, 06:59–08:04 EDT): authoritative pre-T0 verify PASS (telemetry 6/6
  feeds fresh across 2 teams), fire test down→restored→healed, T0 fired, 5/5 injects
  re-anchored, **red director 6 cycles / 3 impacts** (persistent sabotage: nginx binary
  deleted + conf corrupted on both web01s), blue cycles ran (rc=0), engine capture +
  39-file evidence flush, `--keep-range` teardown, DONE.
  Evidence: `/home/hna/tez-dashboard-data/b2d-1706/evidence/run-f199029d/` (+ `scrim11.log`).
- A1–A6 extractor: 6/6 PASS (`b2d-1706/b2-check.py scrim11.log`).
- **Negative test: PASS** — iptables REJECT :9997 on team1 head → gate FAILed
  `2/6 feeds not fresh (team1:windows_evtlog count=0; team1:web main count=0)` and the
  pre-T0 scoreboard gate refused the event (`splunk01-ingest` pin DOWN, RuntimeError,
  no T0, rc=1). Blackout cleared; estate restored to RESULT: PASS. (`scrim-neg.log`,
  `scrim-neg2.log`, `b2-negative.sh`.)
- Platform fixes this proved and shipped (engine `df4619c`, comps `40aa44c`):
  telemetry machine-shape resolution; ensure fd-9/`ssh -n` (ssh was eating team2's loop
  row — phantom "all teams wired"); UF install poll waits for service registration;
  anti-phantom compound UF-OK; scrim copytree idempotency; **restored `start_red_director`
  at T0 that the sliver-c2 squash (69c7fa1) silently dropped — red had never fired in any
  scrim since**.
