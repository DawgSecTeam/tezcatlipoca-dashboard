from iocs.base import IOC, SliverCheck, Artifact

linux_self_inject = IOC(
    name="linux_self_inject",
    description=(
        "Coupled-shim dropper writes Sliver shellcode into systemd-journald via /proc/<pid>/mem "
        "(RWX mmap in target), jumps to shellcode via ptrace SETREGS; dropper unlink()s itself. "
        "Active beacon = IOC present; journald restart evicts injected thread."
    ),
    category="remote_access",
    difficulty=3,
    os="linux",
    check=SliverCheck(preferred_type="beacon"),
    deploy_script="deploy_scripts/linux/linux_self_inject_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_self_inject_remediate.sh",
    sliver_inject=True,  # coupled-shim dropper; no standalone Sliver generate step
    detection_steps=[
        "Check for an active Sliver beacon from the Linux target in C3rberus.",
        "Inspect systemd-journald memory for RWX anonymous mappings: `cat /proc/$(pgrep systemd-journald)/maps | grep rwx`.",
        "No dropper binary will remain on disk (self-deleted via unlink).",
        "Outbound network connections from journald: `ss -tp | grep journald`.",
    ],
    remediation_steps=[
        "Restart systemd-journald to evict the injected thread: `systemctl restart systemd-journald`.",
        "Verify the Sliver beacon disconnects in C3rberus after restart.",
        "No binary cleanup needed — dropper self-deleted.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="Injected shellcode thread in systemd-journald process memory; dropper unlinked"),
    ],
)
