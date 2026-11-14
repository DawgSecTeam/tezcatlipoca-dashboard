from iocs.base import IOC, ScriptCheck, Artifact

windows_pe_section_embed = IOC(
    name="windows_pe_section_embed",
    description=(
        "Sliver shellcode in .c3r_sc section of a legitimate-looking carrier EXE (added via objcopy). "
        "A scheduled task loader reads bytes at PointerToRawData via BinaryReader, "
        "VirtualAllocEx(RWX) + CreateRemoteThread into svchost."
    ),
    category="persistence",
    difficulty=4,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_pe_section_embed_check.ps1"),
    deploy_script="deploy_scripts/windows/windows_pe_section_embed_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_pe_section_embed_remediate.ps1",
    pooled_names=[("CARRIER_NAME", "service"), ("SCHTASK_NAME", "schtask")],
    detection_steps=[
        "Look for a carrier EXE in pool drop dirs: `Get-ChildItem $env:C3R_DROP_DIR -Filter '*.exe' 2>$null`.",
        "Check for .c3r_sc section in the carrier: parse PE header bytes for section name 0x2e 0x63 0x33 0x72 0x5f 0x73 0x63.",
        "Check for the scheduled task: `Get-ScheduledTask -TaskName '<C3R_SCHTASK_NAME>'`.",
    ],
    remediation_steps=[
        "Unregister the scheduled task: `Unregister-ScheduledTask -TaskName '<C3R_SCHTASK_NAME>' -Confirm:$false`.",
        "Delete the carrier EXE: `Remove-Item '$env:C3R_DROP_DIR\\<C3R_CARRIER_NAME>' -Force`.",
        "Kill any injected svchost threads.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="Carrier EXE in pool drop dir with .c3r_sc PE section containing Sliver shellcode"),
        Artifact(dynamic=True, description="Scheduled task C3R_SCHTASK_NAME that reads carrier section + injects"),
    ],
)
