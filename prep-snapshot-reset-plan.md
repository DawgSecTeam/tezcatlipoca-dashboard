# Prep-stage restore points — design and implementation plan

Status: **implemented and live-verified on one range** (2026-11-13). Branch `prep-snapshot`
(`c225f0c`, plus `850ae5b` for the netplan defect the trial found), pushed — 1354 offline
tests pass. Updated 2026-11-17: verified with a **C2 payload matrix** through two
reset→round cycles on a kept 2-team range, after two more defects the first such reboot exposed
(section below); a snapshot taken by the fixed code is still owed one round.

### Live trial result — `snap-trial`, 1 Debian box, hdd-150

Three rounds on one kept range, no redeploy between them:

| step | wall clock |
|---|---|
| cold deploy (engine template + golden + red staging, no C2) | **23.2 min** |
| arm (`competition-ops.py start`) ×3 | **0.1 min** each |
| prepared reset (`--mode prepared`) ×2 | **2.0 / 1.9 min** |
| verify after round 3 | **PASS** (pins, plant coverage, round loop, degradations) |

The invariant held in both directions: after a reset the box carried **no** plant artifact
(`libvirt-sync.service` absent, unit `not-found`), and after the following arm it was present,
enabled and active. So a round costs ~2.1 min of machinery instead of a 23-min redeploy —
and the `red` stage that dominates a real deploy never re-runs.

**A defect the trial found (fixed in `850ae5b`).** The prepared reset reboots every VM, and
red01's `/etc/netplan/51-tlaloc.yaml` pinned the management NIC by name. The file written at
staging said `eth0`; after the rollback the guest booted `ens18`, the name did not match, and
`net0` came up DOWN — the arm then failed `No route to host` to red01. The netplan now matches
each NIC by MAC (which the staging code already reads), and a second reset→arm cycle came up
with `ens18 UP 10.0.0.249/24`. This bug is invisible without a reboot, which is exactly what
the reset introduces.

Still unproven at that point: a comp whose red plant includes a **C2 payload matrix** (the
`payloads_ready` reuse path is what should make round 2 cheap there), and a range with more
than one team. The C2 range is now up — see the next section.

### Kept range with a C2 — what the first reboot-then-arm round broke (2026-11-17)

`scrim-one-light` (2 teams, Sliver, 44 payloads / 1.8 GB across every transport) is the first
range where round 2 is a rollback **of a box running a C2**. Round 1 was clean, and it is what the
matrix reuse buys: the payload freeze that costs ~45 min of deploy time never re-ran — red's T0
setup took about a minute. Round 2 then failed the seed's C2-channel gate — four boxes' payloads
ran and never checked in — and it took that reboot to expose two defects, neither visible inside a
single deploy:

- **The transport listeners came back on the wrong process.** Sliver persists its listeners in
  the server database and restores them at daemon start, and red01 runs a *build-only* server
  beside the main one sharing that database. Whichever wins the boot race owns `:8888`; after
  this rollback the mTLS listener lived on `sliver-build-1`, so an mTLS implant checked in on a
  process the seed never queries. Three fixes: build units are started on demand and **disabled
  at boot**; `SliverClient.ensure_listeners` first **takes the transport jobs back** from its
  build servers (`release_listeners`); and the arm / reused-matrix path calls
  `c2-prep --listeners-only` (`tlaloc_ops.bring_up_listeners`) instead of trusting the disk.

   The daemon's own boot log is the whole mechanism — the two servers race per transport, and the
   loser's restore dies on the shared rows: *"persistent mtls listener (saved job id=1)
   [0.0.0.0:8888]: failed to start: listen tcp 0.0.0.0:8888: bind: address already in use"*, then
   *"…started with new job id=N but failed to update listener record: constraint failed: UNIQUE
   constraint failed: listener_jobs.job_id"*. One boot handed `:8888` to the build server and the
   channels went unread; the next gave all five ports to the main one. Same range, same code, coin
   flip — which is why the arm asks instead of assuming.

