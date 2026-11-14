from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck

linux_kill_shim_beacon = IOC(
    name="linux_kill_shim_beacon",
    description="Sliver beacon at /usr/local/bin/networkd — deployed by linux_kill_shim IOC",
    category="remote_access",
    difficulty=3,
    os="linux",
    check=SliverCheck(preferred_type="beacon"),
    implant_spec=ImplantSpec(is_beacon=True),
    payloads=["beacon_linux"],
    discovery="Check Sliver sessions/beacons; process: networkd at /usr/local/bin/networkd",
    sa_highlight={"processes": ["networkd"]},
    detection_steps=[
        "Check for the running beacon process: `ps aux | grep networkd` — look for "
        "`/usr/local/bin/networkd` running as root with no controlling terminal.",
        "Verify the binary exists on disk: `ls -lh /usr/local/bin/networkd`. "
        "A legitimate systemd-networkd daemon lives at `/lib/systemd/systemd-networkd` "
        "or `/usr/lib/systemd/systemd-networkd`; an executable at `/usr/local/bin/networkd` "
        "is the implant.",
        "Confirm it is a large static ELF (the Sliver beacon): "
        "`file /usr/local/bin/networkd` (shows ELF, statically linked) and "
        "`du -sh /usr/local/bin/networkd` (expect several MB for the Sliver binary).",
        "Correlate with the kill-shim companion IOC: check `ls -lh /bin/.kill.orig` "
        "(displaced original kill binary) and "
        "`lsattr /bin/kill` (expect the `i` immutable flag) — the beacon and shim are "
        "deployed together by linux_kill_shim.",
        "Check active Sliver sessions/beacons in C3rberus (C2 > Targets page) or via "
        "`sliver-client` CLI — the beacon registers under the implant name built for "
        "this team's box.",
    ],
    remediation_steps=[
        "Kill the running beacon process: `sudo pkill -f /usr/local/bin/networkd`. "
        "Confirm it is gone with `ps aux | grep networkd`.",
        "Remove the beacon binary: `sudo rm -f /usr/local/bin/networkd`. "
        "The beacon binary itself is NOT made immutable by this IOC — only `/bin/kill` "
        "and `/bin/.kill.orig` carry the `chattr +i` flag (see linux_kill_shim). "
        "`rm` should succeed immediately once the process is dead.",
        "Remediate the companion kill-shim to close the respawn path: clear immutable "
        "flags on the shim (`sudo chattr -i /bin/kill /bin/.kill.orig`), restore the "
        "original binary (`sudo mv /bin/.kill.orig /bin/kill && sudo chmod 755 /bin/kill`).",
        "Verify no cron job or systemd unit will restart the beacon: "
        "`sudo systemctl list-units | grep networkd` (only systemd-networkd from the "
        "OS package should appear) and `sudo crontab -l -u root` (no networkd entry). "
        "If linux_kill_shim_beacon was paired with a persistence mechanism, remediate "
        "that IOC separately.",
        "Confirm remediation: re-run `ps aux | grep networkd` and "
        "`ls /usr/local/bin/networkd` — both should return empty/no-such-file. "
        "The Sliver beacon will drop from the C3rberus active-beacon list within one "
        "check interval (~60 s beacon jitter + scoring cycle).",
    ],
    artifacts=[
        Artifact(
            path="/usr/local/bin/networkd",
            description="Sliver beacon binary masquerading as a network daemon",
        ),
        Artifact(
            dynamic=True,
            description=(
                "Sliver beacon registration name — build-specific UUID/name assigned "
                "at implant compile time; appears in C3rberus C2 > Targets and in "
                "`sliver-client` beacon list"
            ),
        ),
    ],
)
