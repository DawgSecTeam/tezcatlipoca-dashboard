# IOC Catalog

Complete reference for all IOC definitions in the scoring system.

All IOCs are **training stimuli** deployed to owned, isolated CDE competition VMs.
Blue teams are expected to detect and remediate them — scoring reflects remediation.

---

## How to read this table

| Column | Meaning |
|---|---|
| **Name** | `IOC.name` — key in scoring engine and API |
| **OS** | `windows` \| `linux` \| `firewall` (pfSense) |
| **Diff** | Difficulty 1=Easy / 2=Medium / 3=Hard / 4=Ultra |
| **Pts** | Base points (Easy=5, Medium=10, Hard=20, Ultra=40) |
| **Check** | `SliverCheck` (active session) or `ScriptCheck` (artifact present) |
| **Category** | `remote_access` \| `persistence` \| `users` \| `exfiltration` \| `impair_defenses` |
| **Backup** | ✓ = `backup_copy=True`; watchdog restores from immutable BACKUP_DIR when primary deleted |
| **Deploy All** | ✓ = included in full-catalog Deploy All; — = opt-in only (Generate page / named deploy) |

---

## Resilient Implant IOCs (Plan 12 — added 2026-07-24)

These IOCs survive the naive `kill $pid && rm $binary` pattern. All have `deploy_all_default=False`
and are triggered from the **Generate page** (select storage mode) rather than the standard Deploy All.

### Family 1 — In-Memory Injection (SliverCheck)

The dropper writes shellcode into a host process, then self-deletes. No on-disk binary remains.
Active Sliver session = IOC present.

| Name | OS | Diff | Pts | Category | Description |
|---|---|---|---|---|---|
| `linux_self_inject` | linux | 3 | 20 | remote_access | Dropper ptrace-injects Sliver shellcode into `cron`, self-deletes |
| `linux_self_inject_udev` | linux | 4 | 40 | persistence | Inject dropper + udev rule re-injects via helper reading shellcode from xattr on /etc/hosts |
| `firewall_self_inject` | firewall | 4 | 40 | remote_access | Dropper ptrace-injects Sliver shellcode into syslogd on pfSense, self-deletes (session-type) |
| `firewall_self_inject_devd` | firewall | 4 | 40 | persistence | Inject dropper + devd rule re-injects via helper reading shellcode from extattr on /etc/hosts |
| `windows_self_inject` | windows | 3 | 20 | remote_access | Dropper injects Sliver beacon shellcode into svchost via CreateRemoteThread, self-deletes |
| `windows_self_inject_wmi` | windows | 4 | 40 | persistence | Inject dropper + WMI InstanceDeletionEvent re-injects via shellcode in ADS on hosts file |

**Three hunt layers for combined IOCs:** evict injected thread + remove persistence mechanism + delete shellcode storage.

**Injection-target selection (operator note).** The Linux dropper targets `cron`, not
`systemd-journald`: journald, `systemd-logind` and `polkit` all set
`MemoryDenyWriteExecute=yes`, whose seccomp filter fails the W+X mmap with `EACCES` and
makes remote injection impossible. Verify with
`systemctl show <unit> -p MemoryDenyWriteExecute` before changing the target. On
FreeBSD/pfSense the dropper picks the `syslogd` process that **owns an INET socket** —
FreeBSD 15 runs Capsicum "Casper" helpers that share the `syslogd` comm name, and a
payload injected into a helper is stuck in capability mode and can never call home. See
`docs/PLAN_self_inject_target_selection.md`.

### Family 2 — Fileless Shellcode Storage (ScriptCheck)

Shellcode embedded in a non-executable storage location. ScriptCheck scores whether the storage
artifact is present, regardless of whether a session is active.

| Name | OS | Diff | Pts | Category | Description |
|---|---|---|---|---|---|
| `linux_elf_section_embed` | linux | 4 | 40 | persistence | Sliver shellcode in `.c3r_sc` ELF section of a legit-looking carrier binary |
| `linux_xattr_shellcode` | linux | 3 | 20 | persistence | Sliver shellcode in `user.c3r_sc` xattr on /etc/hosts; cron helper reads + memfd_create exec |
| `firewall_elf_section_embed` | firewall | 4 | 40 | persistence | Sliver shellcode in `.c3r_sc` ELF section of carrier binary (FreeBSD objcopy) |
| `firewall_extattr_shellcode` | firewall | 3 | 20 | persistence | Sliver shellcode in `system c3r_sc` extattr on /etc/hosts |
| `windows_pe_section_embed` | windows | 4 | 40 | persistence | Sliver shellcode in `.c3r_sc` PE section of carrier binary; PowerShell schtask loader |
| `windows_shellcode_registry` | windows | 3 | 20 | persistence | ~509-byte Sliver stager, base64 in an HKLM registry value; schtask loader reads + executes. Stager dials `C2_IP:STAGE_LISTENER_PORT` for its second stage |