- **red01 armed itself at boot.** The held state was `systemctl enable` + stopped, so the reset's
  reboot started the red loop and tlaloc ran its own day-0 seed before the event armed anything
  (`enabled-but-stopped` is only a hold until something reboots). A staged range now installs
  `tlaloc.service` **disabled**, and the release — `start_unit`, or the scrim's director start —
  enables it as it starts, so a mid-event reboot does not silently disarm red.
- **A prepared reset trusted the snapshot to hold it.** The fix above only helps a range prepared
  *after* it: this range's `tz-ready` still carries an enabled unit, so its third reset booted
  straight back into a running red loop — `Started tlaloc.service` 7 s after that boot, still
  cycling ten minutes later with no event open (it planted nothing: its rolled-back intel said the
  boxes were already owned). Unarmed is
  the contract of `--mode prepared`, so `mode_prepared` now stops **and disables** the unit once
  red01 answers SSH (`tlaloc_ops.hold_unit`), whatever the restore point carried.

Both are in `9389710` on `prep-snapshot`, with offline tests for each (engine 1363 pass, tlaloc
suite pass); the reset's own hold is `cabeba0`.

**Trial — two consecutive clean round-2s on the kept range, driven through the dashboard.** Reset
`run-3e49368f` (12.5 min) → round `run-deddca27` (19 min, rc=0); reset `run-aeb065b6` → round
`run-75b417ef` (rc=0). Both rounds reused the matrix ("reusing 44 frozen payload(s) already on
red01"), both cleared the C2-channel gate ("seed manifest proves 6 box(es) planted" — the gate
that failed before), and both ended on the authoritative pre-T0 verify `RESULT: PASS` with plant
coverage 6/6 and no degradations. On red01 in between: after one rollback's boot only
8888/8080/8443 were bound at all — dns and wg had come back on neither daemon — and `:8888` sat on
the build server after one boot and on the main server after the other, which is the coin flip the
fix removes. After the arm, all five transports belonged to `sliver.service`,
`sliver-build-1.service` was disabled, and `tlaloc.service` was installed disabled and enabled
only when the director started. The rounds' *interaction* verdicts are a separate axis and still
NOT READY (score 4: red restore reactions 2/3, blue restorations 1/2, injects 0, notebook 8/10 on
a 10-minute round) — model and campaign quality, not the reset loop.

What those rounds do **not** yet prove is a snapshot taken by this code. This range's `tz-ready`
predates the fixes, so its red01 carries the older agent package and the older unit enablement,
and each round repairs them as it goes: `stage_red01` re-pushes the package, `prepare_sliver`
disables the build units, the plant disables `tlaloc.service` and the director start re-enables it.
An arm that stages nothing — `competition-ops.py start` — therefore fails at the listener bring-up
against such a range, with a message naming the version skew. That is the rule the trial makes
explicit: **a prepared range runs the code its snapshot carries, so upgrading one means preparing
it again with a deploy.**

What this deliberately does not change: the wg listener's `NPort=8888` (it is compiled into the
frozen payloads, so moving it invalidates the matrix).

Owner directive (2026-11-12): the range
is built **once**, snapshotted, and every round — test or real — starts by rolling back and
deploying the attack surface live. The same range, once rebuilt, is the environment the
competition runs on.

### As-built deviations from the plan below

- **The prepared point is taken in phase 8, not phase 7.** Phase 8's box-side re-ensure
  (`reensure_services`) runs after phase 7's wipe, so a phase-7 snapshot would have been the
  moment *before* that work — a reset would then restore boxes the engine had already
  re-ensured past. All three VM classes are snapshotted at one moment instead: after
  `seed_teams`, before unpause/injects (`snap_prepared` in `deploy_lib/phases/_snapshots.py`).
- **Taken once per VM, gated on `red_deployed`.** A phase-8 resume that failed *during*
  arming must not replace the prepared points with a partially armed disk.
