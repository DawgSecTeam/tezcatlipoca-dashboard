from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck

windows_implant_bare = IOC(
    name="windows_implant_bare",
    description="Sliver implant running as standalone binary only",
    category="remote_access",
    difficulty=1,
    os="windows",
    check=SliverCheck(),
    implant_spec=ImplantSpec(is_beacon=False),
    payloads=["msupdate.exe"],
    discovery="Check running processes for unsigned exe in %TEMP% or %APPDATA%",
    detection_steps=[
        "List processes whose executable path falls under a known drop directory: "
        "`Get-Process | Where-Object {$_.Path} | Select-Object Name,Id,Path "
        "| Where-Object {$_.Path -match 'Windows\\\\Temp|ProgramData\\\\Microsoft"
        "|Users\\\\Public|SysWOW64|spool\\\\drivers\\\\color'}`. "
        "An unsigned PE in any of those directories that is not a known Windows "
        "component is the implant.",
        "Scan each candidate drop directory for recently written executables: "
        "`Get-ChildItem 'C:\\Windows\\Temp','C:\\ProgramData\\Microsoft',"
        "'C:\\Users\\Public','C:\\Windows\\SysWOW64',"
        "'C:\\Windows\\System32\\spool\\drivers\\color' -Filter *.exe "
        "-ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending "
        "| Select-Object FullName,LastWriteTime,Length`. "
        "The implant binary is deployed with a Sliver-generated filename of the form "
        "ADJECTIVE_NOUN_session_windows_amd64.exe (e.g. SILENT_BEAST_session_windows_amd64.exe). "
        "It will appear in one of these directories with a recent write time.",
        "Verify the binary is not Windows-signed: "
        "`Get-AuthenticodeSignature '<full_path_from_above>'`. "
        "A legitimate Windows executable will show 'Valid' with a Microsoft "
        "certificate; the implant will show 'NotSigned' or 'UnknownError'.",
        "Confirm via the C3rberus Sliver session list that an active session "
        "originates from this box. The IOC is scored by live session presence; "
        "no persistence mechanism is installed, so the process is the sole artifact.",
    ],
    remediation_steps=[
        "Identify the implant process name and PID from the detection step, "
        "then kill it: `Stop-Process -Name '<process_name>' -Force "
        "-ErrorAction SilentlyContinue`. "
        "Alternatively, kill by full path: "
        "`Get-Process | Where-Object {$_.Path -eq '<full_path>'} "
        "| Stop-Process -Force -ErrorAction SilentlyContinue`.",
        "Delete the implant binary from the drop directory: "
        "`Remove-Item -Force '<full_path>' -ErrorAction SilentlyContinue`. "
        "Check all five candidate directories if you are unsure of the exact path: "
        "`'C:\\Windows\\Temp','C:\\ProgramData\\Microsoft','C:\\Users\\Public',"
        "'C:\\Windows\\SysWOW64','C:\\Windows\\System32\\spool\\drivers\\color'`.",
        "Confirm the process is gone: `Get-Process -Name '<process_name>' "
        "-ErrorAction SilentlyContinue` should return nothing. "
        "Confirm the file is removed: `Test-Path '<full_path>'` should return False.",
        "Verify the Sliver session has dropped from the C3rberus session list. "
        "This IOC has no persistence — there is no scheduled task, service, or "
        "registry run key to clean up. Killing the process and removing the binary "
        "is sufficient for full remediation.",
    ],
    artifacts=[
        Artifact(
            dynamic=True,
            description=(
                "Sliver session EXE with a Sliver-generated filename of the form "
                "ADJECTIVE_NOUN_session_windows_amd64.exe dropped into one of the five "
                "pool directories: C:\\Windows\\Temp, C:\\ProgramData\\Microsoft, "
                "C:\\Users\\Public, C:\\Windows\\SysWOW64, or "
                "C:\\Windows\\System32\\spool\\drivers\\color. "
                "Exact filename and directory are chosen at deploy time."
            ),
        ),
    ],
)