**Check tool per OS:**
- Windows: `[System.IO.File]::ReadAllBytes` + `.c3r_sc` PE header check; `Get-ItemProperty` for registry
- Linux: `readelf -S <carrier> | grep .c3r_sc`; `getfattr -n user.c3r_sc /etc/hosts`
- pfSense: `objdump -h <carrier> | grep .c3r_sc`; `getextattr system c3r_sc /etc/hosts`

---

## Backup-Copy Pivot IOCs (Plan 13)

Three pivot-anchor IOCs have `backup_copy=True` — the deploy pipeline stages an immutable copy of
the binary to BACKUP_DIR (one tier more obscure than DROP_DIR). The watchdog persistence script
restores the primary from BACKUP_DIR when missing. Check scripts return `status=1` when primary
**OR** backup is present; `status=0` only when both are gone **and** the watchdog is removed.

| Name | OS | Diff | Pts | Check | Backup | Notes |
|---|---|---|---|---|---|---|
| `firewall_implant_devd_kqueue` | firewall | 3 | 20 | SliverCheck | ✓ | Pivot gateway — loss severs NXC pivot for the whole team |
| `linux_implant_udev` | linux | 3 | 20 | SliverCheck | ✓ | Pivot set member |
| `windows_implant_inject` | windows | 2 | 10 | SliverCheck | ✓ | Pivot set member |

---

## Effect Library IOCs (Plan 14 — added 2026-07-24)

Net-new scored IOCs from the effect-technique library. Pool-driven add-user IOCs deploy 20
usernames drawn at random from tiered naming pools (`config/naming_pools.yml`), stored in
`C3R_USER_NAMES`; check/remediate scripts loop over the full list.

| Name | OS | Diff | Pts | Category | deploy\_all\_default | Description |
|---|---|---|---|---|---|---|
| `linux_clean_firewall` | linux | 2 | 10 | impair\_defenses | ✓ | Flush iptables/nftables; disable ufw/firewalld via cron re-fire |
| `linux_disable_firewall` | linux | 1 | 5 | impair\_defenses | ✓ | Stop and disable ufw / firewalld services |
| `windows_rid_hijack` | windows | 3 | 20 | users | — | Copy Administrator SAM `F` value onto Guest; Guest enabled in Administrators |
| `linux_all_users_sudoer` | linux | 2 | 10 | users | ✓ | Drop `/etc/sudoers.d/99-admins` granting `NOPASSWD: ALL` |
| `firewall_all_users_sudoer` | firewall | 2 | 10 | users | ✓ | Same sudoers drop-in on pfSense (read-only FS wrapped with `mount -uw /`) |
| `linux_pam_any_password` | linux | 3 | 20 | persistence | — | Insert `pam_permit.so` into `common-auth` / `system-auth`; all PAM services accept any password |
| `firewall_pam_any_password` | firewall | 3 | 20 | persistence | — | Same `pam_permit.so` in pfSense `/etc/pam.d/sshd` and `login` |
| `linux_ssh_authorized_keys` | linux | 2 | 10 | persistence | ✓ | Append red-team public key to `/root/.ssh/authorized_keys` |
| `windows_ssh_authorized_keys` | windows | 2 | 10 | persistence | ✓ | Append red-team public key to `%ProgramData%\ssh\administrators_authorized_keys` |
| `firewall_ssh_authorized_keys` | firewall | 2 | 10 | persistence | ✓ | Append red-team public key to `/root/.ssh/authorized_keys` (pfSense) |
| `windows_adminsdholder` | windows | 4 | 40 | persistence | — | Add `EVERYONE:GenericAll` ACE to `AdminSDHolder`; SDProp propagates to all protected objects (DC-only) |

