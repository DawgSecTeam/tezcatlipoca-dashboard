from iocs.base import IOC, ScriptCheck, Artifact

firewall_elf_section_embed = IOC(
    name="firewall_elf_section_embed",
    description=(
        "Sliver shellcode in .c3r_sc ELF section of a legit-looking carrier binary on pfSense/FreeBSD "
        "(added via objcopy --add-section). Cron-scheduled loader reads section offset via ELF parse, "
        "mmap(RWX), jumps. No ALLOC/EXEC section flags — avoids kernel auto-map."
    ),
    category="persistence",
    difficulty=4,
    os="firewall",
    check=ScriptCheck(script="check_scripts/firewall/firewall_elf_section_embed_check.sh"),
    deploy_script="deploy_scripts/firewall/firewall_elf_section_embed_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_elf_section_embed_remediate.sh",
    pooled_names=[("CARRIER_NAME", "service")],
    detection_steps=[
        "Check pool drop dirs for carrier ELF.",
        "Verify .c3r_sc section: `objdump -h <carrier> | grep .c3r_sc`.",
        "Check crontab for loader.",
    ],
    remediation_steps=[
        "Remove cron entry.",
        "Delete carrier from drop dir.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="Carrier ELF in pool drop dir with .c3r_sc section; named C3R_CARRIER_NAME"),
        Artifact(dynamic=True, description="Cron loader script reading the section and executing shellcode"),
    ],
)
