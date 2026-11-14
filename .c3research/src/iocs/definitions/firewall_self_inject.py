from iocs.base import IOC, SliverCheck, Artifact

firewall_self_inject = IOC(
    name="firewall_self_inject",
    description=(
        "Coupled-shim dropper ptrace-injects Sliver shellcode into syslogd on pfSense/FreeBSD; "
        "self-deletes via unlink. Dropper uses PT_ATTACH + PT_IO + PT_SETREGS + PT_DETACH. "
        "Active session = IOC present; syslogd restart evicts thread."
    ),
    category="remote_access",
    difficulty=4,
    os="firewall",
    check=SliverCheck(preferred_type="session"),
    deploy_script="deploy_scripts/firewall/firewall_self_inject_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_self_inject_remediate.sh",
    sliver_inject=True,  # coupled-shim dropper; no standalone Sliver generate step
    detection_steps=[
        "Check for an active Sliver beacon from the pfSense firewall in C3rberus.",
        "Inspect syslogd memory for RWX mappings: `procstat vm $(pgrep syslogd) | grep rwx`.",
        "Outbound connections from syslogd: `sockstat | grep syslogd`.",
        "No dropper binary remains on disk after self-delete.",
    ],
    remediation_steps=[
        "Restart syslogd: `service syslogd restart` or `kill -9 $(pgrep syslogd) && service syslogd start`.",
        "Verify the Sliver beacon disconnects in C3rberus.",
        "No binary cleanup needed.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="Injected thread in syslogd on pfSense; dropper self-deleted"),
    ],
)
