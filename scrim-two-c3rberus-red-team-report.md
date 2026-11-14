# scrim-two × c3rberus — the first true two-lane night

> ## FINAL RESULT — 2026-11-14, `run-9b5c556a-run7` (supersedes nothing; the night has two sealed chapters)
>
> **The competition shape the user asked for — realistic red on both rails: the tlaloc
> campaign plus an opencode operator driving a real c3rberus platform against the estate,
> blue = quetzalcoatl kit, both lanes on `qwen3.8-flash-next` — RAN. GREEN, interaction
> score 128, 11/11 gates.**
>
> | measure | evt1 `run2` (T0 21:01Z, 91 min) | event R `run7` (T0 04:55Z, 90 min) |
> |---|---|---|
> | interaction score | 127 (11/11) | **128 (11/11)** |
> | red takedowns (campaign) | 69 | 79 |
> | red restore-reactions | 64 | 73 |
> | blue restorations | 46 | 40 (TTR 9–13 min) |
> | blue eradication events | 15 | 13 |
> | injects / evictions | 2 / 0 | 2 / 0 |
> | **red platform lane** | operator thread DIED at T+92 (uncaptured CycleTimeout) | **alive all 90 min**: 74 platform tasks (43 deploy / 17 generate / 10 implant), estate rows 409 → **423** |
>
> Both sealed clean (run2: 67 files, run7: 54 files re-hashed, 0 problems) under
> `tezcatlipoca-competitions/cde-2026/.automated-tests/`. Evidence for the platform half —
> the ledger teardown would otherwise destroy — is in `run7`'s
> `evidence-red/c3rberus-run4/platform-ledger.json` (exported at T+76, before the bell).

## What interaction reality looked like, minute by minute

This is what the artifacts actually show, not what the design promised:

- **Red fought on two clocks.** The campaign struck every ~4 minutes on schedule — stop,
  stop_mask, re-stop after every fix (79 takedowns, 73 restore-reactions). The operator
  lane was *staccato*: each cycle's opencode turn burned past the 900 s cap and got cut,
  yet the work landed asynchronously — platform tasks 58–67+ deploying implants/IOCs into
  the same boxes, and in the later cycles the operator pivoted to **countermeasures**:
  pruning its own schtasks-named implant channels when blue's remediation appeared
  (RED-TEAM.md `implant_channel` / `persist` lines). Nobody coded that behavior; the model
  did it, reading its own notebook.
