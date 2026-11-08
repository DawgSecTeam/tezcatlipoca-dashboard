# scrim-one × quetzalcoatl — 2-hour competition report

> **CORRECTION (2026-11-08, after the readiness re-diagnosis).** The verdict below — "red did
> not move the score" — is WRONG, and the error was mine. Every scrim after the first reused a
> range that red had already destroyed and that no reset had touched: a fresh deploy did start
> **12/12 pins UP on both teams** (`run-1b74a06f-run4/verify.log`, `monitor.log` T+0), and red
> then took **every non-SSH scored service on both teams down inside 14 minutes** by renaming
> binaries (`.tlaloc-off`) and configs (`.tlaloc-bak`) — damage a `systemctl start` cannot undo.
> Runs 5, 6 and 7 inherited that corpse without a reset, so their scoreboards were flat *before*
> their red ever acted. Run 7's red did one takedown because there was nothing left standing.
> Red is not weak; the posture is **decisive to the point of ending the game**, and my measurement
> was invalid. What survives from the analysis below is the harness finding (a non-gating
> services verify let a degraded board start) and the readiness work in §9.

Prepared 2026-11-08. Engine `scrim-one`, red posture `red-aggressive.yaml` (the brutal
tier), 2 teams, 120-minute event clock, models `k2` through the gateway at
`10.0.0.212:4000`, clef decision server at `10.0.0.143:8080`.

**The short answer to "how good is the red team": in this run, red did not move the
score. It held 12 footholds (4 on Windows), planted 78 artifacts and made 18 impacts
over 120 minutes, and the scoreboard did not change once — every pin it touched was
already down before it arrived. Blue did not move it either: it fixed none of the pins
that were broken at T0. The competition ran end to end and produced a flat line for two
hours, and the harness's own gates grade it NOT READY (interaction score 3, 7 of 9
gates failed).**

That is a real result, and a more useful one than a flattering one. Details below, with
the evidence paths.

---

## 1. What actually ran

| | |
|---|---|
| Run | `run-cee41075` (dashboard id) → engine id `run-1b74a06f`, 7th scrim on this range |
| Artifact dir | `~/tez-dashboard-data/runs/run-d6fb9214/comp/.automated-tests/run-1b74a06f-run7` |
| Event clock | 120.7 min, T0 2026-11-08 14:03 UTC |
| Outcome | `succeeded` — capture, reports, teardown, rc=0 |
| Estate after | **0** scrim-one VMs (verified against the realm API). One unrelated bridge `vmbrRe0309a7d` is still on the node — its run id is not this range's, so this teardown correctly did not touch it |

Two teams (105, 106), 7 boxes each: dc01, win02, web01, mail01, db01, splunk01, fw01
(pfSense in-path), 12 scored pins each. Red ran on red01 (vmid 1660, 10.0.0.246) with
Mythic C2 via Gaia, seeded day-0 across all 12 boxes (`assume_breach: full`).

The blue-kit — the quetzalcoatl scripts — was staged into both blue workdirs (7 files
each: `autofirewall.sh`, `watchdawg.sh`, `auditd-rules`, `auditkey-notifier.sh`,
`baseline/`, `port-sources/`, `README.md`) and described in the cycle prompt.

## 2. Interaction reality (this is the part that matters)

The scoreboard was sampled 112 times over 120 minutes. It never changed:

```
T+  0min  team1 6/12  team2 5/12
T+ 29min  team1 6/12  team2 5/12
T+ 59min  team1 6/12  team2 5/12
T+ 89min  team1 6/12  team2 5/12
T+118min  team1 6/12  team2 5/12
```

The pins down at T0 — `win02-ftp`, `dc01-ldap`, `mail01-imap`, `db01-sql`,
`splunk01-splunk`, `dc01-winrm` (and `web01-http` for team2) — were the same pins down
at the end. Blue's own baseline entry agrees (`T+0` LOG: 6 down) and so does its
end-of-event list (`still DOWN (since T+0)` for every one).

Down-time accumulated accordingly: **team1 713 box-minutes down, team2 829**, with a
maximum of 6 and 7 pins down simultaneously.

## 3. Red

> Read with the correction: run 7's red had almost nothing left to take down.
> Run 4's red had everything standing and killed all eight non-SSH pins in 14 minutes.

From the tlaloc after-action (`RED-TEAM.md`) and `INTERACTION.md` (221 red events):

