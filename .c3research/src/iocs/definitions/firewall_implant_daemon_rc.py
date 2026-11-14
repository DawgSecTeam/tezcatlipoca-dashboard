from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import ImplantDaemonRC

firewall_implant_daemon_rc = IOC(
    name="firewall_implant_daemon_rc",
    description="Sliver FreeBSD ELF persisted via daemon(8) rc.d service (pooled service name)",
    category="remote_access",
    difficulty=1,
    os="firewall",
    check=SliverCheck(),
    implant_spec=ImplantSpec(is_beacon=False, persistence_name="ImplantDaemonRC"),
    persistence=ImplantDaemonRC,
    # SERVICE_NAME scored: persistence check keys on rc.d script existence.
    pooled_names=[("SERVICE_NAME", "service")],
    payloads=["beacon_freebsd"],
    discovery="Check /usr/local/etc/rc.d/syslog_relay; check Sliver sessions",
    detection_steps=[
        "Check whether the rc.d service script exists: `ls -la /usr/local/etc/rc.d/syslog_relay`. "
        "Presence of this file confirms the persistence layer is installed.",
        "Inspect the rc.d script to identify the implant binary path: "
        "`cat /usr/local/etc/rc.d/syslog_relay`. Look for the `command=` line pointing to "
        "`/usr/local/bin/syslog_relay` (or an operator-overridden path).",
        "Check whether the implant binary is on disk and running: "
        "`ls -la /usr/local/bin/syslog_relay` and `pgrep -lf syslog_relay`. "
        "The process is launched via `daemon -p /var/run/syslog_relay.pid -f <binary>`, "
        "so the PID file `/var/run/syslog_relay.pid` will also be present while it is running.",
        "Query the service status via the rc.d subsystem: "
        "`service syslog_relay status`. On pfSense the service is set to auto-start "
        "(`syslog_relay_enable=YES` in the rc.d script itself).",
        "Confirm an active C2 session in Sliver: run `sessions` in the Sliver console and "
        "look for a session whose remote address matches this firewall box's IP. A live session "
        "means the implant is beaconing out.",
    ],
    remediation_steps=[
        "Stop the service using both the rc.d helper and the service command: "
        "`/bin/sh /usr/local/etc/rc.d/syslog_relay onestop` and "
        "`service syslog_relay stop`.",
        "Kill any surviving daemon process and clean up the PID file: "
        "`pkill -f /usr/local/bin/syslog_relay; rm -f /var/run/syslog_relay.pid`.",
        "Remount the root filesystem read-write before removing files (pfSense mounts / "
        "read-only by default): `/sbin/mount -uw /`.",
        "Delete the rc.d script and the implant binary: "
        "`rm -f /usr/local/etc/rc.d/syslog_relay /usr/local/bin/syslog_relay`. "
        "Verify removal: `ls /usr/local/etc/rc.d/syslog_relay` should return 'No such file'. "
        "Also confirm `pgrep -lf syslog_relay` returns nothing and the Sliver session "
        "for this box drops — the rc.d persistence re-triggers within ~5 seconds if either "
        "the rc.d script or the binary was left behind.",
    ],
    artifacts=[
        Artifact(
            path="/usr/local/etc/rc.d/syslog_relay",
            description="FreeBSD rc.d service script that auto-starts the implant via daemon(8)",
        ),
        Artifact(
            path="/usr/local/bin/syslog_relay",
            description="Sliver FreeBSD ELF implant binary (default drop path; operator may override via C3R_BINARY)",
        ),
        Artifact(
            path="/var/run/syslog_relay.pid",
            description="PID file written by daemon(8) while the implant process is running",
        ),
        Artifact(
            dynamic=True,
            description="Active Sliver session from the pfSense firewall box — visible in `sessions` in the Sliver console",
        ),
    ],
)
