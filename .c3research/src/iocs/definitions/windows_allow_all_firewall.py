from iocs.base import IOC, Artifact, ScriptCheck

windows_allow_all_firewall = IOC(
    name="windows_allow_all_firewall",
    description="Firewall rule allowing traffic on all ports",
    category="impair_defenses",
    difficulty=1,
    os="windows",
    check=ScriptCheck(
        script="check_scripts/windows/windows_allow_all_firewall_check.ps1"
    ),
    discovery="Check firewall",
    deploy_script="deploy_scripts/windows/windows_allow_all_firewall_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_allow_all_firewall_remediate.ps1",
    sa_highlight={"firewall_rules": ["Strict Rule"]},
    detection_steps=[
        "List all Windows Firewall rules named 'Strict Rule': "
        "`Get-NetFirewallRule -DisplayName 'Strict Rule'`. "
        "Two rules (Inbound and Outbound) will be present if the IOC is active.",
        "Confirm each rule permits all traffic: "
        "`Get-NetFirewallRule -DisplayName 'Strict Rule' | Get-NetFirewallPortFilter` — "
        "Protocol will be 'Any', and LocalPort/RemotePort will be 'Any'.",
        "Verify both rules are enabled and apply to all profiles: "
        "`Get-NetFirewallRule -DisplayName 'Strict Rule' | Select-Object DisplayName,Direction,Action,Enabled,Profile` "
        "— Enabled should be 'True' and Profile 'Any'.",
        "Cross-check via the SA panel's 'firewall_rules' tab — the 'Strict Rule' entry is "
        "highlighted there if SA collection has run since deployment.",
    ],
    remediation_steps=[
        "Remove both 'Strict Rule' firewall rules (inbound and outbound) in a single command: "
        "`Remove-NetFirewallRule -DisplayName 'Strict Rule'`.",
        "Confirm removal — re-running `Get-NetFirewallRule -DisplayName 'Strict Rule'` "
        "should return no results (or a 'No MSFT_NetFirewallRule objects found' error).",
        "Verify the Windows Firewall service is still running and the default profile "
        "policies are restrictive: `Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction`.",
    ],
    artifacts=[
        Artifact(
            path="Strict Rule",
            description="Inbound Windows Firewall rule (NetSecurity) allowing all protocols on all "
            "ports across all profiles; created by New-NetFirewallRule",
        ),
        Artifact(
            path="Strict Rule",
            description="Outbound Windows Firewall rule (NetSecurity) allowing all protocols on all "
            "ports across all profiles; created by New-NetFirewallRule",
        ),
    ],
)