- **Impacts 18** (team1 7, team2 11) · **actions 36 ok / 0 failed** — mechanically clean.
- **Footholds 12**, all root/admin paths, **4 on Windows**; **78 artifacts** planted
  (34 live, 44 dead); **12 C2 callbacks**; seeded on 12 boxes.
- **Tactics actually exercised: 2** (persist, takedown) out of the ladder's repertoire.
- **Initial access: 0 techniques** — expected, because `assume_breach: full` starts red
  with footholds; there was no access phase to score.
- **Takedowns: 1** — `T+23 2:db01 mysql (stop_rename_config)` — and `db01-sql` was
  already down at T0, so it produced no scoreboard change.
- Takedown spread 100% on one target; **restore-reactions 1** (a re-kill after a blue
  restore); **stalls ≥10 min: 0**.

So red was present, persistent and reliable, and almost entirely non-disruptive to the
scoreboard. With the most aggressive posture loaded — a ladder whose brutal tier can
stop, mask, block ports and rename configs on up to 99 boxes per team, unrecoverable —
it chose to consolidate footholds rather than to take scored services away. Three
readings, in order of likelihood:

1. **The decision policy is conservative under `assume_breach: full`.** With footholds
   already held and nothing to "achieve", the clef-scored choice repeatedly favoured
   persistence/implant work (which is visible in the restore table: schtasks, Run keys,
   WMI subscriptions, systemd units, chattr immutability, timestomping) over takedowns.
2. **Takedowns of already-down pins are invisible.** Red took down `db01 mysql`, a pin
   that was down from the seed. If the seed leaves 6-7 pins down, red's most natural
   targets are pre-broken and its work cannot register.
3. **The blue-watchdog restores services between cycles**, so a short takedown can be
   erased before the next scoreboard sample. The 1 recorded restore-reaction and red's
   "blue restores seen: 3" suggest this is a contributing factor, not the main one.

## 4. Blue

From `BLUE-TEAM.md` and the interaction gates:

- **8 cycle attempts: 2 finished rc=0, 4 hit the 10-minute cap, 2 were cut by the event
  window.** Notebook liveliness 4 entries. opencode never wrote `REPORT.md`, so the
  harness synthesized the team narrative — blue did not produce its own after-action.
- **Restorations: team1 0, team2 1** (time-to-restore 21 min, above the 15-minute bar).
- **Injects: 1 submitted of 12 published** (2 submissions counted at capture across
  both teams).
- Work on boxes was real: 45 `./mybox <box>` invocations (13 splunk01, 8 db01, 8 dc01,
  8 win02, 6 mail01, 2 web01) — investigating splunk's unit files, chasing the rogue-admin
  inject, checking sshd and service state.
- **The quetzalcoatl kit was never invoked.** The prompt advertised
  `./blue-kit/autofirewall.sh -d`, `watchdawg.sh` and `baseline/standard.sh`; the only
  occurrences in the cycle logs are the prompt text itself. Blue preferred ad-hoc
  `./mybox` commands. The kit is staged and documented but not yet *used*, which is the
  gap between shipping a toolkit and changing behaviour.

Blue's failure mode is not passivity — it is **spending its one action per cycle on
investigation and injects while the day-0 broken pins stayed broken**. Its own baseline
listed 6 down at T+0 and the same 6 at the end.

## 5. Why the participants were throttled

Two defects cost blue most of its cycles, both now fixed and pushed:

1. **The managed opencode server was reaped after the pipe drain instead of before it**
   (`scrim/blue_agent.py`). The `serve --service` grandchild runs in its own session, so
   `killpg` misses it, and it holds the cycle's stdout — so `communicate()` blocked until
   its idle timer expired (~16-19 min) *before* the reap that kills it could run. Every
   capped cycle overran by a quarter hour: in the first full event blue logged 1-2
   entries in 85 minutes.
2. **The reap matched on `HOME` only**, which bun rewrites for its children, so the match
   missed the very process holding the pipe. It now matches the cycle's workdir as well
   (unique per team per run).

With both fixed, cycles land at ~10 minutes (the cap) instead of ~25: 8 attempts in
120 minutes rather than 3. `CYCLE_TIMEOUT` is 600 s by design; a reasoning model mostly
spends it, which is a capacity limit to tune (`TEZ_CYCLE_TIMEOUT`) rather than a bug.

## 6. The pipeline work this took

The competition could not be run at all at the start. Getting to a completed 2-hour
event took a chain of genuine defects, each fixed at its cause and pushed:

