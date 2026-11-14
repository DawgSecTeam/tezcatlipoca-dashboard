from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import ImplantHiddenSystemd

linux_implant_hidden_systemd = IOC(
    name="linux_implant_hidden_systemd",
    description="Sliver ELF deployed as /usr/local/bin/.syslogd with a disguised systemd service (pooled service name)",
    category="remote_access",
    difficulty=2,
    os="linux",
    check=SliverCheck(preferred_type="beacon"),
    implant_spec=ImplantSpec(is_beacon=True, persistence_name="ImplantHiddenSystemd"),
    persistence=ImplantHiddenSystemd,
    # SERVICE_NAME scored: persistence check keys on the service name.
    pooled_names=[("SERVICE_NAME", "service")],
    payloads=["beacon_linux"],
    discovery="Check systemctl for a service named systemd-journal-compress; inspect ExecStart path for /usr/local/bin/.syslogd",
    detection_steps=[
        "Check whether the disguised service unit exists and is running: "
        "`systemctl status systemd-journal-compress`. A legitimate "
        "`systemd-journal-compress` timer ships with some distros — confirm it "
        "is a *service* (not a timer) and its `ExecStart=` points to "
        "`/usr/local/bin/.syslogd` rather than a real journald helper.",
        "Inspect the unit file directly: "
        "`cat /etc/systemd/system/systemd-journal-compress.service`. "
        "The planted file lists `ExecStart=/usr/local/bin/.syslogd` (or the "
        "build-specific binary path) and `Restart=always`.",
        "Confirm the hidden binary exists: `ls -la /usr/local/bin/.syslogd`. "
        "The leading dot makes it invisible to a plain `ls /usr/local/bin/` — "
        "use `ls -la` or `find /usr/local/bin -name '.*'` to surface it.",
        "Verify an active Sliver beacon session from this box is visible in the "
        "C3rberus C2 panel (`/c2/targets`) — the beacon calls out over mTLS "
        "from the `.syslogd` process.",
    ],
    remediation_steps=[
        "Stop and disable the disguised service: "
        "`sudo systemctl stop systemd-journal-compress && "
        "sudo systemctl disable systemd-journal-compress`.",
        "Delete the planted unit file and reload the daemon: "
        "`sudo rm -f /etc/systemd/system/systemd-journal-compress.service && "
        "sudo systemctl daemon-reload && "
        "sudo systemctl reset-failed systemd-journal-compress`.",
        "Remove the hidden implant binary: `sudo rm -f /usr/local/bin/.syslogd`. "
        "Confirm it is gone: `ls -la /usr/local/bin/.syslogd` should report "
        "no such file.",
        "Confirm the unit no longer exists: `systemctl cat systemd-journal-compress` "
        "should return an error. Wait 30 seconds and re-run "
        "`systemctl is-active systemd-journal-compress` — it should report "
        "`inactive` or `failed` (not `active`).",
    ],
    artifacts=[
        Artifact(
            path="/etc/systemd/system/systemd-journal-compress.service",
            description="Planted systemd unit file; ExecStart points to the hidden .syslogd binary",
        ),
        Artifact(
            path="/usr/local/bin/.syslogd",
            description="Hidden Sliver beacon ELF; dot-prefixed name conceals it from plain ls",
        ),
        Artifact(
            dynamic=True,
            description="Sliver beacon binary (build-specific name/path) deployed as the .syslogd payload",
        ),
    ],
)
