from iocs.base import IOC, SliverCheck, PersistenceMechanism, Artifact

UdevSelfInjectPersist = PersistenceMechanism(
    name="udev_self_inject_persist",
    label="Udev Self-Inject Persist",
    tier=3,
    retrigger_interval_seconds=10,
    check_script="check_scripts/linux/linux_self_inject_udev_persist_check.sh",
    deploy_script="deploy_scripts/linux/linux_self_inject_udev_persist_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_self_inject_udev_persist_remediate.sh",
    tier_group="event",
)

linux_self_inject_udev = IOC(
    name="linux_self_inject_udev",
    description=(
        "Dropper ptrace-injects Sliver beacon into systemd-journald and self-deletes; "
        "a udev rule fires a helper script on uinput events that reads shellcode from "
        "xattr 'user.c3r_sc' on /etc/hosts and re-injects whenever journald restarts. "
        "Three layers: injected thread, udev rule, xattr."
    ),
    category="persistence",
    difficulty=4,
    os="linux",
    check=SliverCheck(preferred_type="beacon"),
    deploy_script="deploy_scripts/linux/linux_self_inject_udev_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_self_inject_udev_remediate.sh",
    persistence=UdevSelfInjectPersist,
    sliver_inject=True,  # coupled-shim dropper; no standalone Sliver generate step
    pooled_names=[("HELPER_NAME", "service")],
    detection_steps=[
        "Check for active Sliver beacon in C3rberus.",
        "Check for udev rule: `ls /etc/udev/rules.d/ | grep -v default`.",
        "Check for xattr on /etc/hosts: `getfattr -d /etc/hosts` — look for 'user.c3r_sc'.",
        "Check for helper script in pool dirs.",
    ],
    remediation_steps=[
        "Remove udev rule first: `rm /etc/udev/rules.d/<rule>; udevadm control --reload`.",
        "Remove helper script from drop dir.",
        "Remove xattr: `setfattr -x user.c3r_sc /etc/hosts`.",
        "Restart journald to evict injected thread: `systemctl restart systemd-journald`.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="Injected thread in systemd-journald; dropper self-deleted"),
        Artifact(path="/etc/hosts", description="xattr 'user.c3r_sc' stores base64-encoded Sliver shellcode"),
        Artifact(dynamic=True, description="Udev rule and helper script in pool dir (C3R_HELPER_NAME)"),
    ],
)