| Defect | Effect | Fix |
|---|---|---|
| `providers/proxmox/` never shipped — a bare `proxmox` line in `.gitignore` matched the directory | deploy died at import | anchored `/proxmox`, restored the provider |
| headscale policy read at the container path, then at the host path, then its groups not carried, then the empty group rendered `[,]`, then a dropped group stranded its grant | 5 consecutive deploy refusals at the policy gate | host path for reads, container path for `policy check`, groups carried, empty group renders `[]`, a grant whose group is gone is dropped |
| team-clone path never gc'd orphaned zvols | `zfs error: dataset already exists` on the next clone | gc before clone, as the golden path already does |
| Windows static address: the guard was per-adapter, then the duplicate lived in the WMI instance store | `Instance MSFT_NetIPAddress already exists`, every team clone | assign through `netsh` (which ignores the instance store) with a read-back that fails loud |
| `run_tree` timeout handler called `communicate()` twice | Python 3.13 `AttributeError`, masking the real timeout | read the dead child's pipes directly (`_reap`) |
| `_feed_stdin` closed the pipe, then `communicate()` flushed it | `ValueError: I/O operation on closed file` in the fire test | `communicate(input=...)` owns the write/close/flush |
| Quotient DSN named `quotient_database` instead of the compose service `db` | `quotient-server` crash-looped, engine never answered, deploy timed out at phase 7 | fixed upstream (DevBot) — this was the last deploy blocker |
| red-stage ctx read `engine_mgmt_ip` from a state document that does not carry it | `ssh: Could not resolve hostname :` in red staging | fall back to `credentials.txt` |
| model parameter given the backend's name (`k2-horizon-7b`) instead of the gateway's group (`k2`) | every blue cycle `Invalid model name` — the first event was red-vs-empty-room | use the gateway id; the empty-room run was cancelled, not reported |

## 7. Verdict

> Scope: these gates grade **run 7 only, on a board red had already destroyed in run 4**.
> They are an accurate record of that run and are *not* a measure of red's ability —
> see the correction at the top and §9.

The harness's gates (`docs/rehearsal-gates.md`), machine-generated at capture:

| side | gate | value | threshold | verdict |
|---|---|---|---|---|
| red | takedowns | 1 | ≥ 6 | **FAIL** |
| red | restore_reactions | 1 | ≥ 3 | **FAIL** |
| red | distinct_tactics | 2 | ≥ 4 | **FAIL** |
| red | windows_footholds | 4 | ≥ 1 | PASS |
| red | max_simultaneous_down | 7 | ≥ 4 | PASS |
| red | stalls | 0 | == 0 | PASS |
| blue | cycles_rc0 | 2 | ≥ 2 | PASS |
| blue | restorations | 1 | ≥ 2 | **FAIL** |
| blue | injects | 1 | ≥ 2 | **FAIL** |
| blue | notebook_entries | 4 | ≥ 10 | **FAIL** |
| blue | timeouts | 4 | ≤ 2 | **FAIL** |

**NOT READY — interaction score 3.**

What this run does establish: red's implant/C2 path works end to end (12 footholds, 12
callbacks, 78 artifacts, zero failed actions), the estate stands up and tears down
cleanly, and the harness can capture and grade a two-hour event unattended.

What it does not establish: any measure of red's *disruptive* skill. A red team that
holds every box and takes down nothing that was up is untested in the only dimension the
scoreboard rewards. The competition needs a scoreboard that starts near-full so red's
takedowns are visible and blue's restorations are meaningful.

## 8. What to do next

1. **Fix the T0 baseline.** 6-7 of 12 pins down at T0 makes the event unscoreable — red
   cannot take down what is already down and blue's restorations cannot show. The seed's
   plant set should leave the estate green, with the misconfigs discoverable rather than
   pre-breaking the scored services.
2. **Re-run with the cadence fixes** and `TEZ_CYCLE_TIMEOUT` tuned to the model (or a
   faster model for blue), so blue gets ~20 actions instead of 8. Expect blue's
   restorations and inject submissions to move first.
3. **Then judge red on takedowns, restore-reactions and tactic spread** against the gates
   above — not on foothold count.
4. **Make the kit reachable and cheap**: blue ignored a documented toolkit in favour of
   ad-hoc commands. Either name the kit's one command at the top of the cycle task, or
   have the watchdog itself run `autofirewall -d`/`watchdawg` so the baseline hardening is
   the floor rather than a choice.

## Evidence

- `INTERACTION.md`, `RED-TEAM.md`, `BLUE-TEAM.md`, `REPORT.md`, `collection.json`
- `scoreboard-state.jsonl` (112 snapshots), `final-scoreboard.json`,
  `final-services-team{1,2}.json`
