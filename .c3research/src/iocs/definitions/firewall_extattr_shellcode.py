from iocs.base import IOC, ScriptCheck, Artifact

firewall_extattr_shellcode = IOC(
    name="firewall_extattr_shellcode",
    description=(
        "Sliver shellcode stored as base64 in FreeBSD extattr 'system c3r_sc' on a pool-selected "
        "helper script file (C3R_DROP_DIR/C3R_HELPER_NAME). "
        "A cron-scheduled helper script reads it via getextattr, decodes, and execs shellcode."
    ),
    category="persistence",
    difficulty=3,
    os="firewall",
    check=ScriptCheck(script="check_scripts/firewall/firewall_extattr_shellcode_check.sh"),
    deploy_script="deploy_scripts/firewall/firewall_extattr_shellcode_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_extattr_shellcode_remediate.sh",
    pooled_names=[("HELPER_NAME", "service")],
    detection_steps=[
        "Check for extattr on helper file: `getextattr system c3r_sc <C3R_DROP_DIR>/<C3R_HELPER_NAME>` — non-empty = IOC present.",
        "Check crontab: `crontab -l` for helper script reference.",
        "Check for helper script in pool drop dirs (C3R_DROP_DIR).",
    ],
    remediation_steps=[
        "Remove cron entry.",
        "Delete helper script from C3R_DROP_DIR.",
        "Remove extattr from helper file: `rmextattr system c3r_sc <C3R_DROP_DIR>/<C3R_HELPER_NAME>`.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="Helper script file (C3R_DROP_DIR/C3R_HELPER_NAME) with FreeBSD extattr 'system c3r_sc' storing base64-encoded Sliver shellcode"),
        Artifact(dynamic=True, description="Cron-scheduled helper script (C3R_HELPER_NAME) in pool drop dir"),
    ],
)
