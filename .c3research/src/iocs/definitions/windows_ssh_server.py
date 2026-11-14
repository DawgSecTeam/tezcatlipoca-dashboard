from iocs.base import IOC, Artifact, ScriptCheck

windows_ssh_server = IOC(
    name="windows_ssh_server",
    description="SSH server running on Windows with pre-planted keys",
    category="remote_access",
    difficulty=2,
    os="windows",
    # Disabled until a range gets a WSUS pointer or a mounted Windows Server
    # FeaturesOnDemand ISO — every current CDE Windows box (Server 2019/2022
    # Standard, no internet, no WSUS) fails `Add-WindowsCapability -Online
    # -Name OpenSSH.Server~~~~0.0.1.0` with CBS HRESULT=0x80070005 (source
    # unavailable), which cascades into `Set-Service sshd → "The specified
    # service does not exist as an installed service"`. Files kept so the
    # IOC can be re-enabled by clearing this flag once a source is wired.
    disabled=True,
    check=ScriptCheck(script="check_scripts/windows/windows_ssh_server_check.ps1"),
    discovery="Check firewall, running services, around the file system",
    deploy_script="deploy_scripts/windows/windows_ssh_server_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_ssh_server_remediate.ps1",
    sa_highlight={"firewall_rules": ["OpenSSH-Server-In-TCP"]},
    detection_steps=[
        "Check whether the OpenSSH Server Windows Capability is installed: "
        "`Get-WindowsCapability -Online -Name 'OpenSSH.Server*'` — a State of `Installed` means "
        "the feature was added.",
        "Check the sshd service status: `Get-Service -Name sshd | Select-Object Name,Status,StartType`. "
        "A running service with `StartType=Automatic` is the planted configuration.",
        "Verify the inbound firewall rule is enabled: "
        "`Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' | Select-Object Name,Enabled,Direction,Action`. "
        "An `Enabled=True` entry on port 22 confirms the attacker opened ingress.",
        "Check for the planted attacker public key: "
        "`Test-Path 'C:\\Users\\Administrator\\.ssh\\id_rsa.pub'` and then "
        "`Get-Content 'C:\\Users\\Administrator\\.ssh\\id_rsa.pub'`. "
        "The red-team key contains the comment `backup-svc@internal-ops`.",
    ],
    remediation_steps=[
        "Stop and disable the SSH daemon: "
        "`Stop-Service -Name sshd -Force; Set-Service -Name sshd -StartupType Disabled`.",
        "Disable the inbound firewall rule: "
        "`Disable-NetFirewallRule -Name 'OpenSSH-Server-In-TCP'`.",
        "Remove the planted attacker public key: "
        "`Remove-Item -Path 'C:\\Users\\Administrator\\.ssh\\id_rsa.pub' -Force`.",
        "Verify remediation: re-run `Get-Service -Name sshd` (should be Stopped/Disabled) and "
        "`Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP'` (Enabled should be False). "
        "The IOC check passes only when all three conditions are cleared simultaneously.",
    ],
    artifacts=[
        Artifact(
            path="C:\\Users\\Administrator\\.ssh\\id_rsa.pub",
            description="Planted attacker public key; comment field reads `backup-svc@internal-ops`",
        ),
        Artifact(
            path="OpenSSH-Server-In-TCP",
            description="Windows Firewall inbound rule allowing TCP/22; enabled by deploy script",
        ),
        Artifact(
            path="sshd",
            description="OpenSSH Server Windows service; set to Automatic startup by deploy script",
        ),
        Artifact(
            path="OpenSSH.Server~~~~0.0.1.0",
            description="Windows Optional Capability installed to provide the sshd binary",
        ),
    ],
)