- `blue-team{1,2}/` — LOG.md, NOTEBOOK.md, `cycles/*.output.log` (45 `./mybox` calls)
- `evidence/{red,blue,engine,harness}/`
- Throttled earlier event (kept for comparison): `~/tez-dashboard-data/evidence-run5-throttled`

---

## 9. Readiness work that followed the correction (2026-11-08)

The gates cannot be satisfied by a board that starts dead, nor by a posture that removes blue's
ability to answer. Five changes, each at its cause:

**Harness (tezcatlipoca-engine, pushed).** The pre-T0 authoritative verify now runs
`--strict-services` and refuses to start the clock unless the services row PASSes, naming
`team:pin` for anything down — including the "no round scored yet" shape, which has no DOWN lines
to find (`scrim/red_stage.py`, `tests/scrim/test_pret0_pin_gate.py`). Phase 8 re-ensures every
scored service *after* the phase-7 disruption pass, which runs after phase 6's `fix_services` and
could leave a pin stopped (`deploy_lib/phases/seed.py`). `splunk`/`splunkd` joined the bring-up
loop — the scored splunk pin had nothing that ever tried to start it (`guests/service_fixup_ops.py`,
golden regenerated). The per-cycle cap became a run parameter (`--cycle-timeout`), threaded
through the queue (`dashboard/runner.py`).

**Competition posture (tezcatlipoca-competitions, pushed).** `scrim-one/red-sustained.yaml`: a
recon-only opening tier, then `stop` / `stop_mask` / `block_port` — never a rename — with
`unrecoverable: false`, `max_down: 4`, `min_standing: 2`, `pause_after_restore_min: 5`, and
`duration_min: 120` (the shipped value of 60 silenced red halfway through a 120-minute event).
The Compfile now pins it; `red-aggressive.yaml` stays for endgame drills, documented as the
posture that takes the board in ~14 minutes.

**The cadence root cause was the model, not the cap.** `k2` is a reasoning model: ~3 s on a toy
prompt but 25–30 minutes inside an agent cycle, so every cycle hit the cap. Benchmarking the
gateway's ten models (`bench_models.py`): `deepseek-flash` answers in **1.1 s with working tool
calls**, `qwen3.8-flash-next` 5.5 s. A rehearsal must run blue on a fast tool-calling model; the
cap only bounds the damage.

**Reset discipline.** `redeploy-competition.py --mode reset` is the documented ladder
(`tz-ready` → `tz-base`+replant → rebuild). Measured: a range that has been through a red wipeout
*and* a partial blue does not reset reliably — all three rungs failed and left three team-2 boxes
`STILL BROKEN` (auth setup failed, "SSH unusable"). For a rehearsal, **a fresh deploy is the
dependable reset**; a reused range is a trap, and the strict gate now makes that trap visible
instead of silent.

**Two queue defects found while driving this.** A run the dashboard marks `failed` can leave its
driver child alive holding the scoring-engine flock (blocked two resets until killed by pid); and
`POST /api/runs` performs the engine-ref fetch synchronously, so a slow GitHub fetch makes the
client time out while the run is still queued — which is how three duplicate redeploys were
created.

**Not yet proven:** a gate-passing run. The last attempt (`run-2ef1679d`, fast model, bounded
posture, strict gate) built its range and then died in the engine's `docker compose build`
(`go mod download`), because the lab had **no outbound HTTP/HTTPS at all** — `proxy.golang.org`,
`registry-1.docker.io`, `archive.ubuntu.com` and `api.github.com` all timed out from the queue
host. That is an egress outage in the lab, not a defect in this tree; the range was reclaimed
(0 scrim-one VMs). When egress returns, the run that proves readiness is:

```
action=scrim comp_id=scrim-one engine_ref=main teams=2
params: duration_min=120 cycle_timeout=900 scoring_vmid=3000 team_identifiers=105,106
        engine_mgmt_ip=10.0.0.243 red_vmid=1660 red_ip=10.0.0.246
        red_model=deepseek-flash blue_model=deepseek-flash
        llm_base_url=http://10.0.0.212:4000/v1 blue_watchdog=true
```

A pass looks like: pre-T0 verify reports 12/12 on both teams and the gate lets the clock start;
blue completes cycles rather than hitting the cap (≤2 timeouts); red takes ≥6 pins, blue restores
≥2 with red re-killing ≥3; ≥2 injects submitted; ≥10 notebook entries.
