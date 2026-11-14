from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import WindowsIFEOService

_IFEO = "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File Execution Options\\"
_DBG = "IFEO Debugger = 'C:\\Windows\\System32\\cmd.exe /c exit'"

windows_ifeo = IOC(
    name="windows_ifeo",
    description=(
        "IFEO hijacks broad blue-team toolkit: Windows built-ins (taskmgr, mmc, perfmon, resmon, msinfo32) "
        "and Sysinternals threat-hunting tools (procexp, procmon, Autoruns, tcpview, procdump, handle, "
        "listdlls, sigcheck, strings, Sysmon, PsExec, Wireshark). "
        "Watchdog service re-plants all keys every 60 s."
    ),
    category="impair_defenses",
    difficulty=2,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_ifeo_check.ps1"),
    persistence=WindowsIFEOService,
    discovery=(
        "Check HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File Execution Options "
        "for Debugger values on common threat-hunting executables"
    ),
    deploy_script="deploy_scripts/windows/windows_ifeo_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_ifeo_remediate.ps1",
    # SERVICE_NAME scored (persistence check: Get-Service -Name $SVC_NAME); IFEO targets are technique-fixed.
    pooled_names=[("SERVICE_NAME", "service")],
    detection_steps=[
        "Enumerate all IFEO subkeys with a Debugger value: "
        "`Get-ChildItem 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File Execution Options' | "
        "ForEach-Object { Get-ItemProperty $_.PSPath -Name Debugger -ErrorAction SilentlyContinue }`. "
        "Look for subkeys named after common blue-team tools — Windows built-ins "
        "(`taskmgr.exe`, `mmc.exe`, `perfmon.exe`, `resmon.exe`, `msinfo32.exe`) and "
        "Sysinternals tools (`procexp.exe`, `procexp64.exe`, `procmon.exe`, `procmon64.exe`, "
        "`tcpview.exe`, `Autoruns.exe`, `Autoruns64.exe`, `autorunsc.exe`, `autorunsc64.exe`, "
        "`procdump.exe`, `procdump64.exe`, `handle.exe`, `handle64.exe`, `listdlls.exe`, `listdlls64.exe`, "
        "`sigcheck.exe`, `sigcheck64.exe`, `strings.exe`, `strings64.exe`, "
        "`Sysmon.exe`, `Sysmon64.exe`, `PsExec.exe`, `PsExec64.exe`, `Wireshark.exe`).",
        "For any suspicious subkey, read the Debugger value — the planted value is "
        "`C:\\Windows\\System32\\cmd.exe /c exit`, which silently kills the tool on launch.",
        "Check for the watchdog service that re-plants ALL IFEO keys every 60 seconds: "
        "`Get-Service -Name <SVC_NAME>` (service name is pool-driven; check the Task Queue "
        "deploy details or search for an auto-start service whose ImagePath runs a .ps1 from "
        "`C:\\Windows\\Temp\\`). If the service is running, removing registry keys alone is "
        "insufficient — they will reappear within 60 seconds.",
        "Confirm the watchdog script on disk: `Get-Content C:\\Windows\\Temp\\<SVC_NAME>.ps1`. "
        "It contains a `while ($true)` loop iterating over the full target list and "
        "calling `Set-ItemProperty` to restore the Debugger value on each iteration.",
        "Review the service registry key: "
        "`HKLM:\\SYSTEM\\CurrentControlSet\\Services\\<SVC_NAME>`. The `ImagePath` value "
        "should point to `powershell.exe ... -File C:\\Windows\\Temp\\<SVC_NAME>.ps1`.",
    ],
    remediation_steps=[
        "Stop and delete the watchdog service FIRST — removing registry keys without stopping "
        "the service causes them to reappear within 60 seconds: "
        "`Stop-Service -Name <SVC_NAME> -Force; sc.exe delete <SVC_NAME>`.",
        "Delete the watchdog PowerShell script: "
        "`Remove-Item -Path 'C:\\Windows\\Temp\\<SVC_NAME>.ps1' -Force`.",
        "Remove the Debugger value from each IFEO subkey for all targeted executables "
        "(all tools listed in the detection step above): "
        "`Remove-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"
        "\\Image File Execution Options\\<exe>' -Name Debugger -ErrorAction SilentlyContinue`. "
        "If no other values remain in the subkey, also remove the key itself with `Remove-Item`.",
        "Verify remediation: re-run `Get-Service <SVC_NAME>` (should return nothing or error) "
        "and re-enumerate IFEO Debugger values (all should be absent). "
        "Wait 60+ seconds and re-check — any reappearance means the service was not fully "
        "removed and the watchdog is still running.",
    ],
    artifacts=[
        Artifact(
            path=_IFEO + "taskmgr.exe",
            description=f"{_DBG} — silences Task Manager",
        ),
        Artifact(
            path=_IFEO + "mmc.exe",
            description=f"{_DBG} — silences Microsoft Management Console (Event Viewer, Services, etc.)",
        ),
        Artifact(
            path=_IFEO + "perfmon.exe",
            description=f"{_DBG} — silences Performance Monitor",
        ),
        Artifact(
            path=_IFEO + "resmon.exe",
            description=f"{_DBG} — silences Resource Monitor",
        ),
        Artifact(
            path=_IFEO + "msinfo32.exe",
            description=f"{_DBG} — silences System Information",
        ),
        Artifact(
            path=_IFEO + "procexp.exe",
            description=f"{_DBG} — silences Process Explorer",
        ),
        Artifact(
            path=_IFEO + "procexp64.exe",
            description=f"{_DBG} — silences Process Explorer (64-bit)",
        ),
        Artifact(
            path=_IFEO + "procmon.exe",
            description=f"{_DBG} — silences Process Monitor",
        ),
        Artifact(
            path=_IFEO + "procmon64.exe",
            description=f"{_DBG} — silences Process Monitor (64-bit)",
        ),
        Artifact(
            path=_IFEO + "tcpview.exe",
            description=f"{_DBG} — silences TCPView",
        ),
        Artifact(
            path=_IFEO + "Autoruns.exe",
            description=f"{_DBG} — silences Autoruns",
        ),
        Artifact(
            path=_IFEO + "Autoruns64.exe",
            description=f"{_DBG} — silences Autoruns (64-bit)",
        ),
        Artifact(
            path=_IFEO + "autorunsc.exe",
            description=f"{_DBG} — silences Autoruns console (32-bit)",
        ),
        Artifact(
            path=_IFEO + "autorunsc64.exe",
            description=f"{_DBG} — silences Autoruns console (64-bit)",
        ),
        Artifact(
            path=_IFEO + "procdump.exe",
            description=f"{_DBG} — silences ProcDump (memory dump / analysis)",
        ),
        Artifact(
            path=_IFEO + "procdump64.exe",
            description=f"{_DBG} — silences ProcDump (64-bit)",
        ),
        Artifact(
            path=_IFEO + "handle.exe",
            description=f"{_DBG} — silences Handle (open handle enumeration)",
        ),
        Artifact(
            path=_IFEO + "handle64.exe",
            description=f"{_DBG} — silences Handle (64-bit)",
        ),
        Artifact(
            path=_IFEO + "listdlls.exe",
            description=f"{_DBG} — silences ListDLLs (DLL injection detection)",
        ),
        Artifact(
            path=_IFEO + "listdlls64.exe",
            description=f"{_DBG} — silences ListDLLs (64-bit)",
        ),
        Artifact(
            path=_IFEO + "sigcheck.exe",
            description=f"{_DBG} — silences Sigcheck (signature verification / malware detection)",
        ),
        Artifact(
            path=_IFEO + "sigcheck64.exe",
            description=f"{_DBG} — silences Sigcheck (64-bit)",
        ),
        Artifact(
            path=_IFEO + "strings.exe",
            description=f"{_DBG} — silences Strings (binary string extraction)",
        ),
        Artifact(
            path=_IFEO + "strings64.exe",
            description=f"{_DBG} — silences Strings (64-bit)",
        ),
        Artifact(
            path=_IFEO + "Sysmon.exe",
            description=f"{_DBG} — silences Sysmon installer/uninstaller (blocks endpoint monitoring deployment)",
        ),
        Artifact(
            path=_IFEO + "Sysmon64.exe",
            description=f"{_DBG} — silences Sysmon (64-bit)",
        ),
        Artifact(
            path=_IFEO + "PsExec.exe",
            description=f"{_DBG} — silences PsExec (remote IR command execution)",
        ),
        Artifact(
            path=_IFEO + "PsExec64.exe",
            description=f"{_DBG} — silences PsExec (64-bit)",
        ),
        Artifact(
            path=_IFEO + "Wireshark.exe",
            description=f"{_DBG} — silences Wireshark",
        ),
        Artifact(
            path="HKLM:\\SYSTEM\\CurrentControlSet\\Services\\<SVC_NAME>",
            description="Watchdog service registry key (pool-driven name); display name 'Windows Defender Service Helper', auto-start",
        ),
        Artifact(
            path="C:\\Windows\\Temp\\<SVC_NAME>.ps1",
            description="Watchdog PowerShell script; re-plants IFEO Debugger keys for all targets every 60 seconds",
        ),
    ],
)
