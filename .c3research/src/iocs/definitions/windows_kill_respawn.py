from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import WmiKillRespawn

windows_kill_respawn = IOC(
    name="windows_kill_respawn",
    description=r"WMI __InstanceDeletionEvent subscription in root\subscription respawns beacon on process termination",
    category="impair_defenses",
    difficulty=3,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_kill_respawn_check.ps1"),
    deploy_script="deploy_scripts/windows/windows_kill_respawn_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_kill_respawn_remediate.ps1",
    payloads=[],  # pure WMI; beacon at C:\Windows\System32\WinDefSvcHelper.exe
    persistence=WmiKillRespawn,
    # WMI_BASE_NAME scored: effect check keys on ${WMI_BASE}Filter; persistence check keys on ${WMI_BASE}Consumer.
    pooled_names=[("WMI_BASE_NAME", "wmi")],
    timestomp=False,  # WMI objects live in CIM repository — timestomping has no effect
    discovery=(
        r"Get-WmiObject -Namespace root\subscription -Class __EventFilter | "
        r"Where-Object {$_.Name -eq 'WinSvcEventFilter'}; "
        r"Get-WmiObject -Namespace root\subscription -Class CommandLineEventConsumer"
    ),
    sa_highlight={},
    detection_steps=[
        r"Query the WMI event filter by name: "
        r"`Get-CimInstance -Namespace 'root\subscription' -ClassName __EventFilter "
        r"-Filter \"Name='WinSvcEventFilter'\"`  — presence means the IOC is active.",
        r"Confirm the matching consumer exists: "
        r"`Get-CimInstance -Namespace 'root\subscription' -ClassName CommandLineEventConsumer "
        r"-Filter \"Name='WinSvcEventConsumer'\"`  — its CommandLineTemplate will point to "
        r"`C:\Windows\System32\WinDefSvcHelper.exe`.",
        r"Verify the binding that wires them together: "
        r"`Get-CimInstance -Namespace 'root\subscription' -ClassName __FilterToConsumerBinding` "
        r"| `Where-Object { $_.Filter -like '*WinSvcEventFilter*' }` — all three objects must be "
        r"removed for full remediation.",
        r"Check whether the beacon binary is on disk: "
        r"`Test-Path 'C:\Windows\System32\WinDefSvcHelper.exe'` — the WMI consumer relaunches "
        r"this binary whenever the process terminates (within 1 second).",
        r"Inspect the WQL subscription query to confirm scope: "
        r"`(Get-CimInstance -Namespace 'root\subscription' -ClassName __EventFilter "
        r"-Filter \"Name='WinSvcEventFilter'\").Query` — should read "
        r"`SELECT * FROM __InstanceDeletionEvent WITHIN 1 WHERE TargetInstance ISA "
        r"'Win32_Process' AND TargetInstance.Name='WinDefSvcHelper.exe'`.",
    ],
    remediation_steps=[
        r"Remove the __FilterToConsumerBinding FIRST (it references both objects below): "
        r"`Get-WmiObject -Namespace 'root\subscription' -Class __FilterToConsumerBinding "
        r"| Where-Object { $_.Filter -like '*WinSvcEventFilter*' } | ForEach-Object { $_.Delete() }`.",
        r"Remove the CommandLineEventConsumer: "
        r"`Get-WmiObject -Namespace 'root\subscription' -Class CommandLineEventConsumer "
        r"-Filter \"Name='WinSvcEventConsumer'\" | ForEach-Object { $_.Delete() }`.",
        r"Remove the __EventFilter last: "
        r"`Get-WmiObject -Namespace 'root\subscription' -Class __EventFilter "
        r"-Filter \"Name='WinSvcEventFilter'\" | ForEach-Object { $_.Delete() }`.",
        r"Kill or stop the beacon process if running, then delete the binary: "
        r"`Stop-Process -Name WinDefSvcHelper -Force -ErrorAction SilentlyContinue; "
        r"Remove-Item 'C:\Windows\System32\WinDefSvcHelper.exe' -Force`.",
        r"Verify all three WMI objects are gone: re-run the detection queries above and confirm "
        r"each returns nothing. The subscription fires within 1 second of process deletion, so "
        r"do NOT delete the binary before removing the WMI subscription — it will immediately "
        r"respawn and may re-register itself.",
    ],
    artifacts=[
        Artifact(
            path=r"root\subscription:__EventFilter[Name='WinSvcEventFilter']",
            description="WMI EventFilter that triggers on WinDefSvcHelper.exe process deletion",
        ),
        Artifact(
            path=r"root\subscription:CommandLineEventConsumer[Name='WinSvcEventConsumer']",
            description=(
                "WMI consumer that relaunches C:\\Windows\\System32\\WinDefSvcHelper.exe "
                "within 1 second of process termination"
            ),
        ),
        Artifact(
            path=r"root\subscription:__FilterToConsumerBinding",
            description="WMI binding linking WinSvcEventFilter to WinSvcEventConsumer",
        ),
        Artifact(
            path=r"C:\Windows\System32\WinDefSvcHelper.exe",
            description="Beacon binary relaunched by the WMI consumer on every process death",
        ),
    ],
)
