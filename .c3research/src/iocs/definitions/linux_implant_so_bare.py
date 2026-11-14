from iocs.base import IOC, Artifact, HidingEntry, ImplantSpec, SliverCheck

linux_implant_so_bare = IOC(
    name="linux_implant_so_bare",
    description="Sliver Linux .so shared-lib implant loaded by a native C loader binary",
    category="remote_access",
    difficulty=2,
    os="linux",
    check=SliverCheck(),
    implant_spec=ImplantSpec(is_beacon=False, format="shared"),
    payloads=["so_loader_linux"],  # loader binary; the .so is generated live by Sliver
    timestomp=True,
    backup_copy=True,
    wants_hiding=[
        HidingEntry(type="process_name", value_literal="so_loader_linux"),
    ],
    discovery=(
        "Check running processes for so_loader_linux; check /proc/<pid>/maps for a .so "
        "in a deploy directory; check Sliver sessions"
    ),
    detection_steps=[
        "Check for the loader process: `ps aux | grep so_loader`. The loader dlopen()s "
        "the .so and calls RunSliver in a thread, so it is the running process — not the "
        ".so itself.",
        "Find the mapped implant .so via the loader's memory maps: "
        "`grep -E '\\.so|deleted' /proc/<loader_pid>/maps` and read the loader's argv: "
        "`tr '\\0' ' ' < /proc/<loader_pid>/cmdline`. The first arg is the .so path.",
        "Verify the file is a Linux shared library: `file <so_path>` should report "
        "'ELF 64-bit LSB shared object, x86-64 ... for GNU/Linux'.",
        "Confirm via the C3rberus session list that an active Sliver session originates "
        "from this box. This IOC has no persistence — killing the loader and removing "
        "both files eliminates the session.",
    ],
    remediation_steps=[
        "Kill the loader process (it holds the .so thread alive): "
        "`pkill -9 -f so_loader_linux 2>/dev/null`.",
        "Remove both the loader binary and the .so file (paths from `/proc/<pid>/maps` "
        "and cmdline above): `rm -f <loader_path> <so_path>`.",
        "Confirm no loader remains: `pgrep -f so_loader_linux` returns nothing, and the "
        "Sliver session is gone from the C3rberus session list.",
    ],
    artifacts=[
        Artifact(dynamic=True,
                 description="Sliver Linux .so shared-lib implant; no extension on target"),
        Artifact(dynamic=True,
                 description="so_loader_linux binary (uniquified _<hex>) that dlopen-launches the .so"),
    ],
)
