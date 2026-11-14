from iocs.base import IOC, SliverCheck, PersistenceMechanism, Artifact

WmiSelfInjectRespawn = PersistenceMechanism(
    name="wmi_self_inject_respawn",
    label="WMI Self-Inject Respawn",
    tier=3,
    retrigger_interval_seconds=5,
    check_script="check_scripts/windows/windows_self_inject_wmi_persist_check.ps1",
    deploy_script="deploy_scripts/windows/windows_self_inject_wmi_persist_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_self_inject_wmi_persist_remediate.ps1",
    tier_group="event",
)

windows_self_inject_wmi = IOC(
    name="windows_self_inject_wmi",
    description=(
        "Dropper injects Sliver beacon into svchost.exe and self-deletes; WMI InstanceDeletionEvent "
        "subscription re-injects whenever svchost is restarted, reading shellcode from an ADS "
        "('hosts:c3r_sc') on the hosts file. Three hunt layers: injected thread, WMI subscription, ADS."
    ),
    category="persistence",
    difficulty=4,
    os="windows",
    check=SliverCheck(preferred_type="beacon"),
    deploy_script="deploy_scripts/windows/windows_self_inject_wmi_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_self_inject_wmi_remediate.ps1",
    persistence=WmiSelfInjectRespawn,
    sliver_inject=True,  # coupled-shim dropper; no standalone Sliver generate step
    pooled_names=[("WMI_BASE_NAME", "wmi")],
    detection_steps=[
        "Check for active Sliver beacon in C3rberus.",
        "Check for WMI subscription: `Get-WmiObject -Namespace root\\subscription -Class __EventFilter -Filter \\\"Name LIKE 'c3r%'\\\"` and similarly for CommandLineEventConsumer.",
        "Check for ADS on hosts file: `Get-Item C:\\Windows\\System32\\drivers\\etc\\hosts -Stream *` — look for c3r_sc stream.",
        "No dropper binary on disk after self-delete.",
    ],
    remediation_steps=[
        "Remove WMI subscription first (prevents re-inject): remove __FilterToConsumerBinding, CommandLineEventConsumer, __EventFilter with matching names.",
        "Remove the ADS: `Remove-Item 'C:\\Windows\\System32\\drivers\\etc\\hosts:c3r_sc'`.",
        "Kill the svchost process hosting the beacon to evict the injected thread.",
    ],
    artifacts=[
        Artifact(dynamic=True, description="Injected thread in svchost.exe; dropper self-deleted"),
        Artifact(path="C:\\Windows\\System32\\drivers\\etc\\hosts:c3r_sc", description="ADS on hosts file storing base64-encoded Sliver shellcode"),
        Artifact(dynamic=True, description="WMI EventFilter/Consumer/Binding subscription trio with pooled C3R_WMI_BASE_NAME prefix"),
    ],
)
