from iocs.base import IOC, ScriptCheck, Artifact

linux_xattr_shellcode = IOC(
    name="linux_xattr_shellcode",
    description=(
        "Sliver shellcode stored as base64 in xattr 'user.c3r_sc' on /etc/hosts. "
        "A cron-scheduled helper script reads the attribute via getfattr, decodes, "
        "and executes via a memfd_create fileless exec every minute."
    ),
    category="persistence",
    difficulty=3,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_xattr_shellcode_check.sh"),
    deploy_script="deploy_scripts/linux/linux_xattr_shellcode_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_xattr_shellcode_remediate.sh",
    pooled_names=[("HELPER_NAME", "service")],
    detection_steps=[
        "Check for xattr on /etc/hosts: `getfattr -d /etc/hosts` — look for 'user.c3r_sc'.",
        "Check for cron entry: `crontab -l -u root` for helper script reference.",
        "Check for helper script in pool drop dirs.",
        "Check for active Sliver session from Linux target.",
    ],
    remediation_steps=[
        "Remove cron entry referencing the helper script.",
        "Delete the helper script from its drop dir.",
        "Remove the xattr: `setfattr -x user.c3r_sc /etc/hosts`.",
    ],
    artifacts=[
        Artifact(path="/etc/hosts", description="xattr 'user.c3r_sc' stores base64-encoded Sliver shellcode"),
        Artifact(dynamic=True, description="Cron-scheduled helper script (C3R_HELPER_NAME) in pool drop dir"),
    ],
)
