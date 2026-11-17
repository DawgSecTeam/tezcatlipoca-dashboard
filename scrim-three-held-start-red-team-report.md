# scrim-three × held start — five gates that read intent, and the range that never got to play

Night of 2026-11-16/17. Competition CDE-2026, two-lane red (tlaloc campaign **plus** an opencode
operator driving the live c3rberus platform) against blue teams 127/128, both lanes
`qwen3.8-flash-next`, 90 minutes, `cycle_timeout 1200`. Artifact:
`tezcatlipoca-competitions/cde-2026/.automated-tests/run-84c0e45d-run5/` — sealed and clean.
Machine verdict: **NOT READY — interaction score 59, 8 gates pass / 3 fail** (run7, the healthy
tail of last night, scored 128).

## What interaction reality looked like, minute by minute

Blue was **present on both lanes for all 90 minutes**: two agent processes alive, three recorded
cycles per team, 21 restorations credited, 43 watchdog restore lines, 14 notebook entries. Red was
**loud and effective**: 40 impacts, 34 restore-reactions, 4 distinct tactics, 8 footholds (4 on
Windows), peak 4 services down at once, 0 stalls — every red gate PASSed.

Three things made it NOT READY, and none of them is an infrastructure failure:

1. **No blue cycle ever finished.** All six blue cycles and all five red-operator cycles logged
   `TIMED OUT at the cycle cap` (two more were cut by the window). `cycles_rc0 = 0` against a
   threshold of 2. The defence we measured was mostly the harness watchdog restarting units, not
   blue thinking: with 1200 s per cycle and one LLM lock serialising two teams over 90 minutes,
   each side got roughly three turns and completed none of them.
2. **The campaign was lopsided.** `Impacts recorded: 40 (team2: 37, team1: 3)` — after T+22 team1
   was never touched again while the loop repeated `stop_mask` on 128.4-web01 and 128.5-db01 for
   the last hour. Half the range was an empty room. `stalls == 0` passed anyway, because the loop
   was busy, and no gate measures per-team balance — so the machine saw a healthy red.
3. **Blue's scored objectives went untouched:** 0 inject submissions (1 open), 0 evictions.

Red's platform lane did real work under the same cap: platform tasks 57 → 65, active IOC rows
465 → 485, with 34 failed / 30 done task outcomes — the async deploys landed even though no
operator turn finished, the same "cosmetic, not functional" reading as run7.

## The part no scoreboard shows: seven hours of gates reading intent

T0 did not arrive until **05:01:18Z**, after a 23:35Z deploy and **four discarded events**. Every
one of the five engine defects tonight was the same species: a check that read what the code
*intended* instead of what the estate *did*.

| when | what happened | the intent-vs-effect gap |
|---|---|---|
| 02:32Z | deploy #1 refused: "only 6/8 catalog-carrying boxes" | the gate compared each box to `int(0.8 × estate median)`; a **complete** 24-row domain controller measured as unplanted → `67736e2` |
| 00:56:56Z | a twin's dashboard restart marked a **moving** deploy `failed: orphaned` | the row read "no worker", the driver was alive holding the flock and writing its own log fd — judged from evidence, never cancelled |
| 03:36Z | event #1 died pre-T0: fire test polled `ValueError: unexpected payload: None` for 14 min | `/api/services/<tid>` is literally null until a range's **first** start; preflight was already checking all 9 pins. Every fire test that ever passed ran on a range started before → `027a420` |
| 04:05Z | event #2 refused at pre-T0 verify: `injects : FAIL` | `--hold-start` defers `create_injects` to `start`; the range was fine and the suggested fix was an operator flag naming a structural fact → `e387eb2` (plus `finish.py`'s "(N inject(s) loaded)", a local count printed as an engine fact) |
| 04:27Z | event #3 refused at red staging: "449 rows, 6 healthy of 8 box IPs" | the health rule fixed in `plant()` was **still live in `ensure_live()`**, three functions away, untested → `e5d9c5c` |
| 04:55Z | event #4 reached T0, blue cycling rc=0 — and was cancelled after 25 min | the scrim started **red's** platform while `--hold-start` deferred the **scoring engine's** clock, which a `--skip-deploy` scrim never calls: 90 minutes of preflight, nothing recorded → `b2d0f3b` + `8c250cc` |
| 05:01:18Z | event #5: `START: competition/start=200 (the clock is running)`, `injects : PASS`, `pins_registered : PASS`, adopted 465 rows on 8/8 boxes | the first fresh-range event this pipeline has ever run end-to-end without an agent in the loop |

The ordering bug is the one that hurts: it needed a deploy with `--hold-start` **and** a scrim with
`--skip-deploy` — a combination that had never been exercised, because every previous event lived
on a workspace that had already been started at least once. That is precisely what a practice run
is for, and it is why tonight's report is written at 07:00Z instead of 04:00Z.

`b2d0f3b` also ships a lesson in process: I pushed the T0 call without updating the three
harnesses that drive `main()` and stub every other side effect of it, so three tests failed on
main for ~6 minutes until `8c250cc`. Run the tests that drive `main()` before pushing a change to
main's flow.

## Verdict

**NOT READY, honestly diagnosed.** No lane died: both red lanes were alive and both blue teams
cycled. What died was *scoring depth* — zero completed agent turns (cap × serialised LLM lock)
and a campaign that abandoned team1 after T+22. Five engine defects fixed and pushed with tests,
one dashboard defect (orphan reconciliation) left open on purpose because shipping it requires a
restart that would orphan the twin's live deploy. The pipeline can now deploy a fresh range and
host a held, skip-deploy event with no manual intervention — that is tonight's real gain, and it
costs four discarded events to buy.

## Recommendations — next iteration

1. Fix the cycle economics before the next two-lane night: 120 min duration, or `cycle_timeout`
   1800 s, or a separate litellm key per team so the shared lock stops serialising turns. Tonight
   proved the harness can run an event whose agents never finish a thought.
2. Add a per-team balance gate (each team ≥ 25% of red impacts, or ≥ N impacts per team per 15 min)
   to `docs/rehearsal-gates.md` — and make the campaign's per-team state a collected artifact,
   since red01 is destroyed at teardown and the 37/3 imbalance is now unroot-causeable.
3. Land the dashboard's `reconcile_orphans` liveness probe (flock + log-fd), when no run is
   moving.
4. Pull the c3rberus ledger inside teardown collection instead of a manual pre-bell export — this
   is the second night running it had to be done by hand.
5. Re-run this exact shape (fresh deploy → `--hold-start` → `--skip-deploy` scrim) on a **new**
   competition ID to prove the fixes are not cde-2026-shaped; tonight's five all live in shared
   code, which is encouraging, but only a second range proves it.
