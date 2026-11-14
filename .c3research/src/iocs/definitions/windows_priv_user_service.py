from iocs.base import Artifact, IOC, ScriptCheck
from iocs.persistence import WindowsPrivUserService

windows_priv_user_service = IOC(
    name="windows_priv_user_service",
    description="Hidden service re-adds privileged admin user svc_healthmon every 60 seconds",
    category="users",
    difficulty=2,
    os="windows",
    check=ScriptCheck(
        script="check_scripts/windows/windows_priv_user_service_check.ps1"
    ),
    persistence=WindowsPrivUserService,
    discovery="Check local users for svc_healthmon; check services for WinUserAccountHelper",
    deploy_script="deploy_scripts/windows/windows_priv_user_service_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_priv_user_service_remediate.ps1",
    # USER_NAME scored (effect check: Get-LocalUser / Get-ADUser); service "WinUserAccountHelper" fixed.
    pooled_names=[("USER_NAME", "user")],
    added_credentials=[],
    sa_highlight={
        "services": ["WinUserAccountHelper"],
        "processes": ["WinUserAccountHelper"],
    },
    detection_steps=[
        "List local user accounts and check for unexpected privileged accounts: "
        "`Get-LocalGroupMember -Group Administrators` (member/workstation) or "
        "`Get-ADGroupMember -Identity 'Domain Admins' -Recursive` (DC).",
        "Check for the watchdog service that re-creates the accounts every 60 seconds: "
        "`Get-Service -Name WinUserAccountHelper` (or `sc.exe query WinUserAccountHelper`). "
        "The service DisplayName is 'Windows User Account Helper'.",
        "Read the PowerShell watchdog script to see the full planted user list: "
        "`Get-Content C:\\Windows\\Temp\\WinUserAccountHelper.ps1`.",
        "Check the C3rberus task details or deploy_params for the exact list of planted usernames.",
    ],
    remediation_steps=[
        "Stop and delete the persistence service before removing the accounts — if you delete "
        "the accounts first, the service re-creates them within 60 seconds: "
        "`Stop-Service -Name WinUserAccountHelper -Force; sc.exe delete WinUserAccountHelper`.",
        "Remove the watchdog script: "
        "`Remove-Item -Path 'C:\\Windows\\Temp\\WinUserAccountHelper.ps1' -Force`.",
        "Remove each account from Administrators/Domain Admins then delete it: "
        "`Remove-LocalGroupMember -Group Administrators -Member <name>; Remove-LocalUser -Name <name>`.",
        "Verify: `Get-Service -Name WinUserAccountHelper` should return nothing, and each "
        "planted user should no longer appear. Wait 60-90 seconds and re-check — if any "
        "account reappears the service was not fully removed.",
    ],
    artifacts=[
        Artifact(
            dynamic=True,
            description="Up to 3 pool-driven local/domain admin accounts recreated every 60 s by WinUserAccountHelper service",
        ),
        Artifact(
            path="WinUserAccountHelper",
            description=(
                "Windows service (DisplayName: 'Windows User Account Helper') that re-creates "
                "the pool users every 60 s; configured for auto-start with restart-on-failure"
            ),
        ),
        Artifact(
            path=r"C:\Windows\Temp\WinUserAccountHelper.ps1",
            description=(
                "PowerShell watchdog script executed by the service; infinite loop that "
                "recreates all pool users and re-adds them to Administrators/Domain Admins"
            ),
        ),
        Artifact(
            path=r"HKLM:\SYSTEM\CurrentControlSet\Services\WinUserAccountHelper",
            description=(
                "Registry key for the watchdog service; may have Deny ACEs applied by the "
                "immutability hook — strip them before attempting sc.exe delete"
            ),
        ),
    ],
)
