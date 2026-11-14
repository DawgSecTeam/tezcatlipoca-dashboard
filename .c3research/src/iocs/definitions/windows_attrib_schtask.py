from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import WindowsAttribIcacls

windows_attrib_schtask = IOC(
    name="windows_attrib_schtask",
    description="Sliver implant binary protected with attrib+R/icacls DENY; scheduled task triggers at logon",
    category="persistence",
    difficulty=3,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_attrib_schtask_check.ps1"),
    persistence=WindowsAttribIcacls,
    discovery="Check for WinDefSvcHelper scheduled task; check C:\\Windows\\System32\\WinDefSvcHelper.exe with attrib and icacls output",
    deploy_script="deploy_scripts/windows/windows_attrib_schtask_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_attrib_schtask_remediate.ps1",
    # SCHTASK_NAME scored (effect check: Get-ScheduledTask -TaskName $TASK_NAME);
    # drives both task name and binary basename (C:\Windows\System32\$SCHTASK_NAME.exe).
    pooled_names=[("SCHTASK_NAME", "schtask")],
    detection_steps=[
        "List scheduled tasks and filter for the planted entry: "
        "`Get-ScheduledTask -TaskName 'WinDefSvcHelper'` — if it returns a task object the IOC "
        "is present. Also visible via Task Scheduler GUI under the root folder.",
        "Inspect the task action and trigger: "
        "`(Get-ScheduledTask 'WinDefSvcHelper').Actions` should show Execute = "
        "`C:\\Windows\\System32\\WinDefSvcHelper.exe`; "
        "`(Get-ScheduledTask 'WinDefSvcHelper').Triggers` shows an AtLogon trigger. "
        "The task runs as NT AUTHORITY\\SYSTEM at highest privilege and is marked Hidden.",
        "Confirm the implant binary exists and is protected: "
        "`attrib C:\\Windows\\System32\\WinDefSvcHelper.exe` — look for the `R` (read-only) flag. "
        "Then `icacls C:\\Windows\\System32\\WinDefSvcHelper.exe` — a DENY ACE for Everyone "
        "(Delete, Delete Child) and a System integrity label will be present.",
        "Check the ACL via PowerShell for the deny entry: "
        "`(Get-Acl C:\\Windows\\System32\\WinDefSvcHelper.exe).Access | "
        "Where-Object { $_.AccessControlType -eq 'Deny' }` — any result confirms the icacls "
        "DENY protection is still in place.",
        "Check file attributes via .NET to confirm the ReadOnly bit: "
        "`[System.IO.File]::GetAttributes('C:\\Windows\\System32\\WinDefSvcHelper.exe')` — "
        "output containing `ReadOnly` means `attrib +R` was applied and a plain `del` will fail.",
    ],
    remediation_steps=[
        "Strip the read-only attribute FIRST (before attempting deletion): "
        "`attrib -R C:\\Windows\\System32\\WinDefSvcHelper.exe` — this must succeed before any "
        "file-delete command will work.",
        "Remove the DENY ACE added by icacls: "
        "`icacls C:\\Windows\\System32\\WinDefSvcHelper.exe /remove:d Everyone` "
        "then reset the integrity level: "
        "`icacls C:\\Windows\\System32\\WinDefSvcHelper.exe /setintegritylevel Medium`.",
        "Delete the implant binary (now unprotected): "
        "`Remove-Item C:\\Windows\\System32\\WinDefSvcHelper.exe -Force`.",
        "Unregister the scheduled task: "
        "`Stop-ScheduledTask -TaskName 'WinDefSvcHelper' -ErrorAction SilentlyContinue; "
        "Unregister-ScheduledTask -TaskName 'WinDefSvcHelper' -Confirm:$false`.",
        "Verify both artifacts are gone: `Get-ScheduledTask 'WinDefSvcHelper'` should return "
        "nothing, and `Test-Path C:\\Windows\\System32\\WinDefSvcHelper.exe` should return False. "
        "If the binary is still present the attrib/icacls strip did not complete — re-run steps "
        "1-3 from an elevated (Administrator) prompt.",
    ],
    artifacts=[
        Artifact(
            path="WinDefSvcHelper",
            description="Scheduled task registered at root level; triggers at any user logon, "
            "runs as NT AUTHORITY\\SYSTEM at highest privilege, hidden flag set",
        ),
        Artifact(
            path="C:\\Windows\\System32\\WinDefSvcHelper.exe",
            description="Sliver implant binary protected with attrib +R (ReadOnly) and "
            "icacls DENY Everyone:(Delete,DeleteChild) at System integrity level; "
            "cannot be deleted without stripping protection first",
        ),
        Artifact(
            path="C:\\Windows\\System32\\Tasks\\WinDefSvcHelper",
            description="On-disk scheduled task XML definition file written by "
            "Register-ScheduledTask",
        ),
    ],
)
