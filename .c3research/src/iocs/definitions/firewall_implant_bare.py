from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck

firewall_implant_bare = IOC(
    name="firewall_implant_bare",
    description="Sliver FreeBSD ELF running as standalone binary only",
    category="remote_access",
    difficulty=1,
    os="firewall",
    check=SliverCheck(),
    implant_spec=ImplantSpec(is_beacon=False),
    payloads=["beacon_freebsd"],
    discovery="Check running processes for unsigned ELF in /tmp; check Sliver sessions",
    detection_steps=[
        "List processes holding the implant binary open: "
        "`fstat /usr/local/bin/netwatch 2>/dev/null`. "
        "Any row beyond the header confirms the process is running. "
        "Also check `ps aux | grep netwatch` and "
        "`ps aux | grep daemon` for the parent `/usr/sbin/daemon -f` wrapper.",
        "Confirm the binary is present on disk: `ls -la /usr/local/bin/netwatch`. "
        "Note the file size — the legitimate pfSense distribution does not ship a "
        "`netwatch` binary in /usr/local/bin; any file there is implant-planted.",
        "Verify the binary is a statically-linked FreeBSD ELF: "
        "`file /usr/local/bin/netwatch`. "
        "A match for 'ELF 64-bit LSB executable, x86-64 ... statically linked, for FreeBSD' "
        "is the beacon payload.",
        "Confirm via the C3rberus Sliver console or session list that an active session "
        "originates from this pfSense box. The implant runs as root with no persistence; "
        "killing the process eliminates the session.",
    ],
    remediation_steps=[
        "Remount the base filesystem read-write so file operations succeed: "
        "`/sbin/mount -uw /`.",
        "Identify and kill all PIDs with the binary open — prefer the fstat method "
        "because the beacon may have renamed its argv via procctl: "
        "`fstat /usr/local/bin/netwatch 2>/dev/null | awk 'NR>1{print $3}' "
        "| sort -u | xargs kill -9 2>/dev/null`. "
        "Then run `pkill -9 -f netwatch 2>/dev/null` as a fallback and wait a moment "
        "before checking again: `sleep 1; pkill -9 -f netwatch 2>/dev/null || true`.",
        "Delete the implant binary: `rm -f /usr/local/bin/netwatch`.",
        "Confirm remediation: `ls /usr/local/bin/netwatch 2>&1` should report "
        "'No such file or directory', and `ps aux | grep netwatch` should return "
        "no matching processes. Verify the Sliver session is gone from the C3rberus "
        "session list — this IOC has no persistence, so killing the process and "
        "removing the binary is sufficient.",
    ],
    artifacts=[
        Artifact(
            path="/usr/local/bin/netwatch",
            description="beacon_freebsd ELF payload; chmod +x then launched via /usr/sbin/daemon -f",
        ),
    ],
)
