from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import ImplantObviousTask

windows_implant_obvious_task = IOC(
    name="windows_implant_obvious_task",
    description="Sliver implant persisted via obviously named scheduled task (pooled task name)",
    category="remote_access",
    difficulty=1,
    os="windows",
    check=SliverCheck(),
    implant_spec=ImplantSpec(is_beacon=False, persistence_name="ImplantObviousTask"),
    persistence=ImplantObviousTask,
    # SCHTASK_NAME scored: persistence check keys on the task name.
    pooled_names=[("SCHTASK_NAME", "schtask")],
    payloads=["msupdate.exe"],
    discovery="Check scheduled tasks for WinUpdaterCheck; check Sliver sessions",
    detection_steps=[
        "List all scheduled tasks at the root path and look for the suspicious entry: "
        "`Get-ScheduledTask -TaskPath '\\' | Where-Object { $_.TaskName -eq 'WinUpdaterCheck' }`.",
        "Inspect the task action to confirm it executes the implant binary: "
        "`(Get-ScheduledTask -TaskName 'WinUpdaterCheck').Actions | Select-Object Execute`"
        " — the execute path should show `C:\\Windows\\Tasks\\WinUpdaterCheck.exe`.",
        "Check whether the implant binary exists on disk: "
        "`Test-Path 'C:\\Windows\\Tasks\\WinUpdaterCheck.exe'`.",
        "Check for a live Sliver session in the C3rberus C2 panel (Targets page) or verify "
        "the process is running: `Get-Process | Where-Object { $_.Path -like '*WinUpdaterCheck*' }`.",
        "Confirm the task runs as SYSTEM with highest privileges: "
        "`(Get-ScheduledTask -TaskName 'WinUpdaterCheck').Principal | "
        "Select-Object UserId, RunLevel`.",
    ],
    remediation_steps=[
        "Stop the running task: `Stop-ScheduledTask -TaskName 'WinUpdaterCheck'`.",
        "Unregister the scheduled task: "
        "`Unregister-ScheduledTask -TaskName 'WinUpdaterCheck' -Confirm:$false`.",
        "Kill the implant process if it is still running: "
        "`Get-Process | Where-Object { $_.Path -eq 'C:\\Windows\\Tasks\\WinUpdaterCheck.exe' }"
        " | Stop-Process -Force`.",
        "Delete the implant binary from disk: "
        "`Remove-Item -Path 'C:\\Windows\\Tasks\\WinUpdaterCheck.exe' -Force`.",
        "Verify remediation is complete — the task must be gone and the binary removed: "
        "`Get-ScheduledTask -TaskName 'WinUpdaterCheck' -ErrorAction SilentlyContinue` should "
        "return nothing, and `Test-Path 'C:\\Windows\\Tasks\\WinUpdaterCheck.exe'` should return False.",
    ],
    artifacts=[
        Artifact(
            path=r"C:\Windows\Tasks\WinUpdaterCheck.exe",
            description="Sliver implant binary (msupdate.exe payload) dropped as WinUpdaterCheck.exe",
        ),
        Artifact(
            path=r"C:\Windows\System32\Tasks\WinUpdaterCheck",
            description="Scheduled task XML definition file; task registered at path \\ running as SYSTEM at logon",
        ),
        Artifact(
            dynamic=True,
            description="Live Sliver C2 session; implant identity (UUID/name) is build-specific",
        ),
    ],
)
