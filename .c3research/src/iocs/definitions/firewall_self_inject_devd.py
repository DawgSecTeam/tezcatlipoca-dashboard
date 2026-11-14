from iocs.base import IOC, SliverCheck, PersistenceMechanism, Artifact

DevdSelfInjectPersist = PersistenceMechanism(
    name="devd_self_inject_persist",
    label="Devd Self-Inject Persist",
    tier=3,
    retrigger_interval_seconds=10,
    check_script="check_scripts/firewall/firewall_self_inject_devd_persist_check.sh",
    deploy_script="deploy_scripts/firewall/firewall_self_inject_devd_persist_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_self_inject_devd_persist_remediate.sh",
    tier_group="event",
)

firewall_self_inject_devd = IOC(
    name="firewall_self_inject_devd",
    description=(
        "Dropper ptrace-injects Sliver beacon into syslogd on pfSense and self-deletes; "
        "a devd drop-in rule fires a helper script reading shellcode from extattr "
        "'system c3r_sc' on /etc/hosts and re-injects on system events. "
        "Three layers: injected thread, devd rule, extattr."
    ),
    category="persistence",
    difficulty=4,
    os="firewall",
    check=SliverCheck(preferred_type="beacon"),
    deploy_script="deploy_scripts/firewall/firewall_self_inject_devd_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_self_inject_devd_remediate.sh",
    persistence=DevdSelfInjectPersist,
    sliver_inject=True,  # coupled-shim dropper; no standalone Sliver generate step
    pooled_names=[("HELPER_NAME", "service")],
    detection_steps=[
        "Check for active Sliver beacon in C3rberus.",
        "Check for devd conf: `ls /usr/local/etc/devd/` for unexpected files.",
        "Check for extattr on /etc/hosts: `getextattr system c3r_sc /etc/hosts`.",
        "Check for helper script in pool dirs.",
    ],
    remediation_steps=[
        "Remove devd conf first: `rm /usr/local/etc/devd/<conf>; service devd restart`.",
        "Remove helper script.",
        "Remove extattr: `rmextattr system c3r_sc /etc/hosts`.",
        "Restart syslogd to evict injected thread: `service syslogd restart`.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="Injected thread in syslogd; dropper self-deleted"),
        Artifact(path="/etc/hosts", description="extattr 'system c3r_sc' stores base64-encoded Sliver shellcode"),
        Artifact(dynamic=True, description="devd conf drop-in and helper script in pool dir (C3R_HELPER_NAME)"),
    ],
)
