from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck

firewall_kill_shim_beacon = IOC(
    name="firewall_kill_shim_beacon",
    description="Sliver beacon at /var/db/.beacon — deployed by firewall_kill_shim IOC",
    category="remote_access",
    difficulty=3,
    os="firewall",
    check=SliverCheck(preferred_type="beacon"),
    implant_spec=ImplantSpec(is_beacon=True),
    payloads=["beacon_freebsd"],
    discovery="Check Sliver sessions/beacons; process masquerading as [syslogd]",
    sa_highlight={},
    detection_steps=[
        "List running processes and look for a `[syslogd]` entry that is NOT the real syslogd: "
        "`ps aux | grep syslogd`. The beacon masquerades via argv[0] spoofing; the real syslogd "
        "runs from `/usr/sbin/syslogd`, while the beacon's executable path resolves to "
        "`/var/db/.beacon`.",
        "Confirm the beacon binary exists at its fixed drop path: `ls -la /var/db/.beacon`. "
        "A non-zero file there is the Sliver beacon payload.",
        "Check the C3rberus Sliver console or beacon list for an active beacon from the "
        "pfSense box IP — the beacon calls home over mTLS and will appear in `GET /c2/beacons`.",
        "Verify the companion kill-shim IOC is also present (the beacon cannot be killed without "
        "clearing the shim first): `ls -lo /bin/kill` — look for the `schg` flag, and "
        "`test -f /bin/.kill.orig && echo present` to confirm the original binary is stashed.",
    ],
    remediation_steps=[
        "Remount the pfSense root filesystem read-write before making any changes: "
        "`/sbin/mount -uw /`. The base FS is read-only by default; writes will silently fail "
        "without this step.",
        "Clear the `schg` immutable flag on both the shim and the stashed original binary: "
        "`chflags noschg /bin/kill /bin/.kill.orig`. This is required before the shim can be "
        "replaced; `rm` or `mv` will fail with EPERM while `schg` is set.",
        "Kill the running beacon process: `pkill -f /var/db/.beacon`. Do this BEFORE removing "
        "the binary so the kill-shim (if still present) cannot immediately respawn it — the shim "
        "respawns `/var/db/.beacon` when it detects a kill signal targeting that PID.",
        "Restore the original `/bin/kill` binary from the stash: "
        "`mv /bin/.kill.orig /bin/kill && chmod 755 /bin/kill`.",
        "Remove the beacon binary: `rm -f /var/db/.beacon`.",
        "Re-check that `ls -lo /bin/kill` no longer shows `schg`, that `/bin/.kill.orig` is absent, "
        "and that `ls /var/db/.beacon` returns no file — if any artifact remains, the "
        "Sliver beacon will reappear on the next beacon interval.",
    ],
    artifacts=[
        Artifact(
            path="/var/db/.beacon",
            description="Sliver beacon binary (FreeBSD ELF); "
            "process masquerades as [syslogd] via argv[0] spoofing",
        ),
        Artifact(
            path="/bin/kill",
            description="Replaced by kill_shim_freebsd; intercepts "
            "kill signals targeting the beacon PID and respawns /var/db/.beacon before "
            "forwarding the signal to the real /bin/.kill.orig",
        ),
        Artifact(
            path="/bin/.kill.orig",
            description="Original pfSense /bin/kill stashed here "
            "by the deploy script; both files protected with chflags schg",
        ),
    ],
)
