from iocs.base import IOC, Artifact, SliverCheck
from iocs.persistence import ServiceSD

windows_implant_inject_stc = IOC(
    name="windows_implant_inject_stc",
    description="Sliver shellcode injected into spoolsv via CreateRemoteThread + winlogon token impersonation",
    category="remote_access",
    difficulty=3,
    os="windows",
    check=SliverCheck(preferred_type="beacon"),
    sliver_inject=True,
    persistence=ServiceSD,
    # SVC_NAME used by service_sd_deploy.ps1 to name the SYSTEM-only SD service hull.
    pooled_names=[("SVC_NAME", "service")],
    payloads=["spoolmon.exe"],
    discovery="Check spoolsv memory for injected shellcode; scan service SDs; check Sliver sessions",
    detection_steps=[
        "Check for active Sliver sessions or beacons calling back from this box — a new session "
        "appearing on a Windows host after the competition start is the primary indicator.",
        "Look for anomalous remote threads inside the target process (typically spoolsv.exe): "
        "`Get-WmiObject Win32_Thread | Where-Object { $_.ProcessHandle -eq (Get-Process spoolsv).Id }` "
        "or use Process Hacker / Sysmon Event ID 8 (CreateRemoteThread) with SourceImage "
        "matching the injector binary.",
        "Scan for the staging executables in `C:\\Windows\\Temp\\`: "
        "`Get-ChildItem C:\\Windows\\Temp\\spoolmon_*.exe, C:\\Windows\\Temp\\shellcode_*.bin "
        "-ErrorAction SilentlyContinue` — these are the injector and raw shellcode blobs "
        "uploaded just before injection (names contain the first 8 chars of the Sliver session ID).",
        "Detect the SYSTEM-only SD persistence service: run "
        "`foreach ($s in (Get-CimInstance Win32_Service).Name) { $sd = sc.exe sdshow $s; "
        "if ($sd -match 'A;;[^;]*;;;SY' -and $sd -notmatch 'A;;[^;]*;;;BA') { $s } }` "
        "— the service `WinCryptSvc32` (display name: Windows Cryptography Helper) will appear. "
        "Standard `sc query` and Services snap-in hide it from non-SYSTEM callers.",
        "Confirm the service registry key exists: "
        "`Test-Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\WinCryptSvc32'` and check its "
        "DACL for Deny ACEs added by immutable protection: "
        "`(Get-Acl 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\WinCryptSvc32').Access "
        "| Where-Object { $_.AccessControlType -eq 'Deny' }`.",
    ],
    remediation_steps=[
        "Kill the injected thread inside the target process if still running — terminate the "
        "shellcode-spawned Sliver session from the C3rberus C2 page, which signals Sliver to "
        "stop the beacon; alternatively end the remote thread with Process Hacker.",
        "Delete the staging artifacts from Temp: "
        "`Remove-Item C:\\Windows\\Temp\\spoolmon_*.exe, "
        "C:\\Windows\\Temp\\shellcode_*.bin -Force -ErrorAction SilentlyContinue`.",
        "Remove Deny ACEs on the service registry key so sc.exe can delete it: open PowerShell "
        "as SYSTEM (via PsExec or NXC smbexec), then run the remediation pattern — "
        "`$rk = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey("
        "'SYSTEM\\\\CurrentControlSet\\\\Services\\\\WinCryptSvc32', "
        "[Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree, "
        "([System.Security.AccessControl.RegistryRights]::ChangePermissions -bor "
        "[System.Security.AccessControl.RegistryRights]::ReadPermissions)); "
        "$sec = $rk.GetAccessControl(); "
        "foreach ($r in @($sec.Access | Where-Object { $_.AccessControlType -eq 'Deny' })) "
        "{ $sec.RemoveAccessRule($r) | Out-Null }; $rk.SetAccessControl($sec); $rk.Close()`.",
        "Stop and delete the `WinCryptSvc32` persistence service: "
        "`sc.exe stop WinCryptSvc32; sc.exe delete WinCryptSvc32`. "
        "Belt-and-suspenders: also delete the registry key directly — "
        "`Remove-Item -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\WinCryptSvc32' "
        "-Recurse -Force -ErrorAction SilentlyContinue`.",
        "Verify removal: `sc.exe query WinCryptSvc32` should exit with error 1060 "
        "(ERROR_SERVICE_DOES_NOT_EXIST), and the SD scan loop above should return no results. "
        "If the service reappears within 5 minutes a second persistence mechanism is still active.",
    ],
    artifacts=[
        Artifact(
            dynamic=True,
            description=(
                "Injector staged to C:\\Windows\\Temp\\spoolmon_<8-char session ID>.exe "
                "— uploaded immediately before shellcode injection, self-executes then exits"
            ),
        ),
        Artifact(
            dynamic=True,
            description=(
                "Raw Sliver shellcode blob staged to "
                "C:\\Windows\\Temp\\shellcode_<8-char session ID>.bin "
                "— passed as argv[2] to spoolmon.exe, injected into target process via "
                "VirtualAllocEx + WriteProcessMemory + CreateRemoteThread"
            ),
        ),
        Artifact(
            path="WinCryptSvc32",
            description=(
                "Windows service with SYSTEM-only security descriptor "
                "(SDDL D:(A;;...;;;SY)) registered under svchost.exe -k netsvcs; "
                "invisible to sc query / Services snap-in for non-SYSTEM callers"
            ),
        ),
        Artifact(
            path=r"HKLM:\SYSTEM\CurrentControlSet\Services\WinCryptSvc32",
            description=(
                "Registry key backing WinCryptSvc32; may carry Deny ACEs added by "
                "immutable-protection hook that block sc.exe delete — must be stripped "
                "before the service can be removed"
            ),
        ),
    ],
)
