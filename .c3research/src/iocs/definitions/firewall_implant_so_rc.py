from iocs.base import IOC, Artifact, HidingEntry, ImplantSpec, SliverCheck
from iocs.persistence import SoLoaderDaemonRC

firewall_implant_so_rc = IOC(
    name="firewall_implant_so_rc",
    description="Sliver FreeBSD .so shared-lib implant with rc.d service persistence re-launching the loader",
    category="remote_access",
    difficulty=3,
    os="firewall",
    check=SliverCheck(),
    implant_spec=ImplantSpec(
        is_beacon=False,
        format="shared",
        persistence_name="SoLoaderDaemonRC",
        apply_immutable=False,
    ),
    persistence=SoLoaderDaemonRC,
    pooled_names=[("SERVICE_NAME", "service")],
    payloads=["so_loader_freebsd"],  # loader binary; the .so is generated live by Sliver
    timestomp=True,
    backup_copy=True,
    wants_hiding=[
        HidingEntry(type="process_name", value_literal="so_loader_freebsd"),
        HidingEntry(type="service", value_env_key="SERVICE_NAME", value_default="c3r_net"),
    ],
    discovery="Check /usr/local/etc/rc.d/ for unusual service scripts; check Sliver sessions",
    detection_steps=[
        "List rc.d service scripts by mtime: `ls -latr /usr/local/etc/rc.d/`. A recently "
        "added script is the persistence anchor (name pool-randomized, e.g. c3r_net).",
        "Inspect the suspicious rc.d script: `cat /usr/local/etc/rc.d/<name>`. If its "
        "start action runs `daemon -f <loader> <so> RunSliver` (or references so_loader), "
        "it is the IOC.",
        "Check whether the service is enabled/running: `service <name> status` and "
        "`grep <name> /etc/rc.conf /etc/rc.conf.local 2>/dev/null`.",
        "Confirm the Sliver session from this box in the C3rberus session list.",
    ],
    remediation_steps=[
        "Stop and disable the service: `service <name> stop; sysrc -x <name>_enable 2>/dev/null`.",
        "Kill any running loader: `pkill -9 -f so_loader_freebsd 2>/dev/null`.",
        "Remove the rc.d script, loader, and .so: "
        "`rm -f /usr/local/etc/rc.d/<name> <loader_path> <so_path>`.",
        "Confirm no loader remains (`pgrep -f so_loader_freebsd`) and the Sliver session is "
        "gone. Because the rc.d script relaunches on boot, the script MUST be removed — "
        "killing the process alone leaves persistence intact.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="rc.d service script (pool-named) that re-launches the loader"),
        Artifact(dynamic=True, description="so_loader_freebsd binary (uniquified _<hex>)"),
        Artifact(dynamic=True, description="Sliver FreeBSD .so shared-lib implant"),
    ],
)
