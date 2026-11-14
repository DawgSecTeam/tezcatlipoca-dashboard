from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import LinuxKillShimImmutable

linux_kill_shim = IOC(
    name="linux_kill_shim",
    description="Shim replaces /bin/kill to respawn beacon on signal; chattr +i prevents removal",
    category="impair_defenses",
    difficulty=3,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_kill_shim_check.sh"),
    deploy_script="deploy_scripts/linux/linux_kill_shim_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_kill_shim_remediate.sh",
    payloads=["kill_shim_linux", "beacon_linux"],
    persistence=LinuxKillShimImmutable,
    timestomp=False,  # chattr +i runs inside deploy_script; stomp after = EPERM
    discovery=(
        "sha256sum /bin/kill (mismatch from package baseline); "
        "lsattr /bin/kill (i flag); "
        "test -f /bin/.kill.orig; "
        "process: networkd at /usr/local/bin/networkd"
    ),
    sa_highlight={"processes": ["networkd"]},
    detection_steps=[
        "Check whether the backup of the original binary exists: `ls -la /bin/.kill.orig`. "
        "Presence of this file is the primary signal that the shim has been deployed.",
        "Check the size of `/bin/kill`: `stat -c '%s %n' /bin/kill`. "
        "The legitimate kill binary is 30–50 KB; the static ELF shim is 800 KB or larger "
        "(>200 000 bytes triggers the scoring check).",
        "Inspect the immutable flag: `lsattr /bin/kill /bin/.kill.orig`. "
        "Both files will show the `i` attribute if the persistence layer is still active; "
        "you must clear this flag before the binary can be replaced.",
        "Look for the beacon process masquerading as a system daemon: "
        "`ps aux | grep networkd` and `ls -la /usr/local/bin/networkd`. "
        "The binary at `/usr/local/bin/networkd` is the Sliver beacon planted alongside the shim.",
        "Verify the sha256 of the current `/bin/kill` against the package baseline: "
        "`dpkg -S /bin/kill` (Debian/Ubuntu) or `rpm -qf /bin/kill` (RHEL), then "
        "`sha256sum /bin/kill` vs `sha256sum /bin/.kill.orig` — the two hashes will differ.",
    ],
    remediation_steps=[
        "Clear the immutable flags from both files FIRST — without this step all subsequent "
        "move/remove operations will fail with EPERM: "
        "`sudo chattr -i /bin/kill /bin/.kill.orig`.",
        "Kill the running beacon: `sudo pkill -f /usr/local/bin/networkd`.",
        "Restore the original kill binary from its displaced backup: "
        "`sudo mv /bin/.kill.orig /bin/kill && sudo chmod 755 /bin/kill`.",
        "Remove the beacon binary: `sudo rm -f /usr/local/bin/networkd`.",
        "Confirm the immutable flag is gone and the binary is stock: "
        "`lsattr /bin/kill` (no `i`), `stat -c '%s' /bin/kill` (≤50 000 bytes), "
        "and `ls /bin/.kill.orig` should return no such file.",
    ],
    artifacts=[
        Artifact(
            path="/bin/kill",
            description="Replaced with static ELF shim; immutable flag set (chattr +i)",
        ),
        Artifact(
            path="/bin/.kill.orig",
            description="Original kill binary displaced here; also made immutable",
        ),
        Artifact(
            path="/usr/local/bin/networkd",
            description="Sliver beacon binary masquerading as a system daemon",
        ),
    ],
)
