from iocs.base import IOC, SliverCheck, Artifact

windows_self_inject = IOC(
    name="windows_self_inject",
    description=(
        "Coupled-shim dropper injects Sliver beacon shellcode into svchost.exe via "
        "CreateRemoteThread; self-deletes via MoveFileExA(DELAY_UNTIL_REBOOT). "
        "Active beacon session = IOC present; killing the svchost process evicts the thread."
    ),
    category="remote_access",
    difficulty=3,
    os="windows",
    check=SliverCheck(preferred_type="beacon"),
    deploy_script="deploy_scripts/windows/windows_self_inject_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_self_inject_remediate.ps1",
    sliver_inject=True,  # coupled-shim dropper; no standalone Sliver generate step
    detection_steps=[
        "Look for an active Sliver beacon from the Windows target in C3rberus.",
        "Check svchost.exe processes for unexpected network connections: `netstat -ano | findstr ESTABLISHED` matched to svchost PIDs.",
        "Inspect svchost memory for injected threads: use `Get-Process svchost | Where-Object {$_.Modules.Count -gt 30}` or a process-inspection tool.",
        "No dropper binary will remain on disk after injection (scheduled for delete-on-reboot).",
    ],
    remediation_steps=[
        "Identify the svchost process hosting the injected thread (via netstat + PID) and terminate it: `Stop-Process -Id <PID> -Force`.",
        "Windows will restart the svchost service group automatically. Verify the Sliver beacon disconnects.",
        "No binary cleanup needed — dropper scheduled itself for delete on next boot.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="Injected thread in svchost.exe process memory; dropper self-deletes on reboot"),
    ],
)
