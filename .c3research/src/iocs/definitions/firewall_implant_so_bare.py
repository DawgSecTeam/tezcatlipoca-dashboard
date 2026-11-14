from iocs.base import IOC, Artifact, HidingEntry, ImplantSpec, SliverCheck

firewall_implant_so_bare = IOC(
    name="firewall_implant_so_bare",
    description="Sliver FreeBSD .so shared-lib implant loaded by a native C loader binary",
    category="remote_access",
    difficulty=2,
    os="firewall",
    check=SliverCheck(),
    implant_spec=ImplantSpec(is_beacon=False, format="shared"),
    payloads=["so_loader_freebsd"],  # loader binary; the .so is generated live by Sliver
    timestomp=True,
    backup_copy=True,
    wants_hiding=[
        HidingEntry(type="process_name", value_literal="so_loader_freebsd"),
    ],
    discovery=(
        "Check running processes for so_loader_freebsd; check fstat/procstat for a "
        ".so held open in a deploy directory; check Sliver sessions"
    ),
    detection_steps=[
        "Check for the loader process: `ps aux | grep so_loader`. The loader dlopen()s "
        "the .so and calls RunSliver in a thread, so it is the running process — not "
        "the .so itself. `fstat` on the loader PID also shows the .so open.",
        "Inspect the loader's arguments to find the .so path: "
        "`ps -ww -o args= -p <loader_pid>`. The first argument is the implant .so in a "
        "pool deploy directory (e.g. /usr/local/bin/<name>_<hex>).",
        "Verify the file is a FreeBSD shared library: `file <so_path>` should report "
        "'ELF 64-bit LSB shared object, x86-64 ... for FreeBSD'.",
        "Confirm via the C3rberus session list that an active Sliver session originates "
        "from this pfSense box. This IOC has no persistence — killing the loader and "
        "removing both files eliminates the session.",
    ],
    remediation_steps=[
        "Remount the root filesystem read-write: `/sbin/mount -uw /`.",
        "Kill the loader process (it holds the .so thread alive): "
        "`pkill -9 -f so_loader_freebsd 2>/dev/null`.",
        "Remove both the loader binary and the .so file (paths from the `ps`/`fstat` "
        "output above): `rm -f <loader_path> <so_path>`.",
        "Confirm no loader remains: `pgrep -f so_loader_freebsd` returns nothing, and "
        "the Sliver session is gone from the C3rberus session list.",
    ],
    artifacts=[
        Artifact(dynamic=True,
                 description="Sliver FreeBSD .so shared-lib implant; no extension on target"),
        Artifact(dynamic=True,
                 description="so_loader_freebsd binary (uniquified _<hex>) that dlopen-launches the .so"),
    ],
)