- **A new `--mode prepared`** rather than changing `reset`: `reset` stays the per-box repair
  ladder for old or damaged ranges, and `prepared` refuses loudly when a VM has no prepared
  point, naming it. `--reset-event` is no longer needed for a round rerun (its engine
  rollback already returns a seeded, unstarted DB).
- **The late account-mode pass stayed in phase 7.** The seed writes `/etc/shadow` via
  `chpasswd` (`tlaloc/effects/rotate_credlist.py`), not `useradd`, so it does not reset
  account-file modes; `final.py`'s comment says "useradd" and is wrong — worth confirming
  live, but it does not move the pass.

## 1. What changes

Today a deploy fuses two things that have very different lifetimes: **building** the range
and **arming** it. Both happen in one pass, and the restore points are taken after arming, so
`tz-ready` is a compromised disk — which is why a rollback today hands back a range where
red's implants are live but red01's memory of them is not.

This plan puts a snapshot boundary between the two:

```
   PREPARE  (once per range)          EVENT START  (every round, incl. the real comp)
   ─────────────────────────          ─────────────────────────────────────────────
   templates, goldens                 deploy the frozen implants onto the boxes
   engine build + event.conf          start the tlaloc loop
   team boxes: clone, configure,      engine: seed teams, unpause, create injects
     domain-join, vuln/service plant    (all anchored at now = T0)
   trail wipe (build traces)
   red01: Sliver, operator,
     payload freeze (all C2s)
        │
        └── SNAPSHOT EVERY VM ────────►  rollback target for round N+1
```

The restore point becomes **fully built, not yet compromised**. Everything expensive and
failure-prone (engine template build, golden conversion, windows bootstrap, domain joins,
the Sliver payload matrix) is paid once. Everything cheap and round-scoped (implant
deployment, wipe, seed, injects) is paid per round.

## 2. Why: the measured case

From `.deploy-timings.jsonl` across Nov 8–12 (parent/child overlap filtered per
`common/timing.py`):

| | |
|---|---|
| delivered deploys observed | 7 of 16 attempts (9 died: phase 7 ×12 across all logs, phase 2 ×3, phase 4 ×1) |
| delivered deploy wall clock | median **127 min** (27–652 m); clean cold samples 124 m and 127 m |
| the `red` stage (one timed block) | median **71 min**, up to 125 m — 31–68% of the deploy |
| `engine_template_build` | median 14 m (skipped on hash match) |
| a full test round today | 187 m = deploy 124 + verify 2 + staging 3 + event 60 |
| `snapshot` create cost | median 9.1 s/box, p90 15.2 s, at `max_workers=4` |

The `red` block is the single biggest cost **and** the single biggest failure source: the
Sliver payload matrix (`c2-prep`) appears 6× in the failure ledger, including one
`timeout after 7200s`, and "day-0 seed did not plant a checked-in C2 channel" 2× more. Every
rendreploy rerolls it. Under this plan it is built once and preserved — the existing
`payloads_ready` short-circuit (`red/tlaloc_ops.py:263`) already knows how to reuse a matrix
that is still on red01; today teardown destroys red01, so it can never fire between rounds.

## 3. Design

### 3.1 The boundary in code

It already exists, in the wrong place. `plant_assume_breach` (`red/tlaloc_ops.py:539`) runs,
in order:

| | step | stage |
|---|---|---|
| 1 | `red01_ops.deploy` — clone, network, engine rules | prepare |
| 2 | `resize_red_disk` | prepare |
| 3 | Sliver/Mythic install, operator, transports | prepare |
| 4 | `install_engine_rules` — DNAT/FORWARD on the engine | prepare |
| 5 | `stage_red01`, `push_config`, `verify_config_contract` | prepare |
| 6 | **payload freeze** (`c2-prep`) — "building all the C2s" | prepare |
| 7 | `payloads_ready` / `pull_payload_manifest` | prepare |
| 8 | `install_unit` (`:717`) — unit installed, not started | prepare (last step) |
| 9 | **`seed_via_red01` (`:719-720`) — deploying them** | **event start** |
| 10 | manifest + C2-channel verification | event start |
| 11 | `start_unit` / hold | event start |

