from iocs.base import IOC, Artifact, CredentialSpec, ScriptCheck
from iocs.persistence import UserScheduledTask

windows_user_scheduled_task = IOC(
    name="windows_user_scheduled_task",
    description="Scheduled task adds non-privileged user BackupOperator every 15min (growing)",
    category="users",
    difficulty=1,
    os="windows",
    check=ScriptCheck(
        script="check_scripts/windows/windows_user_scheduled_task_check.ps1"
    ),
    persistence=UserScheduledTask,
    discovery="Check local users for BackupOperator; check scheduled tasks for WinUserSync",
    deploy_script="deploy_scripts/windows/windows_user_scheduled_task_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_user_scheduled_task_remediate.ps1",
    # USER_NAME scored (effect check: Get-LocalUser -Name $USER);
    # SCHTASK_NAME scored (persistence check: Get-ScheduledTask -TaskName $TASK_NAME).
    pooled_names=[("USER_NAME", "user"), ("SCHTASK_NAME", "schtask")],
    added_credentials=[
        CredentialSpec(username="BackupOperator", password="B4ckup0per2025!", protocol="smb"),
    ],
    sa_highlight={"local_users_groups": ["BackupOperator"]},
    detection_steps=[
        "List local users and confirm the planted account exists: "
        "`Get-LocalUser | Where-Object { $_.Name -eq 'BackupOperator' }` "
        "or `net user BackupOperator` — a result means the IOC effect is present.",
        "Check for the persistence task: "
        "`Get-ScheduledTask -TaskName 'WinUserSync' | Select-Object TaskName,State,Description` "
        "— the task runs as SYSTEM, is marked Hidden, and executes `net.exe user BackupOperator /active:yes` "
        "on a 1-hour repeating trigger.",
        "Inspect the task action directly to confirm the payload: "
        "`(Get-ScheduledTask -TaskName 'WinUserSync').Actions | Select-Object Execute,Arguments`.",
        "Verify there is also a `WinSyncHelper` scheduled task (created by the IOC effect deploy script): "
        "`Get-ScheduledTask -TaskName 'WinSyncHelper'` — removing only one of the two tasks is "
        "insufficient; the scoring check detects the `BackupOperator` account regardless of which task "
        "recreated it.",
        "Confirm the account's password and SMB access by checking for an active session: "
        "`net user BackupOperator` shows account metadata; credential `B4ckup0per2025!` is the planted password.",
    ],
    remediation_steps=[
        "Remove the WinUserSync persistence task first (so it cannot re-enable the account "
        "while you remove it): "
        "`Stop-ScheduledTask -TaskName 'WinUserSync' -ErrorAction SilentlyContinue; "
        "Unregister-ScheduledTask -TaskName 'WinUserSync' -Confirm:$false`.",
        "Remove the WinSyncHelper task created by the effect deploy script: "
        "`Unregister-ScheduledTask -TaskName 'WinSyncHelper' -Confirm:$false -ErrorAction SilentlyContinue`.",
        "Delete the local user account: `Remove-LocalUser -Name 'BackupOperator'` "
        "or `net user BackupOperator /delete`.",
        "Verify both tasks are gone and the account no longer exists: "
        "`Get-ScheduledTask -TaskName 'WinUserSync','WinSyncHelper' -ErrorAction SilentlyContinue` "
        "(should return nothing) and `Get-LocalUser -Name 'BackupOperator' -ErrorAction SilentlyContinue` "
        "(should return nothing). If either task persists, re-run the Unregister step.",
        "Wait one scoring cycle and recheck `Get-LocalUser -Name 'BackupOperator'` — if the account "
        "reappears, a task was not fully removed.",
    ],
    artifacts=[
        Artifact(
            path="BackupOperator",
            description="Local Windows account created by the IOC; password B4ckup0per2025!",
        ),
        Artifact(
            path=r"C:\Windows\System32\Tasks\WinUserSync",
            description="Persistence scheduled task (SYSTEM, Hidden, hourly) that re-enables BackupOperator",
        ),
        Artifact(
            path="WinSyncHelper",
            description="Additional scheduled task created by the effect deploy script; also runs "
            "net.exe user BackupOperator /active:yes on a 1-hour repeating trigger",
        ),
    ],
)
