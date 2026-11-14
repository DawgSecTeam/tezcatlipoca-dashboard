from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import WinInjectWMIRespawn

windows_implant_inject = IOC(
    name="windows_implant_inject",
    description="TCP-pivot session injected into LanmanServer's svchost via a svchost DLL service. "
    "Dual persistence: delayed-auto service re-injects on every boot; WMI event "
    "subscription restarts the injector whenever LanmanServer is restarted at runtime. "
    "Service DLL is file-immutable.",
    category="remote_access",
    difficulty=4,
    os="windows",
    check=SliverCheck(preferred_type="session"),
    sliver_inject=True,
    implant_spec=ImplantSpec(
        format="shellcode",
        # MUST be a session — Sliver pivots are session-only (BishopFox/sliver
        # wiki, Pivots). A beacon built against tcppivot:// never calls back and
        # busy-loops at ~99% CPU on the target. This IOC ran as a beacon until
        # 2026-08-09 and scored ZERO on every team for that reason.
        is_beacon=False,
        apply_immutable=True,
        immutable_scope="binary_only",
        pivot_set_name="devd_kqueue_pivot",
        jitter=20,
    ),
    backup_copy=True,
    deploy_script="deploy_scripts/windows/win_inject_svc_deploy.ps1",
    remediate_script="remediate_scripts/windows/win_inject_svc_remediate.ps1",
    persistence=WinInjectWMIRespawn,
    # SVC_NAME is POOL-SELECTED per box (C3R_SVC_NAME, injected by the pivot deploy
    # path). It controls: injector service name, svchost group key, WMI subscription
    # names, and DLL path (svc_name.lower() + ".dll"). The concrete names below
    # (WinLanSvc / winlansvc.dll) are REPRESENTATIVE examples — the actual planted
    # name varies per box and must be enumerated, not assumed.
    # WMI_NAME is drawn from the dedicated `wmi` pool, NOT reused from SVC_NAME.
    # The subscription objects and the service are different artifact kinds and
    # should not share a namespace: reusing SVC_NAME made the EventFilter,
    # Consumer and the service all one string, so a blue team that found any one
    # of them had the others for free, and it drew WMI names from a pool sized
    # for services. sc.exe start still uses SVC_NAME — only the subscription
    # object names move.
    pooled_names=[("SVC_NAME", "service"), ("WMI_NAME", "wmi")],
    timestomp=True,
    # {C3R_SVC_NAME_LOWER} expanded at deploy time (ioc_deploy._expanded_paths).
    # Deploy script: DLL = svc_name.ToLower() + ".dll" → must use _LOWER variant.
    timestomp_paths=[r"C:\Windows\System32\{C3R_SVC_NAME_LOWER}.dll"],
    payloads=[],
    discovery="The service name is POOL-SELECTED (enumerate it; do not assume a fixed name). "
    "Enumerate svchost-hosted services + their ServiceDll under System32; "
    "check WMI root\\subscription EventFilters/Consumers; "
    "check LanmanServer svchost memory for injected thread; check Sliver sessions. "
    "(Representative example: service WinLanSvc, DLL winlansvc.dll.)",
    detection_steps=[
        "NOTE: the injector service name is drawn from a name-pool and differs per "
        "box — the 'WinLanSvc' / 'winlansvc.dll' below are REPRESENTATIVE examples. "
        "Enumerate to find the real name: list shared-svchost services and their "
        "Parameters\\ServiceDll (`Get-CimInstance Win32_Service | Where-Object "
        "{$_.PathName -like '*svchost.exe -k*'}`), then inspect the one whose DLL "
        "lives in System32 with a DENY DACL / read-only attribute.",
        "Check whether the injector service exists: "
        "`sc.exe query WinLanSvc` or in PowerShell "
        "`Get-Service WinLanSvc -ErrorAction SilentlyContinue`. "
        "A result with DisplayName 'Windows LAN Session Manager' is the planted service.",
        "Verify the service DLL is present and protected: "
        "`Test-Path C:\\Windows\\System32\\winlansvc.dll` should return True. "
        "Confirm the read-only attribute: `(Get-Item C:\\Windows\\System32\\winlansvc.dll).Attributes`. "
        "Confirm the DENY DACL: `icacls C:\\Windows\\System32\\winlansvc.dll` — look for "
        "'DENY Everyone:(W,D,DE,DC)'.",
        "Inspect the svchost group registry entry: "
        "`Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Svchost' "
        "-Name WinLanSvc -ErrorAction SilentlyContinue`. "
        "Also check the service registry keys under "
        "`HKLM:\\SYSTEM\\CurrentControlSet\\Services\\WinLanSvc` and its Parameters subkey "
        "for the ServiceDll value pointing to winlansvc.dll.",
        "Detect the WMI re-injection subscription: "
        "`Get-WmiObject -Namespace root\\subscription -Class __EventFilter "
        "-Filter \\\"Name='WinLanSvc'\\\"` and "
        "`Get-WmiObject -Namespace root\\subscription -Class CommandLineEventConsumer "
        "-Filter \\\"Name='WinLanSvc'\\\"`. "
        "Both objects present = WMI persistence active.",
        "Look for an active Sliver session injected into the LanmanServer svchost process: "
        "in Task Manager or `Get-Process svchost` find the svchost instance hosting "
        "LanmanServer, then inspect its loaded DLLs or network connections "
        "(`netstat -ano | findstr <PID>`) for unexpected outbound TCP connections.",
    ],
    remediation_steps=[
        "NOTE: the service/DLL/WMI-subscription name is pool-selected and differs "
        "per box — substitute the name you enumerated (see detection) for the "
        "representative 'WinLanSvc' / 'winlansvc.dll' shown below.",
        "Remove the attrib read-only flag and DENY DACL on the DLL before attempting deletion: "
        "`attrib -R C:\\Windows\\System32\\winlansvc.dll` then "
        "`icacls C:\\Windows\\System32\\winlansvc.dll /reset`. "
        "Without this step, Remove-Item will fail with an access-denied error.",
        "Stop and delete the injector service: "
        "`sc.exe stop WinLanSvc` followed by `sc.exe delete WinLanSvc`. "
        "Stopping it terminates the injected session thread in svchost.",
        "Remove the svchost group registry value: "
        "`Remove-ItemProperty -Path "
        "'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Svchost' "
        "-Name WinLanSvc -ErrorAction SilentlyContinue`.",
        "Delete the DLL payload: "
        "`Remove-Item C:\\Windows\\System32\\winlansvc.dll -Force -ErrorAction SilentlyContinue`.",
        "Remove the WMI re-injection subscription (all three objects in order): "
        "first delete the __FilterToConsumerBinding: "
        "`Get-WmiObject -Namespace root\\subscription -Class __FilterToConsumerBinding "
        "| Where-Object { $_.Filter -match 'WinLanSvc' } | Remove-WmiObject`; "
        "then the CommandLineEventConsumer and __EventFilter with Name='WinLanSvc'. "
        "Skipping any one object leaves the subscription partially intact.",
        "Verify: re-run `sc.exe query WinLanSvc`, "
        "`Test-Path C:\\Windows\\System32\\winlansvc.dll`, and "
        "`Get-WmiObject -Namespace root\\subscription -Class __EventFilter "
        "-Filter \\\"Name='WinLanSvc'\\\"` — all should return 'not found' / False / null. "
        "Also confirm the Sliver session no longer appears in C3rberus.",
    ],
    artifacts=[
        Artifact(
            path="C:\\Windows\\System32\\<pooled-svc-name>.dll",
            description="Shellcode-bearing DLL loaded by the injector svchost service "
            "(name pool-selected per box; e.g. winlansvc.dll); "
            "protected with attrib +R and DENY DACL for Everyone (W,D,DE,DC); "
            "timestomped to match a neighboring System32 file",
        ),
        Artifact(
            path="HKLM:\\SYSTEM\\CurrentControlSet\\Services\\WinLanSvc",
            description="Service registry key with DisplayName 'Windows LAN Session Manager', "
            "Type=0x20 (shared svchost), Start=2 (auto), DelayedAutostart=1; "
            "Parameters\\ServiceDll points to winlansvc.dll",
        ),
        Artifact(
            path="HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Svchost\\WinLanSvc",
            description="Svchost group MultiString value registering the WinLanSvc service "
            "so svchost.exe -k WinLanSvc is a valid host process",
        ),
        Artifact(
            path="root\\subscription:__EventFilter[Name='WinLanSvc']",
            description="WMI EventFilter watching for LanmanServer state transitions to "
            "'Running' (WQL WITHIN 5 seconds)",
        ),
        Artifact(
            path="root\\subscription:CommandLineEventConsumer[Name='WinLanSvc']",
            description="WMI consumer that fires 'sc.exe start WinLanSvc' via a hidden "
            "PowerShell process whenever LanmanServer is restarted",
        ),
        Artifact(
            path="root\\subscription:__FilterToConsumerBinding[WinLanSvc]",
            description="WMI binding linking the EventFilter to the CommandLineEventConsumer",
        ),
        Artifact(
            dynamic=True,
            description="Sliver session shellcode injected into the LanmanServer svchost.exe "
            "process; the binary payload is build-specific (UUID-named) and lives "
            "only in process memory after injection — no on-disk implant artifact",
        ),
    ],
)
