from iocs.base import IOC, ScriptCheck, Artifact

windows_shellcode_registry = IOC(
    name="windows_shellcode_registry",
    description=(
        "Sliver shellcode stored base64-encoded in HKLM:\\SOFTWARE\\Microsoft\\<REG_SUBKEY>; "
        "scheduled task loader (C3R_SCHTASK_NAME) reads registry value, decodes, "
        "VirtualAllocEx(RWX) + CreateRemoteThread into svchost every 5 minutes."
    ),
    category="persistence",
    difficulty=3,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_shellcode_registry_check.ps1"),
    deploy_script="deploy_scripts/windows/windows_shellcode_registry_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_shellcode_registry_remediate.ps1",
    pooled_names=[("SCHTASK_NAME", "schtask"), ("REG_SUBKEY", "service")],
    # The scored artifact is the shellcode itself, so a real ~509-byte stager is
    # generated per deploy and injected as C3R_SHELLCODE_B64. Without this the
    # deploy planted an empty registry value and the IOC scored as remediated the
    # instant it landed. See docs/PLAN_shellcode_registry_stager.md.
    needs_stager=True,
    detection_steps=[
        "Check registry for shellcode value: `Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\<REG_SUBKEY>' -ErrorAction SilentlyContinue`.",
        "Check for scheduled task: `Get-ScheduledTask -TaskName '<C3R_SCHTASK_NAME>' -ErrorAction SilentlyContinue`.",
        "Check for injected svchost threads with unexpected network connections.",
    ],
    remediation_steps=[
        "Unregister the scheduled task: `Unregister-ScheduledTask -TaskName '<C3R_SCHTASK_NAME>' -Confirm:$false`.",
        "Remove the registry shellcode: `Remove-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\<REG_SUBKEY>' -Name 'SvcIndex' -ErrorAction SilentlyContinue`; if subkey is now empty, remove it too.",
        "Kill any svchost instances hosting the injected beacon thread.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="HKLM:\\SOFTWARE\\Microsoft\\<C3R_REG_SUBKEY> registry value containing base64-encoded Sliver shellcode"),
        Artifact(dynamic=True, description="Scheduled task C3R_SCHTASK_NAME that reads registry + injects every 5 minutes"),
    ],
)