**`deploy_all_default=False` IOCs** (marked — above) are excluded from unattended
"Deploy All IOCs" runs. To deploy them, either name them explicitly in the
`ioc_names` list or use the Deployment page.

---

## Shared-Library (.so) Implant IOCs (added 2026-07-29)

Sliver `format=shared` implants for FreeBSD/Linux. The `.so` is **not** executable,
so a native C **loader** (`so_loader_{freebsd,linux}`) is deployed beside it and
launched as `<loader> <so> RunSliver` — the loader `dlopen()`s the `.so` and calls
the `RunSliver` export in a thread (FreeBSD has no `.init_array` constructor, so the
export must be called explicitly). All are `SliverCheck` (active Sliver session =
IOC present). Generated from the **Generate page** (`format=shared`).

| Name | OS | Diff | Pts | Category | Persistence | Description |
|---|---|---|---|---|---|---|
| `firewall_implant_so_bare` | firewall | 2 | 10 | remote_access | — | FreeBSD `.so` loaded by `so_loader_freebsd`; no persistence |
| `firewall_implant_so_rc` | firewall | 3 | 20 | remote_access | rc.d service | FreeBSD `.so` + rc.d service (`SoLoaderDaemonRC`) relaunches the loader on boot/crash |
| `linux_implant_so_bare` | linux | 2 | 10 | remote_access | — | Linux `.so` loaded by `so_loader_linux`; no persistence |
| `linux_implant_so_systemd` | linux | 3 | 20 | remote_access | systemd unit | Linux `.so` + systemd unit (`SoLoaderSystemd`, `Restart=always`) relaunches the loader |

**Hunt layers:** find the `so_loader` process (`ps`, `fstat`/`/proc/<pid>/maps`) → read its
argv for the `.so` path → remove loader + `.so`; for the persistence variants also remove the
rc.d script / systemd unit, else it relaunches. Both bare and persistence variants carry
`timestomp=True` + `backup_copy=True`.

---

## Hiding-Library IOCs (added 2026-07-24, hook coverage extended 2026-08-01)

A userland **API-hooking library** that filters the operator's own artifacts out of
enumeration results, so a blue teamer running the ordinary tools sees a clean box.
All three are difficulty 4 (Ultra, 40 pts), category `impair_defenses`, `ScriptCheck`
(artifact present = IOC present).

| Name | OS | Diff | Pts | Load mechanism | Config store |
|---|---|---|---|---|---|
| `windows_hide_library` | windows | 4 | 40 | `AppCertDLLs` **and** `AppInit_DLLs` | XOR blob in `HKLM\...\CurrentVersion\Perflib\LastSync` |
| `linux_hide_library` | linux | 4 | 40 | `/etc/ld.so.preload` | XOR blob in `user.net_opts` xattr on `/etc/machine-id` |
| `firewall_hide_library` | firewall | 4 | 40 | `/etc/ld.so.preload` (FreeBSD) | `net_opts` extattr on `/etc/hostid` |

### What the Windows library hides, and from which tool

The hook only fires in processes the library is actually loaded into, so *reachability*
matters as much as the hook itself. Two load mechanisms plus one injection path:

- **AppInit_DLLs** — every process importing `user32.dll` (PowerShell, mmc, explorer).
- **AppCertDLLs** — loaded into a process the first time it calls `CreateProcess`.
- **Child injection** — the library exports `CreateProcessNotify`; when an *already
  injected* process launches an allowlisted child (`net1.exe`, `netstat.exe`,
  `cmd.exe`), the library APC-injects itself into that child before its entry point.
  Those three import no `user32` and are otherwise unreachable.