The cut is between 8 and 9. No new machinery is needed to express it — only to move step 9+
behind the snapshot, and to stop `final.py` calling all of it inside phase 7
(`deploy_lib/phases/final.py:93`).

### 3.2 Restore-point set

Proxmox snapshots are per-VM, so one name carries one meaning across all three VM classes and
`SNAP_READY` keeps its constant (`common/constants.py:29`). `SNAP_BASE` is unchanged and stays
as the deeper fallback rung.

| VM | snapshot | taken at | holds |
|---|---|---|---|
| every managed team box + in-path firewall | `tz-ready` | end of prepare, after trail wipe | built, hardened, domain-joined, vulns planted, no implants |
| red01 | `tz-ready` | **after the payload freeze, before the seed** | Sliver + operator + frozen matrix + staged tlaloc, empty `/var/lib/tlaloc` |
| scoring engine | `tz-ready` | end of prepare, before `seed_teams` | event.conf + credlist + schema, no teams, clock stopped |

Templates (goldens, engine template) are **not** in the set — `qm` templates cannot roll back
(`docs/architecture.md:209`). They are already retained by hash, which is the existing
mechanism and needs no change.

What moves: `final.py` stops at the trail wipe, then snapshots boxes, red01 and the engine,
and returns. The red plant's steps 9–11, `plant_assume_breach`'s seed half, leave phase 7.

### 3.3 Event-start sequence

Every round runs the same sequence, and the real competition runs it too:

1. **Rollback (skip on a freshly prepared range).** Range-atomic, §3.5.
2. **Deploy implants.** New `deploy_red_at_start(ctx)` = seed → manifest + C2-channel check.
   Runs with the engine still paused.
3. **Trail wipe.** Removes the traces step 2 just made (see §3.4).
4. **Engine seed.** `phase8_seed` as today: `reensure_services`, `event_conf_drift`,
   `seed_teams(start=False)`, unpause, injects anchored at now.
5. **Start the loop.** `release_held_red` (`quotient/lifecycle_ops.py:68`) already does
   exactly this and is already called by `competition-ops.py start` at `:133`.
6. **T0.** The harness records it after staging, so red is present and attacking at T0 —
   no uncompromised window in front of the clock.

Steps 4–5 are already the `--hold-start` + `competition-ops.py start` path. This plan only
adds steps 2–3 to it.

### 3.4 The wipe runs twice, not moved

`final.py:112` is explicit: *"a rollback to tz-ready hands blue every deploy log back"* — the
wipe is inside the restore point on purpose. With planting deferred, the wipe must stay where
it is **and** run again at event start:

- **prepare wipe** (inside the snapshot) — removes nakon, domain-join and bootstrap traces.
- **start wipe** (after step 2) — removes the implant deployment's traces.

