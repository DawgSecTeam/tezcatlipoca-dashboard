from iocs.base import HidingEntry, IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import DevdKqueue

firewall_implant_devd_kqueue = IOC(
    name="firewall_implant_devd_kqueue",
    description="pfSense mTLS session persisted via devd/kqueue ticker; pivot gateway anchor",
    category="remote_access",
    difficulty=4,
    os="firewall",
    check=SliverCheck(preferred_type="session"),
    implant_spec=ImplantSpec(
        is_beacon=False,
        persistence_name="DevdKqueue",
        apply_immutable=True,
        immutable_scope="binary_and_persistence",
        pivot_gateway=True,
        pivot_set_name="devd_kqueue_pivot",
    ),
    persistence=DevdKqueue,
    pooled_names=[("TICKER_NAME", "service")],
    wants_hiding=[HidingEntry(type="process_name", value_env_key="TICKER_NAME")],
    backup_copy=True,
    timestomp=True,
    timestomp_paths=["/usr/local/bin/devd_helper", "/usr/local/etc/devd/ifstatus.conf"],
    payloads=["beacon_freebsd", "devd_helper"],
    discovery="Check /usr/local/etc/devd/ifstatus.conf; check for devd_helper process; check Sliver sessions",
    detection_steps=[
        "Check for the devd event-handler config file: `ls -la /usr/local/etc/devd/ifstatus.conf`."
        " If present it will contain a LINK_UP action that re-launches the ticker binary on"
        " every network interface state change.",
        "Check whether the ticker process is alive via its pidfile:"
        " `cat /var/run/devd_helper.pid` then `kill -0 <pid>`. If both the pidfile exists and"
        " the process responds, persistence is active.",
        "List the ticker binary and note its immutable flag:"
        " `ls -lao /usr/local/bin/devd_helper`. The `schg` flag in the flags column means"
        " `chflags noschg` is required before the file can be deleted.",
        "Confirm the Sliver C2 implant is calling home: check active Sliver sessions on the"
        " C3rberus operator console or run `ps aux | grep -E 'devd_helper|beacon'`"
        " on the pfSense shell to see whether a beacon process is running.",
        "Verify devd has loaded the rogue rule: `service devd status` and"
        " `grep -r LINK_UP /usr/local/etc/devd/` — the malicious entry fires on every"
        " interface link-up event (e.g. reboot or cable reconnect).",
    ],
    remediation_steps=[
        "Kill the running ticker process and remove its pidfile:"
        " `kill $(cat /var/run/devd_helper.pid 2>/dev/null)` then"
        " `pkill -f /usr/local/bin/devd_helper` and `rm -f /var/run/devd_helper.pid`.",
        "The ticker binary and devd config are protected with `chflags schg`."
        " Remount the filesystem read-write, strip the immutable flag, then delete both:"
        " `/sbin/mount -uw / && chflags noschg /usr/local/bin/devd_helper"
        " /usr/local/etc/devd/ifstatus.conf && rm -f /usr/local/bin/devd_helper"
        " /usr/local/etc/devd/ifstatus.conf`.",
        "Reload devd so the removed conf is no longer active:"
        " `service devd reload` (falls back to `pkill -HUP devd` if service command fails).",
        "Locate and kill the Sliver beacon/session process and remove its binary."
        " The implant binary path is operator-chosen at generation time (dynamic); search with"
        " `find /usr/local /tmp /var -type f -newer /etc/version ! -name '*.conf' 2>/dev/null`"
        " or inspect running processes with `ps aux` and terminate any unknown daemon.",
        "Re-check that neither the devd config nor the pidfile reappears after a few minutes:"
        " `ls /usr/local/etc/devd/ifstatus.conf /var/run/devd_helper.pid`. If they return,"
        " a surviving process or a second persistence layer is re-deploying them.",
    ],
    artifacts=[
        Artifact(
            path="/usr/local/etc/devd/ifstatus.conf",
            description="devd event rule that re-launches the ticker on every IFNET LINK_UP event;"
            " protected with chflags schg",
        ),
        Artifact(
            path="/usr/local/bin/devd_helper",
            description="kqueue-based ticker binary that watchdogs the Sliver beacon and relaunches"
            " it if dead; protected with chflags schg",
        ),
        Artifact(
            path="/var/run/devd_helper.pid",
            description="PID file written by the ticker process; used by check script to verify"
            " the ticker is alive",
        ),
        Artifact(
            dynamic=True,
            description="Sliver FreeBSD mTLS beacon binary dropped to an operator-chosen path"
            " at generation time (payload file: beacon_freebsd); calls back over mTLS and serves"
            " as the pivot gateway for the team's internal network",
        ),
    ],
)