| Blue-team command | Hidden? | Notes |
|---|---|---|
| `Get-Process`, `tasklist` (PowerShell path) | ✓ | `NtQuerySystemInformation` |
| `Get-ChildItem`, `cmd /c dir` | ✓ | `NtQueryDirectoryFile(Ex)`; `dir` needs the cmd.exe injection |
| `Get-Service` | ✓ | `EnumServicesStatusW` (the non-`Ex` variant .NET uses) |
| `sc query type= all` | ✓ | `EnumServicesStatusExW/A` |
| `net user` | ✓ | `samcli!NetUserEnum` inside `net1.exe`, reached by injection |
| `Get-LocalUser` | ✓ | `SamEnumerateUsersInDomain` |
| `netstat -ano` / `-anob` / `-anoQ` | ✓ | TCP only — the `iphlpapi` `Internal*` table APIs netstat really calls |
| ADUC (`dsa.msc`), `[adsisearcher]` | ✓ | `wldap32!ldap_first_entry`/`ldap_next_entry` |
| `Get-ADUser` | ✓ *after ADWS restart* | Queries ADWS (tcp/9389), not LDAP; deploy restarts the service — see troubleshooting |
| **`netstat -anop UDP`** | ✗ **by design** | UDP hiding is out of scope; UDP endpoints stay visible |
| `Get-NetTCPConnection` | ✗ | Serviced by `WmiPrvSE.exe`, which the library does not reach |
| `tasklist` (WMI path) | ✗ | Modern `tasklist` can query via WMI, bypassing the hook |
| ETW / Sysmon network events | ✗ | Never traverses the user-mode iphlpapi tables |

**Hunt layers:** the library cannot hide its own load mechanism. Check
`HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\AppCertDLLs` and
`HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows\AppInit_DLLs`
(Linux/BSD: `/etc/ld.so.preload`) — a populated value is the tell. Cross-check any
surface in the ✗ rows above against its ✓ counterpart: a socket visible to
`netstat -anop UDP` but absent from `netstat -ano`, or a process in WMI `tasklist`
but not `Get-Process`, means something is filtering the results.

### What the Linux library hides, and from which tool

Loaded via `/etc/ld.so.preload`; config stored in `user.net_opts` xattr on `/etc/machine-id`.

| Blue-team command | Hidden? | Notes |
|---|---|---|
| `ss -tnp` | ✓ | `recvmsg` over netlink; kernel returns the socket table, library filters by HIDE_PROC |
| `netstat -tnp`, `cat /proc/net/tcp` | ✓ | `fopen`/`fopen64` on `/proc/net/tcp` and `/proc/net/tcp6` |
| `ls`, `find` | ✓ | `opendir`/`readdir` filter HIDE_FILE entries |
| `rm <protect_path>` | ✗ (EPERM) | `unlink`/`unlinkat` self-protect the library's own artifacts; bypass token overrides |
| `mv <protect_path>` | ✗ (EPERM) | `rename` self-protect |
| `setfattr`, `getfattr --dump` | ✓ | `setxattr`/`removexattr`/`listxattr` hide and protect the `user.net_opts` config blob |
| `/proc/net/udp` | ✗ **by design** | UDP table not intercepted; only TCP is filtered |

### What the BSD (pfSense) library hides, and from which tool

Loaded via `/etc/ld.so.preload`; config stored in `net_opts` extattr on `/etc/hostid`.

**FreeBSD-specific note:** `netstat` ships setgid `kmem` on FreeBSD. FreeBSD's rtld
unconditionally ignores `LD_PRELOAD` for setgid/setuid binaries (`AT_SECURE=1`). The
deploy script strips the setgid bit (`chmod g-s /usr/bin/netstat`) so the library
loads; FreeBSD 15 `netstat` uses `sysctl` exclusively, never `/dev/kmem`, so this is
safe. The remediate script restores the bit.

| Blue-team command | Hidden? | Notes |
|---|---|---|
| `ps ax` | ✓ | `sysctl kern.proc.proc` via `kern_proc` MIB |
| `netstat -an` | ✓ | `__sysctlbyname("net.inet.tcp.pcblist", …)` from `libsys.so.7`; requires setgid strip |
| `sockstat -4 -6` | ✓ | `cap_sysctl` shims (inside Capsicum sandbox); cache primed pre-sandbox in `init_hooks()` |
| `find`, `ls` | ✓ | `getdirentries`, `fts_children`, `fts_read`, `fdopendir` |
| `rm <protect_path>` | ✗ (EPERM) | `unlink` self-protect; bypass token overrides |
| `setextattr`, `lsextattr` | ✓ | `extattr_set_file`/`extattr_delete_file`/`extattr_list_file` protect the config extattr |

---

## All IOCs by OS

For the full 86-IOC listing, see the IOC Definitions API (`GET /iocs/`).  
For scripting the catalog, filter on `IOC.os` and `IOC.difficulty` fields.

## Implant-Type Spread

> Generated by `scripts/audit/dump_implant_type_spread.py` on 2026-08-30. Re-run when IOC counts change; paste output verbatim to keep this section as the source-of-truth snapshot of the loaded set.

