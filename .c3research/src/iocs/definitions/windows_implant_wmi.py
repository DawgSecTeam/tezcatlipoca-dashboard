from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import WMIRootDefault

windows_implant_wmi = IOC(
    name="windows_implant_wmi",
    description="Sliver implant persisted via WMI subscription in root\\default namespace",
    category="remote_access",
    difficulty=2,
    os="windows",
    check=SliverCheck(preferred_type="beacon"),
    implant_spec=ImplantSpec(is_beacon=True, persistence_name="WMIRootDefault"),
    persistence=WMIRootDefault,
    payloads=["msupdate.exe"],
    discovery="Check root\\default for __EventFilter; check Sliver sessions",
    detection_steps=[
        "Query the WMI root\\default namespace for event filter subscriptions: "
        "`Get-CimInstance -Namespace 'root\\default' -ClassName __EventFilter` in PowerShell. "
        "Any result — in particular a filter named 'WinSecurityMonitor' — indicates the "
        "persistence is active. The filter's WQL query polls "
        "`Win32_LocalTime` every 60 seconds via a `__InstanceModificationEvent WITHIN 60` "
        "trigger.",
        "Confirm the CIMOM registry key has abnormal DENY ACEs that block repository writes: "
        "`(Get-Acl 'HKLM:\\SOFTWARE\\Microsoft\\Wbem\\CIMOM').Access | "
        "Where-Object { $_.AccessControlType -eq 'Deny' }`. "
        "Non-empty output means immutable protection was applied to the WMI repository key, "
        "which will cause standard WMI deletion tools to fail until the ACEs are stripped.",
        "Check the box's Sliver beacon status in C3rberus — a beacon from this host with "
        "a call-home interval around 300 seconds (5 minutes) confirms the implant is "
        "beaconing. The implant binary is deployed to a directory drawn from the Windows "
        "deploy-path pool (default candidates: C:\\Windows\\Temp, C:\\ProgramData\\Microsoft, "
        "C:\\Users\\Public, C:\\Windows\\SysWOW64, "
        "C:\\Windows\\System32\\spool\\drivers\\color).",
        "Search those directories for an unexpected .exe file: "
        "`Get-ChildItem -Path C:\\Windows\\Temp,C:\\ProgramData\\Microsoft,"
        "C:\\Users\\Public -Filter *.exe -ErrorAction SilentlyContinue`. "
        "The filename is chosen from the operator's naming pool at deploy time "
        "and is not predictable in advance; msupdate.exe is the reference payload name.",
    ],
    remediation_steps=[
        "Before removing the WMI EventFilter, strip any DENY ACEs from the CIMOM registry "
        "key — otherwise WMI repository writes will fail with access-denied errors: "
        "open PowerShell as Administrator and run "
        "`$rk = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey("
        "'SOFTWARE\\\\Microsoft\\\\Wbem\\\\CIMOM', "
        "[Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree, "
        "[System.Security.AccessControl.RegistryRights]::ChangePermissions "
        "-bor [System.Security.AccessControl.RegistryRights]::ReadPermissions); "
        "$sec = $rk.GetAccessControl(); "
        "$sec.Access | Where-Object { $_.AccessControlType -eq 'Deny' } | "
        "ForEach-Object { $sec.RemoveAccessRule($_) }; "
        "$rk.SetAccessControl($sec); $rk.Close()`.",
        "Delete all __EventFilter objects from the root\\default namespace: "
        "`Get-WmiObject -Namespace 'root\\default' -Class __EventFilter | "
        "ForEach-Object { $_.Delete() }`. "
        "Verify with `Get-CimInstance -Namespace 'root\\default' "
        "-ClassName __EventFilter` — the result should be empty.",
        "Locate and terminate the Sliver beacon process. Search the deploy-path pool "
        "directories for the implant binary "
        "(`Get-ChildItem C:\\Windows\\Temp,C:\\ProgramData\\Microsoft,"
        "C:\\Users\\Public -Filter *.exe`) and kill the matching process: "
        "`Stop-Process -Name <implant_name> -Force -ErrorAction SilentlyContinue`.",
        "Delete the implant binary from whichever deploy-path directory it landed in: "
        "`Remove-Item -Path <full_path> -Force`. "
        "If the file is marked read-only, run `attrib -R <full_path>` first.",
        "Wait one WMI poll interval (up to 60 seconds) then re-run "
        "`Get-CimInstance -Namespace 'root\\default' -ClassName __EventFilter`. "
        "If the filter reappears, the CIMOM ACE removal step was incomplete — "
        "repeat the ACE-stripping step and delete again. "
        "Also verify the beacon no longer appears in C3rberus.",
    ],
    artifacts=[
        Artifact(
            path="root\\default:__EventFilter[Name='WinSecurityMonitor']",
            description="WMI EventFilter in root\\default namespace; WQL polls "
            "Win32_LocalTime every 60 seconds via __InstanceModificationEvent WITHIN 60; "
            "created by wmi_root_default_deploy.ps1",
        ),
        Artifact(
            path="HKLM:\\SOFTWARE\\Microsoft\\Wbem\\CIMOM",
            description="WMI CIMOM registry key; DENY ACEs added when immutable protection "
            "is applied, blocking EventFilter deletion until ACEs are stripped manually",
        ),
        Artifact(
            dynamic=True,
            description="Sliver beacon .exe dropped to a directory drawn from the Windows "
            "deploy-path pool at deploy time (default candidates: C:\\Windows\\Temp, "
            "C:\\ProgramData\\Microsoft, C:\\Users\\Public, C:\\Windows\\SysWOW64, "
            "C:\\Windows\\System32\\spool\\drivers\\color); filename chosen from the "
            "operator naming pool — msupdate.exe is the reference payload name",
        ),
    ],
)
