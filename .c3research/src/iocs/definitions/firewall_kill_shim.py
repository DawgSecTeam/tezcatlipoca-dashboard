from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import FirewallKillShimImmutable

firewall_kill_shim = IOC(
    name="firewall_kill_shim",
    description="Shim replaces /bin/kill on pfSense to respawn beacon; chflags schg prevents removal",
    category="impair_defenses",
    difficulty=3,
    os="firewall",
    check=ScriptCheck(script="check_scripts/firewall/firewall_kill_shim_check.sh"),
    deploy_script="deploy_scripts/firewall/firewall_kill_shim_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_kill_shim_remediate.sh",
    payloads=["kill_shim_freebsd", "beacon_freebsd"],
    persistence=FirewallKillShimImmutable,
    timestomp=False,  # chflags schg runs inside deploy_script; stomp after = EPERM
    discovery=(
        "sha256 /bin/kill (mismatch); "
        "ls -lo /bin/kill (schg flag); "
        "test -f /bin/.kill.orig; "
        "mount | grep ' / ' (rw = deploy anomaly window)"
    ),
    sa_highlight={},
    detection_steps=[
        "Check whether the backup of the original binary exists: `test -f /bin/.kill.orig && echo SHIM_PRESENT`. "
        "Its presence alone confirms the shim has been deployed.",
        "Verify the replacement binary is oversized: `stat -f %z /bin/kill`. "
        "A legitimate pfSense /bin/kill is a few kilobytes; the shim exceeds 200 000 bytes.",
        "Confirm the schg (system immutable) flag is set on both files: `ls -lo /bin/kill /bin/.kill.orig`. "
        "The flag column will contain 'schg' when the persistence is active.",
        "Check whether the beacon process is running: `pgrep -f /var/db/.beacon` or "
        "`ps aux | grep '\\.beacon'`. The daemon is launched via `/usr/sbin/daemon -f /var/db/.beacon` "
        "at deploy time and may continue running independently of the shim.",
    ],
    remediation_steps=[
        "Remount the base filesystem read-write so file operations succeed: `/sbin/mount -uw /`.",
        "Clear the schg immutable flag from both the shim and the backup: "
        "`chflags noschg /bin/kill /bin/.kill.orig`. "
        "This step is mandatory before any mv or rm will succeed on these paths.",
        "Kill the running beacon process: `pkill -f /var/db/.beacon`.",
        "Restore the original binary: `mv /bin/.kill.orig /bin/kill && chmod 755 /bin/kill`.",
        "Remove the beacon binary: `rm -f /var/db/.beacon`.",
        "Re-run the check (`ls -lo /bin/kill` — no schg, size normal; "
        "`test -f /bin/.kill.orig` — absent; `pgrep -f /var/db/.beacon` — no output) "
        "to confirm full remediation.",
    ],
    artifacts=[
        Artifact(
            path="/bin/kill",
            description="Replaced by kill_shim_freebsd payload; schg flag set",
        ),
        Artifact(
            path="/bin/.kill.orig",
            description="Original /bin/kill moved here before shim install; schg flag set",
        ),
        Artifact(
            path="/var/db/.beacon",
            description="beacon_freebsd payload placed here and launched via daemon(8)",
        ),
    ],
)
