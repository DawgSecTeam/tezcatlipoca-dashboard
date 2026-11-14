from iocs.base import IOC, Artifact, ScriptCheck

windows_sftp_server = IOC(
    name="windows_sftp_server",
    description="SFTP server running as a service on Windows",
    category="exfiltration",
    difficulty=2,
    os="windows",
    # Disabled — shares [[project-ioc-fix-pass-2026-09-10]]'s OpenSSH-capability
    # blocker: the deploy tries `Add-WindowsCapability -Online OpenSSH.Server`
    # first (fails HRESULT=0x80070005 on WSUS-less/offline ranges), then falls
    # back to a PS TCP listener on port 2222 — but the fallback path still
    # emits "The specified service does not exist" errors from the residual
    # `Set-Service sshd` calls. Files kept for re-enable once a WSUS pointer
    # or FeaturesOnDemand ISO is available on the range.
    disabled=True,
    check=ScriptCheck(script="check_scripts/windows/windows_sftp_server_check.ps1"),
    discovery="Check for SFTP/SSH service; check port 22/2222 listeners",
    deploy_script="deploy_scripts/windows/windows_sftp_server_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_sftp_server_remediate.ps1",
    sa_highlight={"services": ["WinSshMgr"], "ports": [":2222"]},
    detection_steps=[
        "Check for the known SFTP/SSH service names: "
        "`Get-Service -Name WinSshMgr,sshd,WinSCP,FileZillaServer,SFTPService "
        "-ErrorAction SilentlyContinue | Select-Object Name,Status,StartType`. "
        "A Running `WinSshMgr`, `sshd`, `WinSCP`, `FileZillaServer`, or `SFTPService` "
        "service indicates this IOC.",
        "Confirm a TCP listener on port 22 or 2222: "
        '`netstat -ano | findstr ":22 \\|:2222 "` '
        "and cross-reference the PID with `tasklist` to identify the owning process.",
        "Inspect the `WinSshMgr` service binary path for the fallback variant: "
        "`sc.exe qc WinSshMgr`. If OpenSSH was unavailable at deploy time, the BINARY_PATH_NAME "
        "will point to "
        "`C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe "
        "-NonInteractive -ExecutionPolicy Bypass -File C:\\Windows\\Temp\\sshlistener.ps1`.",
        "Check for the TCP-listener helper script: "
        "`Test-Path C:\\Windows\\Temp\\sshlistener.ps1` (present only in the "
        "fallback / no-OpenSSH variant).",
        "Check the inbound firewall rules added by the IOC: "
        "`Get-NetFirewallRule -Name 'WinSshMgr-In-2222','OpenSSH-Server-In-TCP' "
        "-ErrorAction SilentlyContinue | Select-Object Name,Enabled,Action`.",
    ],
    remediation_steps=[
        "Stop and delete the `WinSshMgr` service: "
        "`Stop-Service WinSshMgr -Force -ErrorAction SilentlyContinue; "
        "sc.exe delete WinSshMgr`.",
        "If the built-in `sshd` service was enabled by this IOC, stop and disable it: "
        "`Stop-Service sshd -Force -ErrorAction SilentlyContinue; "
        "Set-Service sshd -StartupType Disabled`.",
        "Remove the firewall rules added by the IOC: "
        "`Remove-NetFirewallRule -Name 'WinSshMgr-In-2222' -ErrorAction SilentlyContinue; "
        "Remove-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue`.",
        "Delete the fallback listener script if present: "
        "`Remove-Item -Path C:\\Windows\\Temp\\sshlistener.ps1 -Force "
        "-ErrorAction SilentlyContinue`.",
        "Verify the port is no longer listening: "
        '`netstat -ano | findstr ":22 \\|:2222 "` should return no output. '
        "Also confirm neither `WinSshMgr` nor the IOC-enabled `sshd` appears in "
        "`Get-Service -ErrorAction SilentlyContinue | Where-Object Status -eq Running`.",
    ],
    artifacts=[
        Artifact(
            path="WinSshMgr",
            description="Windows service (sc.exe) registered by the IOC",
        ),
        Artifact(
            path="sshd",
            description="Built-in OpenSSH sshd service, enabled and started when "
            "OpenSSH capability is available on the target",
        ),
        Artifact(
            path="C:\\Windows\\Temp\\sshlistener.ps1",
            description="PowerShell TCP-listener "
            "script dropped as fallback when OpenSSH is not installed",
        ),
        Artifact(
            path="C:\\Windows\\System32\\OpenSSH\\sshd.exe",
            description="OpenSSH sshd binary; "
            "present only when the OpenSSH Server capability was already or newly installed",
        ),
        Artifact(
            path="OpenSSH-Server-In-TCP",
            description="Inbound firewall rule for port 22 "
            "(created when sshd path is used)",
        ),
        Artifact(
            path="WinSshMgr-In-2222",
            description="Inbound firewall rule for port 2222 "
            "(created in the fallback / no-OpenSSH variant)",
        ),
    ],
)