Kind column combines Sliver `format` × control model × check family. See the script's docstring for the full legend. Key columns:

- `exe-session` / `exe-beacon` — Sliver ELF/PE binary; the default path.
- `shared-session` / `shared-beacon` — Sliver `.so` shared-library (freebsd + linux); needs the `so_loader_{freebsd,linux}` native launcher because `.so` is not executable.
- `shellcode-session` — Sliver shellcode format (windows only today, via `windows_implant_inject`).
- `inject` — `SliverCheck` IOC with no `implant_spec`; an inject dropper whose beacon is planted by a companion IOC on the same box.
- `script` — `ScriptCheck` (NXC-only artifact detection; no Sliver session).
- `default-cred` — `DefaultCredCheck` (credential-hunt IOC).

### Per-OS × per-kind counts (86 loaded IOCs)

| OS | default-cred | exe-beacon | exe-session | inject | script | shared-session | shellcode-session | total |
|---|---|---|---|---|---|---|---|---|
| firewall | 1 | 2 | 3 | 2 | 16 | 2 | 0 | 26 |
| linux | 1 | 3 | 3 | 2 | 20 | 2 | 0 | 31 |
| windows | 1 | 3 | 2 | 3 | 19 | 0 | 1 | 29 |

FreeBSD `.so` shared-lib coverage is intentional (`firewall_implant_so_bare`, `firewall_implant_so_rc`, `linux_implant_so_bare`, `linux_implant_so_systemd` — 4 IOCs). Windows has zero `shared-session`: Windows DLL hiding-library work is a separate PE-native path, not Sliver `format=shared`. The `service` format is unused today; a nonzero cell there in a future run means a new Sliver Windows service implant IOC landed and should be documented here.

### Per-IOC rows

| IOC | OS | kind |
|---|---|---|
| `default_cred_firewall` | firewall | default-cred |
| `default_cred_linux` | linux | default-cred |
| `default_cred_windows` | windows | default-cred |
| `firewall_all_users_sudoer` | firewall | script |
| `firewall_disable_fw_rules` | firewall | script |
| `firewall_disable_logging` | firewall | script |
| `firewall_fake_shell` | firewall | script |
| `firewall_hide_library` | firewall | script |
| `firewall_http_server` | firewall | script |
| `firewall_implant_bare` | firewall | exe-session |
| `firewall_implant_daemon_rc` | firewall | exe-session |
| `firewall_implant_devd_kqueue` | firewall | exe-session |
| `firewall_implant_nginx_shim` | firewall | exe-beacon |
| `firewall_implant_so_bare` | firewall | shared-session |
| `firewall_implant_so_rc` | firewall | shared-session |
| `firewall_implant_webshell` | firewall | script |
| `firewall_kill_shim` | firewall | script |
| `firewall_kill_shim_beacon` | firewall | exe-beacon |
| `firewall_pam_any_password` | firewall | script |
| `firewall_random_admins` | firewall | script |
| `firewall_scheduled_add_user` | firewall | script |
| `firewall_ssh_authorized_keys` | firewall | script |
| `linux_adduser_crontab` | linux | script |
| `linux_all_users_sudoer` | linux | script |
| `linux_clean_firewall` | linux | script |
| `linux_disable_firewall` | linux | script |
| `linux_disable_logging` | linux | script |
| `linux_elf_section_embed` | linux | script |
| `linux_fake_false` | linux | script |
| `linux_fake_ntpd` | linux | script |
| `linux_hide_library` | linux | script |
| `linux_http_server` | linux | script |
| `linux_implant_bare` | linux | exe-session |
| `linux_implant_hidden_systemd` | linux | exe-beacon |
| `linux_implant_inject_sshd` | linux | exe-beacon |
| `linux_implant_obvious_systemd` | linux | exe-session |
| `linux_implant_so_bare` | linux | shared-session |
| `linux_implant_so_systemd` | linux | shared-session |
| `linux_implant_udev` | linux | exe-session |
| `linux_kill_shim` | linux | script |
| `linux_kill_shim_beacon` | linux | exe-beacon |
| `linux_mega_cap` | linux | script |
| `linux_mig_suid` | linux | script |
| `linux_pam_any_password` | linux | script |
| `linux_pam_su_permit` | linux | script |
| `linux_priv_user_service` | linux | script |
| `linux_self_inject` | linux | inject |
| `linux_self_inject_udev` | linux | inject |
| `linux_sftp_server` | linux | script |
| `linux_ssh_authorized_keys` | linux | script |
| `linux_xattr_shellcode` | linux | script |
| `firewall_elf_section_embed` | firewall | script |
| `firewall_extattr_shellcode` | firewall | script |
| `firewall_self_inject` | firewall | inject |
| `firewall_self_inject_devd` | firewall | inject |
| `linux_vim_suid` | linux | script |
| `windows_adminsdholder` | windows | script |
| `windows_allow_all_firewall` | windows | script |
| `windows_anon_share` | windows | script |
| `windows_attrib_schtask` | windows | script |
| `windows_disable_logging` | windows | script |
| `windows_easy_admin` | windows | script |
| `windows_guest_admin` | windows | script |
| `windows_hide_library` | windows | script |
| `windows_http_server` | windows | script |
| `windows_ifeo` | windows | script |
| `windows_implant_bare` | windows | exe-session |
| `windows_implant_hidden_service` | windows | exe-beacon |
| `windows_implant_inject` | windows | shellcode-session |
| `windows_implant_inject_stc` | windows | inject |
| `windows_implant_obvious_task` | windows | exe-session |
| `windows_implant_wmi` | windows | exe-beacon |
| `windows_kill_respawn` | windows | script |
| `windows_kill_shim_beacon` | windows | exe-beacon |
| `windows_pe_section_embed` | windows | script |
| `windows_priv_user_service` | windows | script |
| `windows_rid_hijack` | windows | script |
| `windows_self_inject` | windows | inject |
| `windows_self_inject_wmi` | windows | inject |
| `windows_sftp_server` | windows | script |
| `windows_shellcode_registry` | windows | script |
| `windows_ssh_authorized_keys` | windows | script |
| `windows_ssh_server` | windows | script |
| `windows_user_scheduled_task` | windows | script |

