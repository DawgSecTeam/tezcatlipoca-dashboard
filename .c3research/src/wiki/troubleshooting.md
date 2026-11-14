# Troubleshooting

Recovery procedures for the most common operator-facing failures.

## NXC auth failures

**Symptoms**: bulk exec / IOC deploy / cred revalidation reports the box as
unreachable; the credential row flips to `valid=false`; `box_unreachable`
alert fires after `NXC_DEGRADED_THRESHOLD` failures inside
`NXC_DEGRADED_WINDOW_MINUTES`.

**Scoring vs operator creds**: the IOC check / deploy / remediate
pipeline authenticates **only** with the `red_scoring` infrastructure
cred (`is_infrastructure=true`), not operator-added or harvested rows.
If scoring (not an operator action) reports a box unreachable, the
fix is the scoring account, not a new operator cred — re-run
**VALIDATE SCORING ACCT** / `POST /admin/scoring-cred-preflight` and
re-provision via `scripts/infra/create_red_scoring.py` (see
[Running a competition § scoring-preflight](running-competition#scoring-preflight)).
A `scoring_cred_down` alert (critical severity) fires in the Alert panel
the moment the scoring account itself dies on a box — the fastest signal
that this is the scoring account, not a routine per-box NXC blip.

**First test: local vs domain creds.** Inventory boxes carry both a local
(`username`, e.g. `.\Administrator`) and a domain (`domain_username`, e.g.
`TEAM3\Administrator`) account. Trying both takes seconds and splits the
search space in half, because domain auth needs pass-through to a DC and
local auth does not:

```bash
nxc smb <ip> -u '.\Administrator'      -p '<pw>'   # local  — no DC involved
nxc smb <ip> -u 'TEAM<N>\Administrator' -p '<pw>'  # domain — needs a live DC
```

| local | domain | Diagnosis |
|---|---|---|
| fail | fail | the box itself — down, filtered, or creds rotated |
| **pass** | **fail** | **the box is fine — the DOMAIN is broken.** Go to [Kerberos clock skew](#kerberos-clock-skew) and check the DC's ports + clock, not this box |
| fail | pass | local account rotated/disabled |

Which one a tool uses matters when reading its output: `check_cpu_usage.py`
authenticates Windows boxes with `domain_username`, so it reports
"auth failed" for a box whose local auth is perfectly healthy.

**Common causes** (operator-action NXC failures):

- Blue team rotated the password — register a new cred via
  `[Operator Actions § add-creds](operator-actions#add-creds)`.
- WinRM / SMB filtering at the host firewall — verify port 445 (SMB) /
  5985 (WinRM) reachable from the C3rberus host.
- Bad protocol choice — `nxc` SMB cred won't auth a WinRM-only call; pick
  the right cred-type for the action.
- `NXC_BATCH_IOC_SCRIPT_SIZE_LIMIT` exceeded — bump the env var if the
  combined IOC script size has grown.

**Where logs live**:

- Backend: `logs/fastapi_backend_<ts>.log` (the most recent file).
- Test runs: `logs/pytest_<ts>.log`.
- `scripts/diagnostic/check_access.py` — one-shot per-box NXC sanity check using
  inventory creds.

## Implant deploy / generate "hangs" (it is probably queued)

Symptom: an implant deploy or pivot-set generate appears to do nothing for minutes; an
API client that waits on the HTTP response times out (`httpx.ReadTimeout` in tests).

Both endpoints are **asynchronous** — they answer `202 {task_id}` immediately and do the
work under an `OperatorTask`. Nothing is wrong if the task sits a while: every implant
compile serializes through one global Sliver mutex, so concurrent requests wait their
turn. Check the Task Queue (`/c2/tasks`):

- `running (queued) · position N` → waiting behind N other compiles. Expected; wait.
- `running (compiling)` / `running (deploying)` → actively working.
- No change in `progress_json` for a long stretch while *not* queued → genuinely stuck;
  check the backend log.

Do not raise client timeouts to "fix" this — with N concurrent callers the tail wait
scales with N. Poll the task instead. See `docs/PLAN_async_implant_deploy.md`.

## SMB `put_file` NETBIOS timeouts (Windows file delivery)

Symptom: implant deploy / bulk put-file to a Windows box logs
`"[-] Error writing file to share C$: The NETBIOS connection with the remote
host timed out"`, retries exhausted.

> **This section is for file-transfer failures only.** The same
> "NETBIOS connection … timed out" string also appears on plain
> *authentication* (`nxc smb <ip> -u 'DOMAIN\Administrator'` with no `-x`,
> failing at the auth line rather than while writing to `C$`). That is a
> different fault with a different fix — usually DC clock skew, not SMB
> transport. Tell them apart by trying local creds: if
> `-u '.\Administrator'` succeeds while the domain account times out, go to
> [Kerberos clock skew](#kerberos-clock-skew), not this section.

C3rberus now **auto-escalates the file push** SMB → WinRM → SSH on repeated
transient failures (`docs/PLAN_windows_delivery_fallback.md`):

- **WinRM** (port 5985) is tried when the box has a *password* credential
  (pass-the-hash over WinRM is not supported — nthash-only boxes skip it).
- **SSH** (port 22) is tried only when nmap SA shows 22 open (Windows OpenSSH).

Which tier actually delivered is on the task row —
`OperatorTask.progress_json[<ip>].exec_path` = `smb` | `winrm` | `ssh` (visible
in the Task Queue "view output"). If all three fail, the box's deploy fails
loud with every tier named.

Knobs: `NXC_ALT_DELIVERY_ENABLED` (default `true`; set `false` for SMB-only),
`NXC_WINRM_PORT` (5985), `NXC_WINRM_CHUNK_SIZE` (1500). For the WinRM tier to be
available, ensure 5985 is reachable (many boxes get it from
`promote_domain_controllers.py` Phase 1).

## pfSense sshguard blocking the C3rberus host

**Symptom**: NXC to a **pfSense/firewall** box suddenly fails for *everything*
(IOC checks, deploy/remediate, red_scoring auth, SA collection) — connections
time out or reset at the SSH layer — even though the box is up, the cred is
correct, and other boxes are fine. Often starts after a burst of rapid NXC SSH
connections or a batch of auth failures (a stale/rotated cred, or the
`MaxStartups` churn behind "Unknown SSH Version, skipping" errors).

**Cause**: pfSense runs **sshguard**, which watches sshd for auth failures /
connection abuse and, past a threshold, adds the source IP to the `sshguard`
**pf block table** — silently dropping all further SSH from that host. Because
C3rberus opens many short-lived NXC SSH connections to each pfSense, the
**C3rberus host's own IP** can end up in that table, blocking the whole NXC
pipeline to that firewall until the entry ages out.

**This is not a cred problem** — the credential is valid; the packets are being
dropped by pf before sshd ever authenticates. Don't rotate creds or re-run
scoring-preflight; that won't help.

**Diagnose** (from the pfSense console/webGUI shell, or any host that can still
reach it): is the C3rberus IP blocked?

```sh
pfctl -t sshguard -T show          # list currently-blocked IPs
```

**Clear** the block immediately:

```sh
pfctl -t sshguard -T delete <c3rberus_host_ip>   # unblock one IP
# or flush the whole table:
pfctl -t sshguard -T flush
```

**Prevent** (competition setup): whitelist the C3rberus management IP so
sshguard never blocks it — add it to sshguard's allow list on each pfSense
during provisioning (fold into the infra deploy script).

**SA visibility**: the Targets page **sshguard** SA tab (see
[c2-targets.md § SA-tabs](c2-targets.md#sa-tabs)) surfaces `sshguard`'s
running state and its current pf block-table entries every SA cycle. A
`data-testid="sshguard-host-blocked-badge"` appears when the C3rberus host's
own IP is in the table — determined by comparing the table against
`config.C2_IP`, the same value implants use for their call-home address
(`CLAUDE.md` § Sliver/pivot). **Caveat**: on a multi-homed C3rberus host,
`C2_IP` can be auto-derived from the wrong interface (the same footgun
`resolve_c2_ip()` already warns about at startup) — if the badge doesn't
appear but you suspect a block, don't trust the badge alone; check the raw
blocked-IP list on the tab against `pfctl -t sshguard -T show` directly. A
`sshguard_block` alert (severity `warn`) fires once when the C3rberus host
transitions into the blocked state, not on every SA cycle while still
blocked.

### SA access-loss early-warning alerts

Beyond `sshguard_block`, SA collection watches five more ways a blue team can
cut off C2/scoring access and emits a `warn` alert on the *transition into*
the bad state (once, not per cycle — recovery clears the tracker silently).
These fire ahead of the coarse `box_unreachable` (3 consecutive NXC failures)
and `session_lost` signals, so treat them as a head start, not a confirmed
outage. All five are Linux/pfSense (Windows firewall blocking is not tracked).

- **`fail2ban_block`** — fail2ban on a Linux box has banned the C3rberus host
  IP (`config.C2_IP`). Our next SSH/NXC login will be dropped. Check
  `fail2ban-client status sshd` on the box; unban with
  `fail2ban-client set sshd unbanip <C2_IP>`.
- **`firewall_c2_block`** — a local firewall rule (Linux iptables/nftables, or
  pfSense pf) drops/rejects one of our C2 ports (MTLS call-home, script-fetch,
  or a live pivot listener) or our C2 IP. `details.blocked_ports` /
  `details.rule_snippet` name the offending rule. On pfSense this can sever the
  whole team's pivot. The monitored port set is derived at runtime (config
  listeners + active `PivotSessions`), never hardcoded.
- **`selinux_enforcing`** — SELinux flipped from permissive/disabled to
  enforcing on a Linux box. The implant still runs but policy may deny its
  outbound connections; watch for a following `session_lost`.
- **`sshd_config_restricted`** — a change to `/etc/ssh/sshd_config` that locks
  our access: `PermitRootLogin no`/`prohibit-password`,
  `PasswordAuthentication no`, or an `AllowUsers` that omits the scoring user
  (`config.RED_SCORING_USERNAME`). `details.reasons` lists which. Note the
  restriction only takes effect after sshd reloads.
- **`auth_keys_removed`** — the SSH `authorized_keys` fingerprint set on a
  Linux box shrank (a persistence key was removed). `details.removed_count`
  gives how many. Key *additions* never alert.

## Sliver not connecting

**Symptoms**: implants generate fine, deploy completes, but no session /
beacon ever appears on the Targets page.

**Checklist**:

1. **Listener present** — `/admin/status` → `components.sliver` should show
   the mTLS listener bound to `MTLS_LISTENER_PORT` (default `8888`). If
   not, the backend should create one on startup; check the log for
   `Created Sliver mTLS listener on port ...`.
2. **C2 URL match** — `SCRIPT_SERVER_URL` and the Sliver implant's
   `c2_url` must point at a host the target can actually reach. Most
   common bug: targets behind a per-team firewall can only reach the
   C3rberus host through the pivot listener — generate the implant with
   the correct pivot port.
3. **Implant binary mismatch** — implants compiled against a Sliver
   server config different from the one loaded at runtime won't connect.
   Regenerate the implant.
4. **Network path** — `ping`/`nmap` the target from the C3rberus host;
   the nmap health loop also surfaces this as `box_unreachable` alerts.

## SA collection errors

**Symptoms**: SA tab on the Targets page shows stale data; per-box
"Run SA Now" returns "no data".

**Checks**:

- `logs/fastapi_backend_<ts>.log` — search for the box IP; SA collection
  errors are logged at WARN.
- `SA_MAX_CONCURRENT_BOXES` capped too low for the box count — SA cycles
  can starve.
- Combined-SA script size — SA combined script body is small (one-liners),
  so size limit rarely bites, but check anyway.

## Kerberos clock skew

**Symptoms**: golden ticket / DCSync / ccache tests fail with Kerberos
errors; `clock_skew` alert (warn severity) fires in the Alert panel;
`impacket-ticketer` or `nxc -k` returns "Clock skew too great".

> **Skew does not always say "clock".** The common presentation is
> **`Connection Error: The NETBIOS connection with the remote host timed out`**
> on *domain* auth, with no mention of time anywhere. Everything looks like a
> network or SMB fault, and it is not. Before chasing NETBIOS, secure channels,
> or SMB tuning, run `fix_dc_clocks.py --check` — it is ~10 seconds and
> immediately rules skew in or out.
>
> Worked example (2026-08-07), which cost several hours of misdirected work:
>
> | Check | Result | What it *seemed* to mean |
> |---|---|---|
> | `nxc smb <member> -u 'DOMAIN\Administrator'` | NETBIOS timeout | dead/filtered box |
> | `nxc smb <member> -u '.\Administrator'` | `Pwn3d!` | box is fine — so it's the domain |
> | `nltest /sc_query` | `ERROR_NO_LOGON_SERVERS`, empty Trusted DC Name | broken secure channel |
> | `Test-ComputerSecureChannel -Repair` | "The server is not operational" | LDAP down |
> | DC port scan | only 445 open; 53/88/389/636 closed | **AD DS is down on the DC** |
> | `fix_dc_clocks.py --check` | **DC at +10798s, all 9 others within ±3s** | the actual cause |
>
> The member box was never at fault: it was correctly configured and simply had
> no functioning logon server. Note the ordering trap — a snapshot revert
> restored the DC's *services* but reintroduced a ~3-hour skew, so AD came back
> and domain auth still failed. **Fix the clock after any DC revert, before
> concluding the revert failed.** See also
> [Stuck or unresponsive VM](#stuck-or-unresponsive-vm).
>
> The `+10799s` (~3 h) value is a recurring lab signature, not a coincidence —
> if you see roughly three hours, it is this.
>
> Second worked example (2026-08-10, team 6) — **same port signature, different
> cause**, which is why the triage no longer names one:
>
> | Check | Result | What it means |
> |---|---|---|
> | DC port scan | only 445 open; 53/88/389/636 closed | *seems* like AD DS down — it is not |
> | `Get-Service NTDS,DNS,kdc,Netlogon,ADWS` | all `Running` | AD services are alive |
> | `nltest /dsgetdc:TEAM6.C3` | DC returned, `PDC GC DS LDAP KDC` flags | AD is fully functional locally |
> | `netstat -ano` | `lsass` bound `0.0.0.0:88/389/636` | listeners are up |
> | `Get-NetFirewallRule` (AD group) | `Allow/Inbound/Any`, byte-identical to a healthy DC | rules are not the cause |
> | probe from a second box on the subnet | same failure | not specific to the scanning host |
> | Proxmox hard reset | no change | not transient state |
> | **snapshot revert** | **all four ports open immediately** | persistent on-disk state, post-snapshot |
>
> The lesson is the ordering one again, in the other direction: the port scan is
> a *symptom*, and confirming with `Get-Service` + `nltest` over the still-open
> 445 costs ten seconds. After the revert, both documented follow-on breaks
> appeared and each needed its own fix — DC clock `+10800s` (`fix_dc_clocks.py`)
> and the member's secure channel returning `False`
> (`Test-ComputerSecureChannel -Repair`, no rejoin needed). Neither announces
> itself as what it is; both present as `NETBIOS connection timed out`.

**Cause**: Windows Time service on DCs drifts by hours in the lab
environment. Kerberos rejects tickets when client and DC clocks differ
by more than 270 seconds (5 minutes, standard Kerberos tolerance).

**Pre-test fix** (run before live test runs or before kick-off):

```bash
python scripts/infra/fix_dc_clocks.py
# Probes every DC via NXC + reads target NTP offset.
# Sets drifting DCs (> 270s) to correct UTC via `nxc Set-Date`.
# Box-timezone-aware; exits 0 if all DCs within tolerance.
```

Wired automatically into `scripts/testing/run_tests.sh` when `LIVE_MODE=1`;
best-effort (non-fatal if a DC is unreachable).

**Periodic alert**: the SA collection loop checks DC clocks each cycle
and emits a `clock_skew` alert (warn) when any DC exceeds 270 s. The
alert deduplicates per-DC over a 30-minute window. Filter for it in the
Alert History panel (`kind = clock_skew`). The alert is informational —
it does **not** auto-fix; run `fix_dc_clocks.py` manually.

## Kerberos realm case-sensitivity (ticket validation)

**Symptom**: `POST /c2/tickets/{id}/validate` reports the Windows/DC
branches valid but every domain-joined Linux box comes back
`valid=false` with an SSH detail like `no valid credentials` or a
`getST`-fallback detail that mentions `KRB_AP_ERR_MODIFIED` or a realm
mismatch.

**Cause**: the Windows KDC is case-insensitive, but MIT krb5 (the SSH
client's GSSAPI) and the box's SSSD keytab compare realm case
**sensitively**. If the forged ticket, `krb5.conf`, login principal, or
`getST` SPN mix cases (`corp.sus` vs `CORP.SUS`), the KDC ticket still
works on Windows but Linux GSSAPI drops it.

**How C3rberus handles it**: `netexec/wrapper.py::_synth_krb5_conf` /
`ssh_kerberos_check` and `cred_harvester.validate_ticket_against_box`
canonicalize every realm reference to **uppercase**
(`realm_u = realm.upper()`) and forge a fresh per-user golden ticket
with that same uppercase realm before the SSH attempt — so the four
places (forged ticket, `KRB5_CONFIG`, `login_user`, `host/<fqdn>` SPN)
agree.

**When you'll see it anyway**: inventory `domain:` typed in a mixed
case that a downstream helper reads verbatim before the canonicalization
runs, or a hand-written `krb5.conf` overriding `KRB5_CONFIG`. Fix: put
the realm in the inventory in whatever case you like (it is upcased on
use), and don't set `KRB5_CONFIG` in the environment before running the
backend — it will override the per-realm one C3rberus writes.

See `docs/PLAN_ticket_multibox_validation.md` §6.3 for the full
mechanism.

## `RANGE NOT CLEAN — aborting run_tests`

`scripts/testing/run_tests.sh` gates live runs on
`check_cpu_usage.py --fail-on-unreachable --settle-attempts 3 --settle-delay 45`.
Any failure prints:

```
RANGE NOT CLEAN — aborting run_tests.
Box(es) above the CPU threshold or unreachable after reboot +
remediation (see above). Likely cause: an IOC with PERSISTENCE that
survived remediation and re-launched its implant, or a box that won't return.
```

**Read the sweep output above the banner, not the banner.** It covers two
unrelated conditions and names only the first as "likely", so it points at a
stuck implant even when nothing is above the CPU threshold:

| Sweep line | Real condition | Where to go |
|---|---|---|
| `[!] <box> NN.N%  <-- above 20%` | a surviving implant is burning CPU | hunt the implant / persistence |
| `all clear — nothing at or above 20%` **plus** `N unreachable/auth-failed` | **nothing is hot** — a box failed to authenticate | [NXC auth failures](#nxc-auth-failures), then [Kerberos clock skew](#kerberos-clock-skew) |

A run aborting with `all clear — nothing at or above 20%` immediately above the
banner is the second case every time. Windows boxes are probed with
`domain_username`, so one broken DC fails **every** member of that team and
presents as "auth failed" on a box that is entirely healthy locally.

**Ordering (fixed 2026-08-08):** the DC clock guard now runs **before** this
gate. It used to run after, which meant a skewed DC broke domain auth, tripped
the gate, and the run exited *before* the clock fix that would have repaired it
ever executed — the condition could not self-heal across re-runs (three
consecutive runs died on it on 2026-08-07). The gate depends on working domain
auth, so its precondition runs first. If you are driving the gate by hand rather
than through `run_tests.sh`, do the same:

```bash
python scripts/infra/fix_dc_clocks.py --check   # ~10s, rules skew in or out
python scripts/infra/fix_dc_clocks.py           # correct any drifting DC
```

**`--triage`:** `run_tests.sh` passes it, and it is worth passing by hand too.
On any failure it maps each auth-failed box to its team's DC, probes that DC's
AD ports, and names the likely cause — one report per DC rather than one per
affected member:

```
Triage — auth failures
  team 10 DC c3-win25-10-1 (192.168.1.246): 445 open, CLOSED: 53 DNS, 88 Kerberos, 389 LDAP, 636 LDAPS
    => Team 10 cannot authenticate against this DC. The member boxes are probably fine.
    => CONFIRM the cause before acting — 445 still answers, so ask the DC directly:
       nxc smb 192.168.1.246 -u '.\Administrator' -p '<pw>' -X 'Get-Service NTDS,DNS,kdc,Netlogon | ...'
```

**This port shape has two causes, and the scan cannot tell them apart** — which
is why the triage names the symptom and hands you the command instead of naming
a cause. Run the `Get-Service` + `nltest /dsgetdc:<DOMAIN>` check it prints:

| DC answers | Meaning | Fix |
|---|---|---|
| services `Stopped`, `nltest` fails | AD DS really is down | reset the DC |
| services `Running`, `nltest` returns the DC | **AD is UP; the ports are being filtered** | reset will NOT help — snapshot revert |

The second case is real and was mistaken for the first on **2026-08-10** (team 6).
Every AD service was `Running`, `nltest /dsgetdc:TEAM6.C3` returned the DC with
`PDC GC DS LDAP KDC` flags, and `lsass` was bound to 88/389/636 — yet nothing
outside the box could reach those ports, from two different source hosts. What
makes it deceptive is that every firewall check comes back clean: rules
`Enabled=True, Profile=Any, Action=Allow` and **byte-identical to a healthy DC**,
zero block rules, stock `DefaultInboundAction`. A Proxmox hard reset did not
clear it; only a snapshot revert did, which means the state is persistent on
disk. If you meet this again, `Get-NetIPsecRule` and `netsh wfp show filters` are
the two layers below `Get-NetFirewallRule` that were never checked.

If the DC's ports are all open, the triage lists boot churn and clock skew as
candidates rather than asserting either.

**Auto-remediation caveat:** the gate attempts a Proxmox reset of flagged
boxes. Verify from the output that it actually reset the VM you expect — a
reset reported against a different vmid/node than
`reboot_and_wait_vms.py --ips <ip>` resolves for the same box, or one returning
`HTTP 500: got no worker upid`, means the box was never reset and the gate's
"after reboot + remediation" wording is not true for that run.

## Pulling box logs after a failure

The backend log only shows whether **our** NXC call succeeded or timed out.
It cannot tell a genuinely crashed box from one that was merely
connection-throttled by concurrent traffic (sshd `MaxStartups`, SMB
negotiation queue saturation under overlapping test workers). For that you
need the box's own view:

```bash
python scripts/diagnostic/pull_box_logs.py --ip 192.168.1.212 \
    --at "2026-07-25 13:43:13" [--before-sec 60] [--after-sec 240]
```

`--ip` must be in inventory; `--at` is the failure timestamp. The window
defaults to 60 s before and 240 s after. OS is auto-detected from
inventory and dispatched accordingly:

| OS | Source |
|---|---|
| Linux | `journalctl --since ... --until ...` over SSH |
| Windows | `Get-WinEvent` — System log + SMBServer/Operational, over SMB |
| pfSense / FreeBSD | tail of `/var/log/system.log` + `/var/log/auth.log` (no journald, so no exact time-range filter — eyeball against the printed window) |

Known throttle/crash signatures (`MaxStartups`, OOM-kill, `segfault`,
`panic`, smbd crash/restart) are flagged in the output.

**Read-only, and strictly post-hoc.** It opens exactly one SSH/SMB
connection per invocation. Never loop it during a live test — that adds to
the very connection load being diagnosed.

**Firewall boxes are dialed at `wan_ip`.** If the target has a `wan_ip` in
inventory (a pfSense whose LAN mgmt IP is unreachable from the host), this tool
connects to the WAN address automatically — pass its LAN `--ip` from inventory as
usual (`docs/PLAN_firewall_wan_dial_standalone.md`).

## Stuck or unresponsive VM

An in-guest reboot can silently no-op — a `shutdown /r` fired over NXC that
never cycles the VM (observed on the Windows workstation boxes, which
reached 500+ hours of uptime while "rebooting"). When a box is wedged, hard
power-cycle it through the Proxmox API instead:

```bash
python scripts/testing/reboot_and_wait_vms.py            # all inventory VMs
python scripts/testing/reboot_and_wait_vms.py --ips 192.168.1.212
python scripts/testing/reboot_and_wait_vms.py --dry-run  # print, don't execute
```

The Proxmox reset matches VMs by **name** and `--ips` filters on the LAN `ip`
(both identity), but the post-reset NXC health-gate **dials a firewall at its
`wan_ip`** — so a pfSense whose LAN mgmt IP is unreachable still passes the gate
once it is back up (`docs/PLAN_firewall_wan_dial_standalone.md`).

It issues one Proxmox `status/reset` per VM (matched by inventory `name`;
a stopped VM is `start`ed instead), then health-gates on NXC reachability —
`--retries` rounds spaced `--delay` seconds apart after a `--settle` delay.
Stragglers past the budget produce a warning, not a failure.
`run_tests.sh` fires this before pytest in live mode; opt out with
`--no-reboot-vms`.

Auth: `$PROXMOX_API_TOKEN`, else parsed from `docs/PROXMOX_NOTES.md`
(host via `$PROXMOX_HOST`, default `pve1.voi`). That notes file also holds
the raw curl recipes for node lookup / reboot / reset if you need them by
hand. **It is gitignored because it contains a live API token — never
commit it.**

**A reset does not fix a broken service role.** A hard reset clears a wedged
guest, but a box that comes back with a *role* still dead (e.g. a DC answering
445 while 53/88/389/636 stay closed) has lost more than runtime state — resetting
it again will not help. Confirm with a port check against a healthy peer of the
same role before repeating the reset:

```bash
for p in 53 88 389 445 636 3268; do
  timeout 4 bash -c "echo > /dev/tcp/<suspect-dc>/$p" 2>/dev/null \
    && echo "  $p OPEN" || echo "  $p CLOSED"
done
```

At that point a snapshot revert is the next step — **and it requires explicit
operator permission** (see `docs/PROXMOX_NOTES.md`; a rollback is a last resort
for a VM that is unresponsive even after a reset, never a routine action).

**After reverting a DC, fix its clock before judging the result.** A revert
restores services but commonly reintroduces a multi-hour skew, and the skew
breaks domain auth in a way that looks nothing like a time problem — so a
successful revert can present as a failed one. Sequence:

```bash
# 1. revert (with permission)  2. wait for AD ports  3. THEN:
python scripts/infra/fix_dc_clocks.py --check
python scripts/infra/fix_dc_clocks.py
```

See [Kerberos clock skew](#kerberos-clock-skew) for the full worked example.

## Deploying the Windows hiding library restarts ADWS on domain controllers

`windows_hide_library`'s deploy script issues `Restart-Service ADWS` when that
service is present and running. It is a no-op on non-DCs.

**Why:** `Get-ADUser` does not issue LDAP from `powershell.exe` — it connects to
ADWS on tcp/9389, and ADWS makes the `wldap32` call inside its own long-running
process. `AppInit_DLLs` injects only at process *start*, so an ADWS that was
already running when the IOC deployed never picks up the hook and `Get-ADUser`
keeps listing the hidden user until the next reboot. Restarting it makes that
surface deterministic.

**What an operator sees:** a brief Active Directory Web Services blip at deploy
time. `Get-ADUser` fails for a second or two, then works normally (filtered).
ADUC and `[adsisearcher]` are unaffected — they use LDAP in-process and are
covered without any restart.

If the restart fails, deploy still reports success: every other surface works,
and only the `Get-ADUser` path is left unfiltered.

---

## Recovering a box where the hiding library degraded the host

The Windows hiding library loads into essentially every process, and its
`CreateProcessNotify` export runs on every process launch. If a box starts
behaving badly after deploy — process launches failing, PowerShell/CLR errors,
`0xC0000409` (`STATUS_STACK_BUFFER_OVERRUN`) in the Application log — remove the
load mechanisms first.

**In-band (host still usable).** Set the bypass variable, then run the IOC's
remediate script:

```powershell
$env:_SYS_AUDIT_TOKEN = 'a3f2e891-7c4b-4d9a-b5e6-1f2a3c4d5e6f'
```

The library's self-protection hooks (`NtDeleteFile`, `NtDeleteValueKey`,
`NtDeleteKey`) refuse to let their own artifacts be deleted without it.

**Offline (in-band execution is gone).** Mount the disk from another host and:

1. Delete the DLL from its `C3R_DROP_DIR` (pool-driven; the deployed path is on
   the `DeployedIOCs.deploy_params` row).
2. Load the offline `SYSTEM` hive and clear
   `SYSTEM\CurrentControlSet\Control\Session Manager\AppCertDLLs`.
3. Load the offline `SOFTWARE` hive and clear
   `Microsoft\Windows NT\CurrentVersion\Windows\AppInit_DLLs`
   (and set `LoadAppInit_DLLs` to 0).

Clearing the registry values alone is enough to stop new processes loading it —
the file can be removed afterwards. A DLL still mapped into running processes
cannot be deleted until they exit; schedule it via
`PendingFileRenameOperations` or delete after a reboot.

**Note on which tools to trust while diagnosing.** With the library active,
`Get-Process`, `Get-ChildItem`, `dir`, `Get-Service`, `net user`, and
`netstat -ano` are all filtered. `netstat -anop UDP`, `Get-NetTCPConnection`
(WMI-serviced), and Sysmon/ETW telemetry are **not** — see
[IOC Catalog](ioc-catalog.md) for the full covered/not-covered table.

---

## Recovering a box where the BSD hiding library degraded the host

The pfSense/FreeBSD hiding library is loaded via `/etc/ld.so.preload` and runs in every
dynamically linked process. If a box starts behaving badly after deploy — process
launches failing, SSH sessions crashing, permission errors where none existed — remove
the library.

**In-band (host still usable via SSH or console):**

```sh
mount -uw /                                      # re-mount rw if needed
# Remove from preload:
grep -v 'hide_library_bsd' /etc/ld.so.preload > /tmp/preload.tmp && mv /tmp/preload.tmp /etc/ld.so.preload
# Restore netstat's setgid bit (deploy stripped it):
chmod g+s /usr/bin/netstat
# Remove the library file (path is in DeployedIOCs.deploy_params C3R_DROP_DIR):
rm -f /usr/local/lib/hide_library_bsd.so   # or wherever C3R_DROP_DIR pointed
# Remove config extattr:
rmextattr -f net_opts /etc/hostid
mount -ur /
```

Alternatively, run the IOC's remediate script — it performs all of the above steps.

**Offline (in-band execution is gone).** Mount the UFS/ZFS filesystem from another host:

1. Remove the library path from `/etc/ld.so.preload`.
2. Delete the `.so` file from its pool directory (recorded in `DeployedIOCs.deploy_params`).
3. The `net_opts` extattr on `/etc/hostid` is self-contained — remove it with `rmextattr` or ignore it (it only matters when the library is loaded).
4. After re-mount on the original host, run `chmod g+s /usr/bin/netstat` to restore the setgid bit.

---

## Self-inject IOC deploys but no beacon ever calls back

Symptom: `linux_self_inject` / `firewall_self_inject` (and the `_udev` / `_devd`
persistence variants) report `status: 1` from the deploy, then the harness fails ~180s
later with `No new session/beacon appeared from <ip> within 180s`.

The deploy's success only means the ptrace calls succeeded. The payload runs *after*
detach, so a fault there used to look like a clean deploy. Two known causes:

1. **The payload crashed the target.** Check the target box:
   ```bash
   journalctl -k --since "-1h" | grep -iE "segfault|general protection"   # Linux
   grep -iE "syslogd.*(signal|core|fault)" /var/log/messages              # FreeBSD
   ```
   A `general protection fault` whose `sp` is not a multiple of 16 is the
   unaligned-stack signature: Sliver's Go payload uses SSE (`movaps`), which `#GP`s on
   a misaligned stack. Fixed in both shims; if it reappears, the alignment step before
   the register handoff has regressed — `tests/test_self_inject_alignment.py` guards it.

2. **The wrong process was injected.** On FreeBSD/pfSense, several processes share the
   `syslogd` comm name:
   ```
   /usr/sbin/syslogd -s                 <- the real daemon (owns udp4 *:514)
   syslogd: syslogd.casper (syslogd)    <- Capsicum helper
   syslogd: system.net (syslogd)        <- Capsicum helper
   ```
   A helper runs in capability mode, so an injected payload can never open a socket —
   no callback, no crash, nothing logged. Confirm with
   `ps -o pid,command -p "$(pgrep syslogd | tr '\n' ,)"` and `sockstat -4 | grep syslogd`.

Since the fail-loud change, a target that dies post-injection reports
`injection ran but target process died (dropper rc=3)` instead of a silent success.
See `docs/PLAN_self_inject_target_selection.md`.

## Operator terminals keep dropping or erroring

The three operator terminals (OS shell, Sliver shell, Sliver console) ride a
WebSocket. Symptoms and causes:

**Terminals drop every time you edit a file (dev):** the backend is running with
`--reload`. WatchFiles restarts the worker on any repo edit, killing every open
socket. Start it without reload — reload is now off by default; see
[Configuration § Dev server reload flag](configuration#dev-server-reload-flag).
Just run `bash scripts/run.sh` (no `C3R_RELOAD`, no `--reload`).

**Terminal shows red `ERROR:` on every command but stays "connected":** the
underlying Sliver session died under the socket. The backend now detects this,
sends a `session_dead` frame, and closes with code **4010** → the terminal shows
"Session ended" and does **not** auto-reconnect (reconnecting to a gone session
is pointless). Open a fresh session.

**A dropped terminal now auto-reconnects** (bounded backoff, up to 6 attempts)
and shows a `[RECONNECTING…]` overlay; a client heartbeat ping keeps NAT/proxy
paths warm. If it exhausts attempts it parks on a manual `RECONNECT` button.

**OS-shell terminal drops with `[session ended]` after ~2 minutes of no typing:**
the PTY tunnel died at Sliver's ~2 min idle timer (see the
`project_sliver_pivot_2min_tunnel_death` note in Claude's memory).
`PtyHub` (`fastapi_backend/c2/pty_hub.py`) sends an empty `TunnelData` heartbeat
every 60 s while any client is attached to hold the timer off; symptoms usually
mean that keepalive is broken or the tunnel died for another reason. Verify:

1. `grep 'PtyHub' logs/uvicorn.log` around the drop time — the shutdown line
   names the tunnel id.
2. `sliver-client` → `tunnels` — the tunnel should be gone if the drop was
   clean, still listed (and orphaned) if the reader task raised.
3. Look for `session_dead` frames in browser DevTools → Network → WS → Messages
   — the `error` field carries the reason string.

See `docs/PLAN_pty_session_shell.md` for the full protocol + Non-goals (runtime
resize is not supported — `ShellReq` on this sliver-py version has no size field).

**Diagnosing which cause:** the backend now logs every socket's lifecycle —
`grep 'ws OPEN\|ws CLOSE' logs/uvicorn.log` shows endpoint, session, and the
close code. Cross-reference the browser DevTools → Network → WS close code:

| Close code | Meaning | Auto-reconnect? |
|---|---|---|
| `1006` / `1011` / `1012` | network / server going-away / restart (incl. `--reload`) | yes |
| `1000` / `1005` | normal / clean client close (e.g. user typed `exit`) | no |
| `4003` | auth / blue-team / bad token | no |
| `4009` | session in use by another operator | no |
| `4010` | Sliver session dead / gone | no |

## Wedged box: glibc / NSS / PAM / AppCertDlls corruption

**Symptom.** `phase8_summary.regressions[]` in the newest
`tests/perf/results/soak_<ts>.json` is non-empty, or
`tests/test_e2e_box_health.py::test_box_health_deploy_cycle` fails with
"N box-health regression(s):" and one or more `cmd_key` entries per box.
On a live box, basic commands abort before their first line of output —
e.g. `ls` on Ubuntu 20.04 emitting
`ls: /lib/x86_64-linux-gnu/libc.so.6: version 'GLIBC_2.34' not found
(required by /usr/lib/libnss_sss.so)`.

**Diagnostic path.**

1. Inspect the regression's `post_raw` (or the `raw` capture in the pytest
   report). The stderr line tells you the class:
   - `GLIBC_[0-9.]+ not found` — a `.so` on the box links against a
     newer glibc than what's on disk. Most likely a cross-distro apt
     pull (e.g. sssd/libnss-sss from a jammy backport ended up on 20.04).
   - `Cannot open shared object file` — a dangling entry in
     `/etc/ld.so.preload` points at a `.so` a remediate deleted.
   - `The specified module could not be loaded` (Windows) — AppCertDlls
     residue pointing at a deleted DLL. See "Recovering a box where the
     hiding library degraded the host" above.
   - `could not be loaded | corrupt` (Windows, `ps_dotnet` probe) —
     CLR loader corruption. Usually the same AppCertDlls class.
2. Revert the box via `scripts/testing/reboot_and_wait_vms.py --mode
   rollback --box <name>` (see [Stuck or unresponsive VM](#stuck-or-unresponsive-vm)).
3. Re-run the bisect:
   `scripts/diagnostic/repro_glibc_break.sh --box <name> --admin-pass '<pw>'`.
   The script solo-deploys every Linux IOC on that one box, probes with
   `ls / && getent passwd root && id root` after each, and stops at the
   first IOC that leaves the box broken. Output lands in
   `scratchpad/glibc_repro_perIOC.log` +
   `scratchpad/glibc_repro_summary.md`.
4. Do NOT run the bisect while any pytest / soak session is in flight —
   it mutates live-box state via the operator backend. The script
   refuses to run when `pgrep` finds one active.

If the bisect returns clean on solo IOCs and clean on full deploy-all, the
break is environmental — most often `scripts/infra/join_linux_domain.py`
pulling `libnss-sss` from a wrong-distro apt source. Re-run with
`--sources-only` to check `/etc/apt/sources.list.d/` on the box.

## Component restart procedure

`scripts/run.sh` kills any prior backend/frontend on `:3000` / `:5173`
and restarts both. State that survives:

- DB (`db/database.db`) — competition history, IOC results, scores.
- Sliver server state under `~/.sliver/` — implants, sessions if Sliver
  itself stayed up.
- **Competition status.** If the competition was `RUNNING` or `PAUSED`
  when the backend died/restarted, it comes back in the same state
  automatically — `CheckScheduler`, the playbook scheduler, and IOC
  scoring all resume as if the backend never went down. A
  `competition_resumed_after_restart` alert (info severity) fires on
  every such auto-resume; check the Alert History panel or
  `GET /admin/status` → `components.competition_restart` to confirm a
  restart happened and see what it resolved to (`resumed_as`). If the
  competition's `scheduled_end_time` already elapsed while the backend
  was down, it comes back `STOPPED` instead of `RUNNING`/`PAUSED` —
  `components.competition_restart.reason` reads
  `"scheduled_end_passed"` in that case. `PREFLIGHT_ON`/`PREFLIGHT_ARMED`
  restore separately via the existing `armed_start_time` mechanism
  (unchanged by this). See `docs/PLAN_competition_status_restart.md`.

State that does **not** survive a backend restart:

- In-memory NXC failure tracker rolling window.
- `WrapperCache` (plaintext script bodies in memory).
- Live SSE subscribers; clients will reconnect.

For Sliver only: `scripts/reset_sliver.sh` (destructive, kills sessions).
For DB-only: `scripts/reset_db.sh`.

<!-- audited 2026-06-13 — added Kerberos clock-skew section (62a4094). env vars verified against core/config.py (NXC_DEGRADED_THRESHOLD, NXC_DEGRADED_WINDOW_MINUTES, MTLS_LISTENER_PORT, SCRIPT_SERVER_URL, NXC_BATCH_IOC_SCRIPT_SIZE_LIMIT, SA_MAX_CONCURRENT_BOXES). Log filename `fastapi_backend_<ts>.log` matches logs/ directory. Updated 2026-06-26 — added scoring-vs-operator-cred note (red_scoring is_infrastructure, nxc_routing scoring_only). Updated 2026-07-21 — competition status (RUNNING/PAUSED) now survives a backend restart; see docs/PLAN_competition_status_restart.md. Updated 2026-07-30 — added post-hoc box-log pull (scripts/diagnostic/pull_box_logs.py) + stuck-VM Proxmox reset (scripts/testing/reboot_and_wait_vms.py) sections, moved out of CLAUDE.md § Pre-Competition Setup Scripts; flags verified against each script's argparse. Updated 2026-08-02 — added BSD hiding-library offline recovery section (setgid strip + extattr cleanup). -->