Running it twice is safe: it is idempotent, and it already reads red01's `world.json` for the
never-touch skip-list (`guests/clean_trails.py:161-163, 356-368` — live logs show "25 planted
path(s) on the never-touch skip-list"). Skip the start wipe and blue inherits the deploy
transcript, which is the exact defect the comment was written to prevent.

### 3.5 Rollback is range-atomic

Today `redeploy --mode reset` is a **per-box** ladder
(`redeploy/redeploy_reset_ops.py`): each box independently rolls back, and a box that stays
unhealthy escalates. That granularity is wrong for a preserved range, for two reasons:

- **AD consistency.** A rolled-back DC next to a live member is a dead secure channel. The
  ladder's own field report is this failure class: rung 2/3 force a re-join and clear
  `.nakon-domain-<team>-*` markers (`redeploy/redeploy_plant_ops.py:207-227`), and the Nov 8
  readiness run still ended with **3 team-2 boxes STILL BROKEN** after all three rungs
  (`.session-notes.md` run-2ef1679d, `scrim-one-red-team-report.md:289-294`).
- **Red consistency.** Boxes carrying implants from round N plus a red01 rolled back to pre-seed
  is the desync this plan exists to remove.

So the reset target is **the whole range, or nothing**: boxes, firewalls, red01 and the engine
roll back together, then the start phase re-arms everything. The per-box ladder survives only
as the degradation path for a box whose snapshot is missing or won't restore — which is what
`snapshot_support_hint` (`infra/lifecycle.py:95`) already reports on.

A useful side effect: the PAM restart trap is documented as living *"on the post-plant disk"*
(`redeploy_reset_ops.py:122`). Rolling back to a pre-plant disk should stop provoking it. Keep
the detection and the support bundle; it should simply stop firing.

### 3.6 Same range for the real comp — ownership mostly dissolves

I previously flagged run-id ownership as a blocker. On the same-range model it is not: the
dashboard already carries a range through its life.

`RANGE_ACTIONS` (`dashboard/runner.py:48`) — verify, destroy, redeploy, start, freeze, resume,
end, delete_team — plus `scrim --skip-deploy`/`--resume-event`, all resolve to the workspace
that **holds** the range via `_range_workspace` (`:456`), and act on that workspace's state
document. Ownership (`tezcatlipoca;comp-<id>;run-<id>`) therefore stays the deploy run's for
the range's whole life, and the final teardown still proves it.

Three rules follow, and they are operational, not code:

1. Prepare the range **once** with a normal `deploy`. Never `deploy` that competition id again
   while the range is alive — a second deploy builds a second range and `_range_workspace`
   then refuses the ambiguity.
2. Every round goes through `redeploy` / `start` / `scrim --skip-deploy`. A bare `scrim`
   seeds its **own** workspace and deploys (`runner.py:347-349`), which is a new range.
3. Do not retire the range between rounds. `range_retires` is the ledger for "gone"; a retired
   range refuses every action except the teardown that reclaims its kept templates.

Evidence accumulates per round under `<workspace>/.automated-tests/<run-key>/`, which is
where it already goes.

## 4. Code changes

Minimum set, in dependency order. No new abstractions; each item is a move or a parameter.

1. **`red/tlaloc_ops.py`** — split `plant_assume_breach` at the existing seam. Add
   `deploy_implants: bool = True`; when `False` it stops after `install_unit` (`:717`), records
   `state["red_staged"] = True`, and returns the same summary shape without a manifest. New
   `deploy_red_at_start(ctx)`: the `:719-742` half (seed, manifest, C2-channel gate), raising
   as it does today. Reuse the `assume_breach off` early-exit structure at `:742` rather than
   adding a second branch.
2. **`deploy_lib/phases/final.py`** — stop calling the seed half. Order becomes: domains →
   final pass → red prepare → late account-mode pass → trail wipe → snapshot boxes, red01,
   engine. Update the module docstring, which currently names red as part of phase 7.
3. **`deploy_lib/phases/_snapshots.py`** — `snap_ready` already takes one target; add the two
   call sites that are not in `ctx.all_targets` (red01, engine) or a small `snap_ready_all`
   that covers targets + those two by vmid. `ctx.all_targets` is teams × boxes only
   (`deploy_lib/targets.py:25-31`), which is precisely why red01 and the engine are outside
   the set today.
4. **`quotient/lifecycle_ops.py:89`** — `cmd_start` calls `deploy_red_at_start` before
   unpausing, then the start wipe, then the existing unpause/injects/`release_held_red`
   sequence. `release_held_red` already exists and already starts the loop; this only inserts
   the deployment in front of it.
5. **`redeploy/redeploy_reset_ops.py`** — new `--mode prepared` (range-atomic rollback to
   `tz-ready` on every VM incl. red01 + engine, no per-box escalation) as the loop's reset. Keep
   `reset` as the repair ladder for damaged ranges. `--reset-event` becomes redundant for the
   loop (the start phase re-seeds) — keep it for engine corruption only.
6. **`dashboard/runner.py`** — the `redeploy` action's default mode is already `reset`
   (`:163`); point it at `prepared` and add a `start` follow-on so one submit = one round.

## 5. Gates that make a reset trustworthy

A reset is only usable for the real comp if it can prove it landed clean. Before T0, refuse
the event unless all hold — reuse what exists:

| gate | mechanism |
|---|---|
| every VM's rollback returned ok | Proxmox task result per vmid (not fire-and-forget) |
| every box answers | `wait_for_boxes_ssh` + guest agent for Windows |
| every scored port open from the engine | `ports_closed_from_engine` (`redeploy_reset_ops.py`) |
| domain trusts intact | new: member→DC secure-channel probe; today only forced re-join covers it |
| red01 ready | `payloads_ready` true, `/var/lib/tlaloc` empty of prior-round state, unit installed-not-started |
| engine prepared | `/api/login` answers, no teams, clock not started |
| whole range | `verify-competition.py` PASS (already the pre-T0 gate, already strict) |

The last one is the real gate: `verify` is what a deploy claim is judged by, and it should
judge a reset the same way.

## 6. Instrumentation, and the number that decides this

`red` is one untimed split today (`timed(ctx.comp_dir, 7, "red")`, `final.py:93`), and slot-0
golden build has no `timed()` wrapper at all (`golden.py:54`) — there is an unexplained
83-minute hole in one historical deploy (`b3-4team`, between `push_event_conf` and
`apply_team_boxes`). Neither can be sized without instrumenting.

Add, so the first trial produces the answer:

- `red_prepare` and `red_deploy` as separate timed ops.
- `reset_rollback` (per VM and total), `start_wipe`, `start_seed` in the start phase.
- a `timed()` wrapper on slot-0 golden build.

Then the estimate becomes measurement. The arithmetic to beat, from the numbers in §2 — all
**inferred**, none measured:

| loop | per round (60 m event) |
|---|---|
| today: teardown + cold redeploy | ~3.1 h |
| redeploy with templates kept | ~1.7–2 h |
| prepared snapshot + start phase | ~1.2–1.8 h |

The win depends entirely on the prepare/deploy split of that 71-minute median: if the freeze
is most of it, per-round cost falls to roughly rollback + seed + seed-engine + event.

## 7. Risks

| risk | standing |
|---|---|
| **Double-plant** if the start phase runs without a rollback first | real; guard on a `red_deployed` state flag that the reset clears |
| Rollback of a 60 GB Windows box is not instant | stop+start, `SHUTDOWN_GRACE_S=120`, `START_GRACE_S=900`; serial today — measure, then decide on concurrency |
| Disk: red01 60 GB + engine + 2 snapshots per box | thin pools, but preflight math (`infra/resources.py`) counts provisioned bytes; headroom was 1207 GB free vs ~410 GB needed on the node used |
| Templates still can't roll back | unchanged; hash-keyed retention covers it |
| Engine snapshot pins secrets to deploy time | correct for a same-event loop; a credential rotation mid-range would desync snapshot and `estate-state.json` |
| Concurrent ranges on one node bounce its networking | pre-existing, not addressed here (`run.log` warns on every submit) |
| PAM trap | expected to stop firing; detection stays |

## 8. Rollout

1. Land the split + instrumentation behind nothing (it is behaviour-preserving for a
   deploy that still seeds at the end): `deploy_implants=True` keeps today's result.
2. Prepare one range with `deploy_implants=False`, confirm all three VM classes carry
   `tz-ready`, confirm red01's snapshot predates any `/var/lib/tlaloc` state.
3. Run the start phase from that prepared range. This is the first proof: red at T0, verify
   PASS, a full event.
4. Reset → start, three times, recording per-step timings and whether any box needed the
   repair ladder. **This is the go/no-go for using the range for the real comp.**
5. If clean across three rounds, run the real competition on that range.

The three-round trial is the deliverable to judge this on: we have 16 timed deploys and
**zero measured rollbacks** in the estate's history.
