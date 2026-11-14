from iocs.base import IOC, Artifact, ScriptCheck

windows_hide_library = IOC(
    name="windows_hide_library",
    description=(
        "AppCertDLLs hiding DLL that patches 13 functions via inline 64-bit trampolines "
        "(12-byte mov rax/jmp rax) in every new process spawned on the box. Hooks "
        "NtQuerySystemInformation (hide processes), NtQueryDirectoryFile/Ex (hide files), "
        "GetTcpTable/2/GetTcp6Table/2/GetUdpTable/GetUdp6Table (hide TCP/UDP connections), "
        "EnumServicesStatusExW/A (hide services), NetUserEnum (hide accounts), and "
        "NtSetValueKey (validate XOR config blob writes). Config stored as REG_BINARY at "
        "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Perflib\\009\\LastSync. "
        "Activated by an AppCertDLLs registry value under Session Manager; Windows "
        "kernel32 loads it into every CreateProcess-spawned process automatically. "
        "Teaches defenders that tasklist, netstat, sc query, net user, dir, and Get-* "
        "PowerShell cmdlets are not authoritative when AppCertDLLs is hijacked, and "
        "that REG_BINARY blobs in Perflib\\009 can be covert config channels."
    ),
    category="impair_defenses",
    difficulty=4,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_hide_library_check.ps1"),
    deploy_script="deploy_scripts/windows/windows_hide_library_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_hide_library_remediate.ps1",
    payloads=["hide_windows.dll"],
    # C3R_DLL_NAME = pool-driven DLL filename (scored)
    # C3R_CERTDLL_VALUE = pool-driven AppCertDLLs value name (scored)
    # C3R_DROP_DIR = pool-driven drop directory (scored)
    pooled_names=[("DLL_NAME", "dll"), ("CERTDLL_VALUE", "certdll_value")],
    discovery=(
        "reg query \"HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\AppCertDLLs\"; "
        "Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Perflib\\009' -Name LastSync; "
        "Get-ChildItem <drop_dir>\\<dll_name>; "
        "Get-Process | Where-Object {$_.Modules -match '<dll_name>'}"
    ),
    detection_steps=[
        "Check AppCertDLLs registry key for foreign entries: "
        "`reg query \\\"HKLM\\\\SYSTEM\\\\CurrentControlSet\\\\Control\\\\Session Manager\\\\AppCertDLLs\\\"`. "
        "Any entry pointing to a non-Microsoft DLL is the IOC.",
        "Verify the DLL exists on disk at the registered path: "
        "`Get-Item -Path <dll_path>`. Check file timestamp and digital signature.",
        "Check for covert config blob: "
        "`Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Perflib\\009' -Name LastSync`. "
        "A REG_BINARY value here that is not a standard Perflib counter is the config.",
        "Verify hiding is active: compare `tasklist` output in a child process started "
        "after the DLL load vs a process started before (or in a VM snapshot pre-IOC).",
        "Enumerate loaded modules for the DLL name: "
        "`Get-Process | ForEach-Object { $_.Modules } | Where-Object { $_.FileName -like '*<dll_name>*' }`.",
    ],
    remediation_steps=[
        "Set bypass env var before any cleanup: "
        "`[Environment]::SetEnvironmentVariable('_SYS_AUDIT_TOKEN', "
        "'a3f2e891-7c4b-4d9a-b5e6-1f2a3c4d5e6f', 'Machine')`.",
        "Remove the AppCertDLLs value: "
        "`Remove-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\AppCertDLLs' "
        "-Name '<certdll_value>'`.",
        "Remove the DLL from disk: `Remove-Item -Path '<drop_dir>\\<dll_name>' -Force`.",
        "Remove the config blob: "
        "`Remove-ItemProperty -Path "
        "'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Perflib\\009' -Name LastSync`.",
        "Reboot or restart critical processes — DLL hooks persist in already-running "
        "processes (loaded at CreateProcess time) until process restart.",
    ],
    artifacts=[
        Artifact(
            dynamic=True,
            description="Pool-driven DLL dropped to C3R_DROP_DIR\\C3R_DLL_NAME; "
            "patches 13 functions via inline trampolines in all new processes",
        ),
        Artifact(
            dynamic=True,
            description="AppCertDLLs registry value C3R_CERTDLL_VALUE under "
            "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\AppCertDLLs "
            "pointing to the DLL path",
        ),
        Artifact(
            path=r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Perflib\009\LastSync",
            description="REG_BINARY config blob; XOR-encrypted hide entries; "
            "NtSetValueKey hook validates writes to prevent accidental overwrite",
        ),
    ],
    sa_highlight={"services": ["WinCertStore", "SecurityBaseProvider"]},
)
