from iocs.base import IOC, ScriptCheck, Artifact

linux_elf_section_embed = IOC(
    name="linux_elf_section_embed",
    description=(
        "Sliver shellcode in .c3r_sc ELF section of a legit-looking carrier binary "
        "(added via objcopy --add-section, no ALLOC/EXEC flags). "
        "Cron-scheduled loader reads section via ELF header parse, mmap(RWX), jumps."
    ),
    category="persistence",
    difficulty=4,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_elf_section_embed_check.sh"),
    deploy_script="deploy_scripts/linux/linux_elf_section_embed_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_elf_section_embed_remediate.sh",
    pooled_names=[("CARRIER_NAME", "service")],
    detection_steps=[
        "Check pool drop dirs for carrier ELF: `ls $C3R_DROP_DIR`.",
        "Verify .c3r_sc section: `readelf -S <carrier> | grep -q '.c3r_sc'`.",
        "Check crontab for loader reference.",
    ],
    remediation_steps=[
        "Remove cron entry.",
        "Delete carrier binary from drop dir.",
        "Optionally: kill any running instances if loader is still active.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="Carrier ELF in pool drop dir with .c3r_sc section containing Sliver shellcode; named C3R_CARRIER_NAME"),
        Artifact(dynamic=True, description="Cron loader script that reads the ELF section and execs shellcode"),
    ],
)