- **Blue defended for real.** 46 named repairs across the night (watchdog.log, per-service
  `RESTORED` lines — team2's httpd/mysql got hit and healed five times each), 13
  eradication actions touching persistence, not just symptoms, both teams cycling rc=0
  eleven times. What blue *couldn't* see was the point: c3rberus is a red-side asset and
  no blue action ever reduced platform row count. A future scenario should give blue one
  visible hook into it.
- **The 900 s cap is the wrong knob for a first crawl.** Every sampled red cycle TIMEOUT
  line was cosmetic — thread re-armed 12 s later (the fix from evt1's death worked: five
  caps, zero thread deaths). But the bookkeeping reads ugly; raise to 1200 s or split the
  first prompt.

## The part no scoreboard shows: the 15-hour road to T0

Event R was the last 90 minutes of a night whose other 13.5 hours were spent teaching the
deploy engine to land this exact range. The headline numbers below are the *interaction*;
this is the *reality behind it* — a red01 rebuild (teardown at the previous bell destroys
everything, including the platform DB and its payload matrix — 30 implants, 62 min to
rebuild), then **ten distinct bugs, each one encoded as engine code plus tests, never as a
manual repair**:

| # | bug (symptom at the gate) | fix |
|---|---|---|
| 1 | stale seed doc vs rebuilt DB → operator login "Invalid credentials" | `200b3d9` `_bootstrap_admin` re-adoption ladder (seed pw → factory default → wipe-and-reseed, then rotate back) |
| 2 | `--keep-templates` leaked into a resume's argv → rc=2 | resume branch drops scrim-only extras |
| 3 | resume forwarded no identity knobs → refused against own live range (vmid 1278 vs planned 1000-era) | dashboard `35e0388`: knob loop + `from_phase` + `force_from_phase` |
| 4 | refused resumes' preflight ZEROED the phase cursor → stale-estate gate kept re-firing | `from_phase` param + force flag (escape hatch, honest in logs) |
| 5 | fetch-source probe "saw nothing" — blamed on :4000 warmup | `7c933bf` retry envelope (partly wrong theory, kept anyway — cheap insurance) |
| 6 | readiness gated only the API face while the script server lagged minutes | `e84a5ad` dual-face wait; `507ae7f` probe re-issue window |
| 7 | **the real root: state held a PHANTOM box password; the hop died at LOGON_FAILURE hidden behind `2>&1`** | `fe115cd` packet-over-state resolver + candidate passwords + HOPFAIL/LOCAL leg-named diagnostics; `84ce5fa` carry-path parity |
| 8 | estate gate refused a fully-planted range ("5/8 boxes") — fixed 25-row floor vs legit 24-row windows catalogs, read mid-straggler | `dcc7075` settle-poll + 80%-of-median health, `b818d31` one operator-style redeploy pass |
| 9 | deploy-all windows `put_file` races | cross-cred fallback (platform's own ladder, verified landing: local `red_scoring` → `MIRA-12x\red_scoring`) |
| 10 | **credentials.txt regenerated on the carry path lost the domain credlist row → the `packet_creds` gate refused the event 36 min before T0** | `0861d16` domain_creds packet-outranks-state; phase-8 doc refresh `d60ae84a` |

How we *knew* #7 was the root: 25 minutes of py-spy stacks (engine parked in an ssh wait),
a manual byte-exact repro of the failing hop that worked in 2 seconds from the same host,
then the leg test: `nxc` with the packet password printed **2 × Pwn3d**, with the state
password **0 matches**. The password the boxes actually held was `passwords.json`'s; the
engine's resume path had been consulting its own state file, which still carried a mint-era
value that was never stamped. The packet is the authority — the rule was already in
`inputs.py`'s docstring; the carry path had simply never obeyed it. That is what tonight
paid for: the rule now holds in the resolver (`_box_password`), the probe candidates,
`secrets.py` box_password/box_creds/domain_creds — everywhere, with tests.

And #10 is the counter-case worth keeping in the report: **the gate earned its keep.** It
refused to start an event whose published documents contradicted each other — blues would
have found a credentials.txt with no domain row while their packet named one. Everything
else was green at that moment (boxes live, stamps correct, rows planted). A doc-fidelity
gate catching a doc bug is the gate working exactly as designed.

## Verdict

READY — the way the user framed it: "a more realistic competition." The red side now
includes a platform the operator *steers* (an actual C2 UI's API, driven live by an agent,
leaving an auditable 74-task trail), the campaign keeps the metronome pressure, and blue's
kit took both without noticing the second lane exists — which is precisely the asymmetry a
platform lane should create.

## Recommendations — next iteration

1. Red cycle budget 1200 s (or a two-prompt cold start: recon turn, then ops turn).
2. Wire the platform-ledger export into collection itself — teardown timing shouldn't
   depend on a human thinking of it at T+76.
3. Payload-matrix survival across bells: snapshot-after-c2-prep or artifact import — the
   62-minute rebuild is the single biggest recurring cost (logged in .session-notes.md).
4. Give blue one visible hook into the platform (banner, cert on a shared port) so the
   red-lane asymmetry is contestable rather than invisible.
5. Fleet-scale events: this geometry (2 teams) held 8 boxes/224+ rows/two agent lanes
   through a rebuild night; the resume machinery is now packet-authoritative and leg-named —
   ready to multiply teams.

---
*Provenance: engine `0861d16` @ proxmox node, comp `run-4f8dce5f/comp`; event run
`run-3abc35ab` (scrim, skip_deploy); all numbers re-hashed into the two seals; the 15-hour
saga's primary record is `/home/hna/tez-dashboard-data/.session-notes.md` and the engine
git log. Prior chapter: `scrim-one-red-team-report.md`.*
