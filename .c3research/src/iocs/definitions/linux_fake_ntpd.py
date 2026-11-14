from iocs.base import IOC, Artifact, ScriptCheck

linux_fake_ntpd = IOC(
    name="linux_fake_ntpd",
    description="Fake ntpd service: static binary listens on TCP 123 and self-restarts via systemd",
    category="impair_defenses",
    difficulty=2,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_fake_ntpd_check.sh"),
    discovery="Check netstat, systemctl",
    deploy_script="deploy_scripts/linux/linux_fake_ntpd_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_fake_ntpd_remediate.sh",
    payloads=["fake_ntpd_linux"],
    detection_steps=[
        "Check whether port 123 (NTP) has an unexpected listener: "
        "`sudo ss -tulpn | grep 123`. The fake binary shows a TCP socket bound "
        "to 123 (real ntpd/chrony use UDP 123, not a TCP listener).",
        "Inspect the running service: `systemctl status ntpd.service`. A "
        "legitimate ntpd entry shows the real ntpd package path; the fake "
        "binary is `/usr/sbin/ntpd` and the service will show `Restart=always` "
        "in its unit.",
        "Read the service unit file: `cat /etc/systemd/system/ntpd.service`. "
        "The planted unit pairs `ExecStart=/usr/sbin/ntpd` with `KillMode=process` "
        "and `Restart=always` — real distro ntpd units do not pair those fields "
        "this way.",
        "Verify the binary at `/usr/sbin/ntpd` is not the legitimate package: "
        "`file /usr/sbin/ntpd` (a statically-linked ELF, not the distro's "
        "dynamically-linked ntpd) and confirm it is not owned by any package "
        "(`rpm -qf /usr/sbin/ntpd` or `dpkg -S /usr/sbin/ntpd` returns no owner). "
        "On connect it spawns an interactive `/bin/bash` wired to the socket.",
        "Confirm the wants symlink exists: "
        "`ls -la /etc/systemd/system/multi-user.target.wants/ntpd.service`. "
        "Its presence means the service persists across reboots.",
    ],
    remediation_steps=[
        "Stop and disable the service to prevent restart: "
        "`sudo systemctl stop ntpd.service && sudo systemctl disable ntpd.service`.",
        "Remove the planted service unit file and its wants symlink: "
        "`sudo rm -f /etc/systemd/system/ntpd.service "
        "/etc/systemd/system/multi-user.target.wants/ntpd.service`.",
        "Delete the fake binary: `sudo rm -f /usr/sbin/ntpd`. If your "
        "distribution uses a real ntpd package, reinstall it afterward: "
        "`sudo apt-get install --reinstall ntp` (Debian/Ubuntu) or "
        "`sudo yum reinstall ntp` (RHEL/CentOS).",
        "Reload systemd so the removed unit is no longer tracked: "
        "`sudo systemctl daemon-reload`.",
        "Verify remediation: confirm port 123 is no longer listening "
        "(`ss -tulpn | grep 123` returns nothing), the binary is absent "
        "(`ls /usr/sbin/ntpd` returns no such file), and the service is "
        "dead (`systemctl is-active ntpd.service` returns `inactive`).",
    ],
    artifacts=[
        Artifact(
            path="/usr/sbin/ntpd",
            description="Fake static ELF binary; listens on TCP 123 and spawns "
            "an interactive /bin/bash wired to each connection",
        ),
        Artifact(
            path="/etc/systemd/system/ntpd.service",
            description="Planted systemd unit with Restart=always and "
            "KillMode=process",
        ),
        Artifact(
            path="/etc/systemd/system/multi-user.target.wants/ntpd.service",
            description="Systemd enable symlink that re-activates the service "
            "on every boot",
        ),
    ],
)
