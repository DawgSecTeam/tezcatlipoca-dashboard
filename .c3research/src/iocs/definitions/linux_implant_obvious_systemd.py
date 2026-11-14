from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import ImplantObviousSystemd

linux_implant_obvious_systemd = IOC(
    name="linux_implant_obvious_systemd",
    description="Sliver ELF with obviously named systemd service persistence (pooled service name)",
    pooled_names=[("SERVICE_NAME", "service")],
    category="remote_access",
    difficulty=1,
    os="linux",
    check=SliverCheck(),
    implant_spec=ImplantSpec(is_beacon=False, persistence_name="ImplantObviousSystemd"),
    persistence=ImplantObviousSystemd,
    payloads=["beacon_linux"],
    discovery="Check systemctl for systemd-network-proxy; check Sliver sessions",
    detection_steps=[
        "Check whether the service unit exists and is active: "
        "`systemctl status systemd-network-proxy` and "
        "`systemctl is-active systemd-network-proxy`.",
        "Inspect the unit file directly: "
        "`systemctl cat systemd-network-proxy` — confirms the unit exists even if "
        "the service is not currently running.",
        "Identify the implant binary named in `ExecStart`: "
        "`grep ExecStart /etc/systemd/system/systemd-network-proxy.service`. "
        "The default drop path is `/usr/local/bin/systemd-network-proxy`.",
        "Verify the binary is a Sliver ELF: "
        "`file /usr/local/bin/systemd-network-proxy` and "
        "`ls -la /usr/local/bin/systemd-network-proxy` — look for a recently "
        "modified executable not belonging to any installed package.",
        "Confirm an active Sliver C2 session or beacon by checking outbound connections "
        "from the process: `ss -tnp | grep systemd-network-proxy` or "
        "`lsof -i -p $(pgrep -f systemd-network-proxy)`.",
    ],
    remediation_steps=[
        "Stop and disable the malicious service: "
        "`sudo systemctl stop systemd-network-proxy && "
        "sudo systemctl disable systemd-network-proxy`.",
        "Remove the unit file and reload the daemon: "
        "`sudo rm -f /etc/systemd/system/systemd-network-proxy.service && "
        "sudo systemctl daemon-reload && "
        "sudo systemctl reset-failed systemd-network-proxy`.",
        "Delete the implant binary: "
        "`sudo rm -f /usr/local/bin/systemd-network-proxy`. "
        "If `rm` fails with EPERM the file may have `chattr +i` set — run "
        "`sudo chattr -i /usr/local/bin/systemd-network-proxy` first.",
        "Verify the service is fully gone: "
        "`systemctl cat systemd-network-proxy 2>&1` should return "
        '"No files found for systemd-network-proxy" — if it still resolves, '
        "a drop-in or symlink remains and the check will still score as present.",
    ],
    artifacts=[
        Artifact(
            path="/etc/systemd/system/systemd-network-proxy.service",
            description="Systemd unit file that starts the Sliver implant and restarts it on exit",
        ),
        Artifact(
            path="/usr/local/bin/systemd-network-proxy",
            description="Sliver ELF implant binary (default drop path); ExecStart in the unit file",
        ),
        Artifact(
            dynamic=True,
            description="Sliver ELF binary: build-specific name and path set via C3R_BINARY "
            "at generation time; /usr/local/bin/systemd-network-proxy is the default",
        ),
    ],
)
