from iocs.base import IOC, Artifact, ScriptCheck

windows_http_server = IOC(
    name="windows_http_server",
    description="Python HTTP server running as a service on Windows (WinDiagMgr)",
    category="exfiltration",
    difficulty=2,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_http_server_check.ps1"),
    discovery="Check for WinDiagMgr service; check port 8080/8000 listeners",
    deploy_script="deploy_scripts/windows/windows_http_server_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_http_server_remediate.ps1",
    # Service NAME pool-driven + scored — the check does Get-Service -Name $SVC_NAME
    # (docs/PLAN_build_check_deploy_name_pools.md §5B). No persistence.
    pooled_names=[("SERVICE_NAME", "service")],
    sa_highlight={
        "services": ["WinDiagMgr"],
        "processes": ["python", "python3", "powershell"],
        "ports": [":8080", ":8000"],
    },
    detection_steps=[
        "Check for the service by name: `Get-Service -Name WinDiagMgr` — status Running confirms"
        " the IOC is active; any non-absent result (Stopped, StartPending, etc.) still means the"
        " service object exists and the IOC is present.",
        "Inspect the service binary path to confirm the payload: "
        "`(Get-WmiObject Win32_Service -Filter \"Name='WinDiagMgr'\").PathName` — it will contain"
        " either a Python interpreter running `http.server 8080` or PowerShell executing"
        " `C:\\Windows\\Temp\\httplistener.ps1`.",
        "Confirm the listener is active on port 8080 (primary) or 8000 (fallback check):"
        ' `netstat -ano | findstr /R ":8080 :8000"` — the owning PID should resolve to a'
        " python.exe or powershell.exe process.",
        "Cross-reference the PID from netstat: `Get-Process -Id <PID>` — if the service is"
        " running, the process name will be `python` or `powershell`.",
        "Check for the fallback helper script (present only in the PowerShell path):"
        " `Test-Path C:\\Windows\\Temp\\httplistener.ps1`.",
    ],
    remediation_steps=[
        "Stop the service: `Stop-Service -Name WinDiagMgr -Force`.",
        "Delete the service: `sc.exe delete WinDiagMgr` — the IOC is NOT remediated until the"
        " service object is fully removed (stopping it alone is insufficient; the check script"
        " treats any existing service object as status=1).",
        "Remove the PowerShell fallback script if present:"
        " `Remove-Item -Path C:\\Windows\\Temp\\httplistener.ps1 -Force -ErrorAction SilentlyContinue`.",
        "Verify the service is gone: `Get-Service -Name WinDiagMgr -ErrorAction SilentlyContinue`"
        " should return nothing.",
        "Confirm port 8080 is no longer listening: `netstat -ano | findstr ':8080'` should return"
        " no output.",
    ],
    artifacts=[
        Artifact(
            path="HKLM:\\SYSTEM\\CurrentControlSet\\Services\\WinDiagMgr",
            description="Service registry key created by sc.exe; display name 'Windows Diagnostics Manager'",
        ),
        Artifact(
            path="C:\\Windows\\Temp\\httplistener.ps1",
            description="PowerShell HttpListener script; only present when Python is not found on the box",
        ),
    ],
)
