from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import ServiceSD

windows_implant_hidden_service = IOC(
    name="windows_implant_hidden_service",
    description="Sliver implant persisted via SYSTEM-only SD service (pooled service name)",
    category="remote_access",
    difficulty=2,
    os="windows",
    check=SliverCheck(preferred_type="beacon"),
    implant_spec=ImplantSpec(is_beacon=True, persistence_name="ServiceSD"),
    persistence=ServiceSD,
    # SVC_NAME used by service_sd_deploy.ps1 to name the SYSTEM-only SD service.
    pooled_names=[("SVC_NAME", "service")],
    payloads=["msupdate.exe"],
    discovery="Scan service SDs for SYSTEM-only ACL; check Sliver sessions",
    detection_steps=[
        "Confirm the service exists from SYSTEM context (NXC smbexec runs as "
        "LocalSystem = SYSTEM): `sc.exe query WinCryptSvc32`. Both `sc query "
        "type= all` and `sc query <name>` will FAIL for non-SYSTEM callers "
        "because the SYSTEM-only security descriptor blocks all SCM access "
        "to this service entry for non-SYSTEM principals.",
        "Enumerate service security descriptors as SYSTEM (or in an elevated "
        "PowerShell session via NXC smbexec) to spot SYSTEM-only ACLs: "
        "`Get-CimInstance Win32_Service | ForEach-Object { $n = $_.Name; "
        "$s = sc.exe sdshow $n 2>$null; "
        "if ($s -notmatch 'A;;[^;]*;;;BA' -and $s -notmatch 'A;;[^;]*;;;AU' "
        "-and $s -match 'A;;[^;]*;;;SY') { \"$n: $s\" } }`. "
        "Any hit is a SYSTEM-only SD service.",
        "Confirm the service details and its SDDL: `sc.exe sdshow WinCryptSvc32`. "
        "The planted SDDL is `D:(A;;CCLCSWRPWPDTLOCRSDRCWDWO;;;SY)` — the lone "
        "ACE grants only SYSTEM (SY); no BA or AU entries are present.",
        "Check for an active Sliver beacon in the C3rberus C2 panel (Targets page) "
        "or via `Get-CimInstance Win32_Process | Where-Object { $_.Name -like "
        "'msupdate.exe' }` — the beacon binary is the actual C2 implant calling "
        "home. Note: the WinCryptSvc32 service uses `svchost.exe -k netsvcs` as "
        "its binary path; it does NOT directly launch the beacon. The service "
        "artifact provides the SYSTEM-only SD hiding mechanism; the beacon process "
        "runs independently (bare upload and execute).",
    ],
    remediation_steps=[
        "Stop and delete the hidden service (requires SYSTEM or equivalent; "
        "NXC smbexec runs as LocalSystem and can bypass the SYSTEM-only SD): "
        "`sc.exe stop WinCryptSvc32` then `sc.exe delete WinCryptSvc32`.",
        "If `sc.exe delete` fails (registry key may have Deny ACEs added by the "
        "immutable-protection hook), remove Deny ACEs from the registry key first: "
        "open `HKLM:\\SYSTEM\\CurrentControlSet\\Services\\WinCryptSvc32` via "
        "`regedit` or PowerShell `.NET` registry API, strip all Deny rules, then "
        "retry the delete.",
        "Belt-and-suspenders: delete the registry key directly to ensure the SCM "
        "entry is gone even if `sc.exe delete` returned an error: "
        "`Remove-Item -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\WinCryptSvc32' "
        "-Recurse -Force`.",
        "Terminate the Sliver beacon process if it is running: "
        "`Stop-Process -Name msupdate -Force -ErrorAction SilentlyContinue`. "
        "Then locate and delete the dropped beacon binary (default staging path is "
        "`C:\\Windows\\Temp\\msupdate.exe`; path may vary if the operator used a "
        "custom deploy pool).",
        "Verify remediation: `sc.exe query WinCryptSvc32` must return exit code "
        "1060 (ERROR_SERVICE_DOES_NOT_EXIST), and the Sliver beacon must no longer "
        "appear in the C3rberus Targets page.",
    ],
    artifacts=[
        Artifact(
            path="WinCryptSvc32",
            description="Windows service with SYSTEM-only SD (D:(A;;CCLCSWRPWPDTLOCRSDRCWDWO;;;SY)); "
            "invisible to sc query for non-SYSTEM callers; display name 'Windows Cryptography Helper'",
        ),
        Artifact(
            path=r"HKLM:\SYSTEM\CurrentControlSet\Services\WinCryptSvc32",
            description="Service registry key; may carry Deny ACEs added by immutable-protection hook",
        ),
        Artifact(
            dynamic=True,
            description="Sliver beacon binary (msupdate.exe) dropped to a configurable staging "
            "directory (default C:\\Windows\\Temp\\msupdate.exe; operator may use a custom "
            "DEPLOY_PATH_POOL_WINDOWS path)",
        ),
    ],
)