No IOCs were dropped by the loader on this snapshot; every module in `iocs/definitions/` was accepted with a validated check/deploy/remediate script triad (or `SliverCheck` with `implant_spec`) and every non-runtime payload was present. A future run that drops IOCs adds a `## Dropped by loader` section to the script output; paste it here so the counts above cannot silently misrepresent the loaded set.

---

### Windows (29 IOCs)

Includes credential access, persistence, impair-defenses, and remote access categories.
Key technique families: implant injection (STC, LanmanServer DLL), persistence (registry
run keys, service, WMI, scheduled tasks, IFEO), impair (Defender exclusion, firewall rules),
credential access (LSASS dump, SAM), fileless storage (registry shellcode, PE section),
RID hijack, SSH authorized keys, AdminSDHolder ACE.

### Linux (31 IOCs)

Includes remote access implants (ELF + `.so` shared-lib), persistence (systemd service,
cron, udev, LD_PRELOAD), impair (iptables, auditd, full firewall flush/disable), credential
access (pam_su, sudoers, pam_permit.so), fileless storage (xattr, ELF section), SSH authorized keys.

### Firewall / pfSense (26 IOCs)

Includes remote access implants (ELF + `.so` shared-lib), persistence (devd, FreeBSD rc.d,
pfSense package hooks), fileless storage (extattr, ELF section), firewall rule injection,
sudoers drop-in, pam_permit.so, SSH authorized keys.

---

## Scoring invariants

- `status=1` (IOC present) → team has **not** remediated; points held by red team
- `status=0` (IOC absent) → team remediated; points awarded to blue team
- `status=-1` (check failed/error) → no score change; alert emitted to operator
- Split scoring: effect 40% + persistence 40% + completion bonus 20% of base difficulty points
- Backup-copy IOCs: `status=1` while primary **or** backup present; partial credit not applicable
  for the binary itself (binary presence is an effect-only check)

<!-- audited 2026-07-24 — created for Plan 12 + Plan 13; documents 13 new resilient IOCs and 3 backup-copy pivot IOCs. Run check_wiki_anchors.py before any anchor additions. Updated 2026-08-01 — Windows hide-library hook table updated for CreateProcessNotify injection, Internal* TCP hooks, samcli/NetQueryDisplayInformation. Updated 2026-08-02 — added Linux and BSD coverage tables (per-hook what-hides-what). -->
