from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck

windows_kill_shim_beacon = IOC(
    name="windows_kill_shim_beacon",
    description=r"Sliver beacon at C:\Windows\System32\WinDefSvcHelper.exe — respawn target for windows_kill_respawn",
    category="remote_access",
    difficulty=3,
    os="windows",
    check=SliverCheck(preferred_type="beacon"),
    implant_spec=ImplantSpec(is_beacon=True),
    payloads=["msupdate.exe"],
    discovery=r"Check Sliver sessions/beacons; process: WinDefSvcHelper.exe in System32",
    sa_highlight={"processes": ["WinDefSvcHelper.exe"]},
    detection_steps=[
        "Check for the running beacon process: open Task Manager or run "
        "`Get-Process -Name WinDefSvcHelper -ErrorAction SilentlyContinue` in PowerShell. "
        "A result confirms the Sliver beacon is currently executing from System32.",
        "Verify the binary exists on disk: "
        r"`Test-Path 'C:\Windows\System32\WinDefSvcHelper.exe'`."
        " A legitimate Windows binary by this name does not exist; any file at this path "
        "is the implant. Confirm it is large (several MB) with "
        r"`(Get-Item 'C:\Windows\System32\WinDefSvcHelper.exe').Length`"
        " — Sliver beacons are typically 10-20 MB.",
        "Inspect the binary to confirm it is the Sliver implant: run "
        r"`Get-AuthenticodeSignature 'C:\Windows\System32\WinDefSvcHelper.exe'`"
        " — the signature will be invalid or absent (unlike legitimate Windows binaries). "
        "Also check `strings` output for the tell-tale Sliver mTLS certificate header.",
        "Correlate with companion IOCs: check for the WMI respawn subscription "
        r"(`Get-WmiObject -Namespace root\subscription -Class __EventFilter"
        r" | Where-Object {$_.Name -eq 'WinSvcEventFilter'}`) and the WinDefSvcHelper "
        "scheduled task (`Get-ScheduledTask -TaskName 'WinDefSvcHelper' -ErrorAction SilentlyContinue`) "
        "— these companion IOCs will re-launch the beacon if not also remediated.",
        "Confirm active C2 registration: check C3rberus C2 > Targets page or run "
        "`sliver-client` and list beacons — the beacon registers under the implant name "
        "built for this team's box and will show the target box IP as the callback address.",
    ],
    remediation_steps=[
        "Terminate the running beacon process before attempting file deletion: "
        "`Stop-Process -Name WinDefSvcHelper -Force -ErrorAction SilentlyContinue`. "
        "Confirm it is gone with `Get-Process -Name WinDefSvcHelper -ErrorAction SilentlyContinue` "
        "— an empty result means the process has stopped.",
        "If the `windows_attrib_schtask` persistence is present, strip file protection FIRST "
        "before deleting: `attrib -R C:\\Windows\\System32\\WinDefSvcHelper.exe` then "
        "`icacls C:\\Windows\\System32\\WinDefSvcHelper.exe /remove:d Everyone` then "
        "`icacls C:\\Windows\\System32\\WinDefSvcHelper.exe /setintegritylevel Medium`. "
        "Skipping this step causes `Remove-Item` to fail with an Access Denied error.",
        "Delete the beacon binary: "
        r"`Remove-Item 'C:\Windows\System32\WinDefSvcHelper.exe' -Force`. "
        "Verify with "
        r"`Test-Path 'C:\Windows\System32\WinDefSvcHelper.exe'`"
        " — it must return False.",
        "Remediate the WMI respawn companion IOC (windows_kill_respawn) to close the "
        "re-launch path: delete the filter, consumer, and binding from root\\subscription — "
        "see that IOC's remediation steps. Until the WMI subscription is removed, "
        "killing the process alone is insufficient; it will restart within 1 second.",
        "Remediate any other companion persistence IOCs (windows_attrib_schtask scheduled task, "
        "windows_ifeo IFEO service WinDefSvcHelper) that would re-drop or re-launch the beacon. "
        "Verify final state: `Get-Process -Name WinDefSvcHelper -ErrorAction SilentlyContinue` "
        "returns empty and the Sliver beacon drops from C3rberus active-beacon list within one "
        "check interval (beacon jitter + scoring cycle).",
    ],
    artifacts=[
        Artifact(
            path=r"C:\Windows\System32\WinDefSvcHelper.exe",
            description=(
                "Sliver beacon binary masquerading as a Windows Defender service helper; "
                "deployed from the msupdate.exe payload. May have attrib +R / icacls DENY "
                "protection applied by the companion attrib_icacls persistence mechanism."
            ),
        ),
        Artifact(
            dynamic=True,
            description=(
                "Sliver beacon registration name — build-specific UUID/name assigned at "
                "implant compile time; visible in C3rberus C2 > Targets and in "
                "`sliver-client` beacon list"
            ),
        ),
    ],
)
