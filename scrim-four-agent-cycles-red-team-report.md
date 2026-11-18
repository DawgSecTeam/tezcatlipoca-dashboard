# scrim-four × agent cycles — the endpoint-death failure mode is gone; the cap is now the wall

Night of 2026-11-17/18. Competition CDE-2026, two-lane red (tlaloc campaign **plus** an opencode
operator driving the live c3rberus platform) against blue teams 127/128, 90 minutes,
`cycle_timeout 1200`, `--blue-watchdog`, templates kept. For the first time the three agent lanes
ran on **different models** — blue1 `qwen3.8-flash-next`, blue2 `qwen3.8-27b`, red operator `k2` —
so this run could tell a model problem from a harness problem.
Artifact: `tezcatlipoca-competitions/cde-2026/.automated-tests/run-808cfc2c-run2/` — sealed and
clean.
Machine verdict: **NOT READY — interaction score 65, 11 gates pass / 1 fail**
(previous run: 59, 8 pass / 3 fail).

## What interaction reality looked like

Blue was **present and answering for all 90 minutes**: 7 blue cycles completed `rc=0`
(run-84c0e45d: zero), 24 restorations against 34 takedowns, 37 explicit restore events seen by
red, 19 of 24 restores inside 15 minutes, 2 injects submitted, 5 eradications, 18 notebook
entries. Red was loud: 34 takedowns, 5 distinct tactics, 4 Windows footholds, 49 successful
actions to 3 failed, 0 stalls.

That is a tug-of-war, not a red-versus-empty-room run: red took `db01`'s mysql down 19 times and
blue put it back 19 times, red put `red_scoring` in ftp01's local Administrators and blue found it.

## The failure this run was built to test — and it did not occur

Last run: 11 cycles, 0 `rc=0`, every one dead at the cap with opencode's retry trail
`ECONNRESET → attempt 2 → attempt 3 → HTTP 524` burning the budget.

This run: **four cap-timeouts, zero stream-drop signatures.** `grep -c 'ECONNRESET\|524\|retrying'`
on all four partials returns 0. Every timed-out lane was *working* when the clock cut it:

- blue1/flash-next deleted the attacker's `blk ldap` firewall rule on ad01, re-enabled FTPSVC
  after red set `StartType=Disabled`, submitted inject #1 at T+6, then found `red_scoring` in
  ftp01's local Administrators with a LastLogon timestamp inside the same cycle.
- red/k2 read the platform source, drove `/c2/iocs/deploy-and-register` (task 66) and polled
  `score_history` — a long multi-step operator turn, cut at 1200 s.

Before the event, 12 production-path cycles across the same three models completed `rc=0`
(4 each, serial + 3-way + 6-way waves, worst case 402 s) — so the harness, the staging and the
endpoint were all sound; what the event did was put a *scope* on top of them.

## The one failing gate: timeouts 4 > 2

Not a defect in the agents. The lanes that missed are the lanes that choose unbounded work:
flash-next's open-ended account sweep (3 misses) and k2's platform API sequences (2). team2 /
qwen3.8-27b ran 6–8 minute cycles and completed six of them. The fix is calibration — a cycle
budget the prompt respects, or a per-lane cap — not another harness change.

## Red balance: 37/3 → 26/8, and now gated

The previous run's worst finding was `impacts_by_team {team2: 37, team1: 3}` passing every red
gate. Two changes landed before tonight: tlaloc's breadth guard no longer keys on a cap that
defaults to zero (`16140d9`), and `min_team_takedowns` now gates the *split*, not just the total
(`2ea91ba`). Tonight: **team1 26 / team2 8**, and the new gate read `8 >= 8 PASS` where the old
aggregate gate would have passed 37-vs-3 too. red still favours `team1:db01` (56% of takedowns on
one target) — rotation health worth watching, no longer invisible.

## Six defects found and killed at the root tonight

| defect | evidence | commit |
|---|---|---|
| Both blue lanes serialised on one lock because locks keyed on the URL only | `scrim/event_run.py` | `969bf8b` |
| Breadth guard dead when `max_down` is unset (the 37/3 cause) | `tlaloc/policy.py` | `16140d9` |
| PowerShell `Where-Object {$_}` reaching PowerShell literally through mybox | blue partial logs | `bf89681` |
| Aggregate takedown gate satisfiable by hammering one team | 37-vs-3 after-action | `2ea91ba` |
| A scrim's own deploy started the red loop before T0 | tlaloc's takedowns at 03:10-03:16 while the event had not begun | `e8eecb7` |
| `run_tree` could not be handed to a helper as a `runner` | `TypeError` in the teardown's red-state pull | `b88e7b0` |

Two more were found and fixed in the deploy path on the way to the event: red01's SSH was never
proven before the deploy used it (cloud-init's failed `package_upgrade` left `ssh.socket` stopped,
reported misleadingly as "disk could not grow"), and the netplan pin that exists for the
follow-on reboot ran *after* the SSH wait (`0e32153`, `7eb3231`).

Each fix carries a test that fails without it — including the 37-vs-3 replay and the
`--hold-start` assertion.

## What is still not ready

- **`timeouts 4 > 2`** — the honest reason for NOT READY, and a calibration question.
- **Red's rotation** still concentrates on one box.
- **Blue's `EVICTED:` lines (9) are self-reported**; red's own health checks saw 0 evictions, so
  what blue removed from red's footholds is unverified.
- **The platform ledger is a T+75 export only** — the automated teardown pull failed (defect 6)
  and the last ~15 minutes of c3rberus task history is gone with the DB.

## The path here, since it was not a straight line

The first event attempt (`run-46f88c2d`) was refused by its own pre-T0 gate with 4 pins down, and
it was right to refuse: the deploy that preceded it had released red twelve minutes before the
bell (defect 5), and red had masked mysql on a box that was never scored for it. The range was
recovered with `redeploy --mode reset --boxes ad01,ftp01` — the engine's own `tz-ready` ladder,
not hand repairs — and the second attempt started from a verified green estate (9/9 both teams).
