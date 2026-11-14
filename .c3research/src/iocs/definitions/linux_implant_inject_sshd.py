from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import SshdInjectSystemd

linux_implant_inject_sshd = IOC(
    name="linux_implant_inject_sshd",
    description="Sliver ELF launched via pure-C shim injected into sshd; C2 appears under sshd PID",
    category="remote_access",
    difficulty=3,
    os="linux",
    check=SliverCheck(preferred_type="beacon"),
    implant_spec=ImplantSpec(is_beacon=True, persistence_name="SshdInjectSystemd"),
    persistence=SshdInjectSystemd,
    payloads=["beacon_linux", "libpam_recv.so"],
    deploy_script="deploy_scripts/linux/linux_implant_inject_sshd_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_implant_inject_sshd_remediate.sh",
    discovery="Check sshd process memory for injected library; check systemctl for systemd-ssh-relay",
    detection_steps=[
        "Check whether the LD_PRELOAD drop-in is present for the SSH service "
        "(Ubuntu 24.04 uses `ssh`, others use `sshd`): "
        "`cat /etc/systemd/system/ssh.service.d/security-module.conf` and "
        "`cat /etc/systemd/system/sshd.service.d/security-module.conf`. "
        "A malicious drop-in sets `Environment=LD_PRELOAD=/usr/local/lib/libpam_recv.so`.",
        "Confirm the shim library is on disk: `ls -la /usr/local/lib/libpam_recv.so`. "
        "Check for an immutable flag: `lsattr /usr/local/lib/libpam_recv.so` "
        "(an `i` attribute means `chattr +i` was applied and the file cannot be removed directly).",
        "Look for the staged beacon dotfile and shim staging copy: "
        "`ls -la /tmp/.pam_unix /tmp/libpam_recv.so`. "
        "`/tmp/.pam_unix` is the Sliver beacon binary that `libpam_recv.so` executes when sshd loads; "
        "`/tmp/libpam_recv.so` is the staging copy left behind after installation to `/usr/local/lib/`.",
        "Verify whether sshd is currently running with the shim loaded: "
        "`grep -r libpam_recv /proc/*/maps 2>/dev/null | head` — any hit means the library "
        "is live in an sshd worker process.",
        "Check for the persistence service: `systemctl status systemd-ssh-relay` and "
        "`systemctl cat systemd-ssh-relay`. "
        "If the unit file exists at `/etc/systemd/system/systemd-ssh-relay.service`, the "
        "beacon will be re-launched on the next sshd restart even after a reboot.",
    ],
    remediation_steps=[
        "Remove the systemd drop-in and reload the daemon so sshd no longer inherits LD_PRELOAD: "
        "`rm -f /etc/systemd/system/ssh.service.d/security-module.conf "
        "/etc/systemd/system/sshd.service.d/security-module.conf && systemctl daemon-reload`.",
        "Clear the immutable flag (if set) and delete the shim: "
        "`chattr -i /usr/local/lib/libpam_recv.so 2>/dev/null; "
        "rm -f /usr/local/lib/libpam_recv.so`.",
        "Remove the staged beacon binary: `rm -f /tmp/.pam_unix /tmp/libpam_recv.so`.",
        "Restart SSH so new connections no longer load the shim: "
        "`systemctl restart ssh || systemctl restart sshd`. "
        "Verify it came back up: `systemctl is-active ssh || systemctl is-active sshd`.",
        "Stop, disable, and delete the persistence service that would re-inject on next boot: "
        "`systemctl stop systemd-ssh-relay; systemctl disable systemd-ssh-relay; "
        "rm -f /etc/systemd/system/systemd-ssh-relay.service; systemctl daemon-reload; "
        "systemctl reset-failed systemd-ssh-relay`.",
        "Re-run `grep -r libpam_recv /proc/*/maps 2>/dev/null` after remediation — any remaining "
        "hit means a live sshd worker still holds the shim in memory and sshd must be restarted "
        "again. Also verify `systemctl cat systemd-ssh-relay` returns an error (unit gone).",
    ],
    artifacts=[
        Artifact(
            path="/usr/local/lib/libpam_recv.so",
            description="LD_PRELOAD shim that launches the beacon when sshd loads it; "
            "may have chattr +i immutable flag set",
        ),
        Artifact(
            path="/etc/systemd/system/ssh.service.d/security-module.conf",
            description="Systemd drop-in (Ubuntu 24.04 / systems where ssh.service exists) "
            "that sets LD_PRELOAD=/usr/local/lib/libpam_recv.so on the SSH daemon",
        ),
        Artifact(
            path="/etc/systemd/system/sshd.service.d/security-module.conf",
            description="Systemd drop-in (RHEL/Debian/other distros using sshd.service) "
            "that sets LD_PRELOAD=/usr/local/lib/libpam_recv.so on the SSH daemon",
        ),
        Artifact(
            path="/tmp/.pam_unix",
            description="Staged Sliver beacon binary (hidden dotfile); "
            "loaded and executed by libpam_recv.so when sshd starts",
        ),
        Artifact(
            path="/tmp/libpam_recv.so",
            description="Staging copy of the LD_PRELOAD shim left in /tmp after "
            "installation to /usr/local/lib/; the deploy script uses cp not mv",
        ),
        Artifact(
            path="/etc/systemd/system/systemd-ssh-relay.service",
            description="Persistence unit file; oneshot service that re-launches sshd "
            "with LD_PRELOAD set so the beacon survives sshd restarts and reboots",
        ),
        Artifact(
            dynamic=True,
            description="Sliver beacon process masquerading under the sshd PID tree; "
            "binary content is build-specific (generated per-competition)",
        ),
    ],
)
