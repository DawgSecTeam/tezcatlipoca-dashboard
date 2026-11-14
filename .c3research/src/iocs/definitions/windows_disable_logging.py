from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import LogCleanupTask

windows_disable_logging = IOC(
    name="windows_disable_logging",
    description="Scheduled task periodically clears Windows event logs (WinDiagCleanup)",
    category="impair_defenses",
    difficulty=2,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_disable_logging_check.ps1"),
    persistence=LogCleanupTask,
    discovery="Check event IDs 1102/104 for recent log clearing; check for WinDiagCleanup task",
    deploy_script="deploy_scripts/windows/windows_disable_logging_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_disable_logging_remediate.ps1",
    # Scheduled-task NAME pool-driven + scored — effect + persistence checks do
    # Get-ScheduledTask -TaskName $TASK_NAME (docs/PLAN_build_check_deploy_name_pools.md
    # §5B). Persistence is immutable=False so protect_paths is display-only.
    pooled_names=[("SCHTASK_NAME", "schtask")],
    detection_steps=[
        "List all scheduled tasks and filter for the planted one: "
        "`Get-ScheduledTask -TaskName 'WinDiagCleanup'` — a result means the IOC is present.",
        "Confirm the task action is clearing event logs: "
        "`(Get-ScheduledTask -TaskName 'WinDiagCleanup').Actions` should show "
        "`Execute: wevtutil.exe` with `Arguments: cl Security`.",
        "Check the task XML on disk: `type "
        '"C:\\Windows\\System32\\Tasks\\WinDiagCleanup"` — look for the '
        "`<Command>wevtutil.exe</Command>` and `<Arguments>cl Security</Arguments>` nodes.",
        "Look for evidence of past log-clearing events: "
        "`Get-WinEvent -LogName Security -FilterXPath "
        '"*[System[EventID=1102]]" -MaxEvents 5` (Event ID 1102 = Security log cleared) '
        "and `Get-WinEvent -LogName System -FilterXPath "
        '"*[System[EventID=104]]" -MaxEvents 5` (Event ID 104 = System log cleared).',
        "Note the task runs as NT AUTHORITY\\SYSTEM with RunLevel Highest and is flagged "
        "Hidden — it will not appear in the Task Scheduler GUI by default; use PowerShell.",
    ],
    remediation_steps=[
        "Stop and remove the scheduled task: "
        "`Unregister-ScheduledTask -TaskName 'WinDiagCleanup' -Confirm:$false`.",
        "Verify the task is gone: `Get-ScheduledTask -TaskName 'WinDiagCleanup' "
        "-ErrorAction SilentlyContinue` should return nothing.",
        "Delete the task definition file left on disk: "
        '`Remove-Item -Force "C:\\Windows\\System32\\Tasks\\WinDiagCleanup"` '
        "(the file may persist even after Unregister-ScheduledTask on some Windows builds).",
        "Re-enable Security and System event logging to confirm logs are now being written: "
        "`wevtutil sl Security /e:true` and `wevtutil sl System /e:true`.",
        "Wait 30 minutes (one task interval) and confirm Event ID 1102 does NOT reappear: "
        "`Get-WinEvent -LogName Security -FilterXPath "
        '"*[System[EventID=1102]]" -MaxEvents 1 -ErrorAction SilentlyContinue`. '
        "If 1102 recurs the task was re-created by another persistence mechanism.",
    ],
    artifacts=[
        Artifact(
            path="C:\\Windows\\System32\\Tasks\\WinDiagCleanup",
            description="Scheduled task XML file; action runs wevtutil.exe cl Security every 30 min",
        ),
        Artifact(
            path="WinDiagCleanup",
            description="Scheduled task registered under the root task folder, runs as SYSTEM, hidden",
        ),
    ],
)
