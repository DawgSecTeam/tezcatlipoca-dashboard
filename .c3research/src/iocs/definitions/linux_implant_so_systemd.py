from iocs.base import IOC, Artifact, HidingEntry, ImplantSpec, SliverCheck
from iocs.persistence import SoLoaderSystemd

linux_implant_so_systemd = IOC(
    name="linux_implant_so_systemd",
    description="Sliver Linux .so shared-lib implant with systemd unit persistence re-launching the loader",
    category="remote_access",
    difficulty=3,
    os="linux",
    check=SliverCheck(),
    implant_spec=ImplantSpec(
        is_beacon=False,
        format="shared",
        persistence_name="SoLoaderSystemd",
        apply_immutable=False,
    ),
    persistence=SoLoaderSystemd,
    pooled_names=[("SERVICE_NAME", "service")],
    payloads=["so_loader_linux"],  # loader binary; the .so is generated live by Sliver
    timestomp=True,
    backup_copy=True,
    wants_hiding=[
        HidingEntry(type="process_name", value_literal="so_loader_linux"),
        HidingEntry(type="service", value_env_key="SERVICE_NAME", value_default="c3r-net"),
    ],
    discovery="Check /etc/systemd/system/ for unusual unit files; check Sliver sessions",
    detection_steps=[
        "List systemd units by mtime: `ls -latr /etc/systemd/system/*.service` and "
        "`systemctl list-units --type=service --all | grep -iE 'c3r|net-proxy'`. A "
        "recently added unit (name pool-randomized, e.g. c3r-net) is the anchor.",
        "Inspect the suspicious unit: `systemctl cat <name>.service`. If its `ExecStart` "
        "runs `<loader> <so> RunSliver` (or references so_loader), it is the IOC.",
        "Verify the referenced .so is a Linux shared library: `file <so_path>` should "
        "report 'ELF 64-bit LSB shared object, x86-64 ... for GNU/Linux'.",
        "Confirm the Sliver session from this box in the C3rberus session list.",
    ],
    remediation_steps=[
        "Stop and disable the unit: `systemctl disable --now <name>.service`.",
        "Kill any running loader: `pkill -9 -f so_loader_linux 2>/dev/null`.",
        "Remove the unit file, loader, and .so, then reload systemd: "
        "`rm -f /etc/systemd/system/<name>.service <loader_path> <so_path> "
        "&& systemctl daemon-reload`.",
        "Confirm no loader remains (`pgrep -f so_loader_linux`) and the Sliver session is "
        "gone. Because the unit relaunches on boot/crash (Restart=), the unit file MUST "
        "be removed — killing the process alone leaves persistence intact.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="systemd .service unit (pool-named) that re-launches the loader"),
        Artifact(dynamic=True, description="so_loader_linux binary (uniquified _<hex>)"),
        Artifact(dynamic=True, description="Sliver Linux .so shared-lib implant"),
    ],
)
