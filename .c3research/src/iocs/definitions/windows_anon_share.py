from iocs.base import IOC, Artifact, ScriptCheck

windows_anon_share = IOC(
    name="windows_anon_share",
    description="Folder shared with anonymous full access",
    category="exfiltration",
    difficulty=2,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_anon_share_check.ps1"),
    discovery="Check file explorer, server manager",
    deploy_script="deploy_scripts/windows/windows_anon_share_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_anon_share_remediate.ps1",
    detection_steps=[
        "List all SMB shares and look for a share named exactly `C` (not the built-in `C$` admin "
        "share): `Get-SmbShare | Where-Object { $_.Name -eq 'C' }`. A result means the IOC share "
        "is present.",
        "Check the anonymous-access registry value: "
        "`Get-ItemPropertyValue 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Lsa' "
        "EveryoneIncludesAnonymous`. A value of `1` means anonymous users are treated as Everyone, "
        "which grants unauthenticated read/write to the `C` share.",
        "Confirm the share path and permissions: "
        "`Get-SmbShare -Name C | Select-Object Name,Path,Description` and "
        "`Get-SmbShareAccess -Name C`. The deploy gives `ChangeAccess` to `Everyone`, so anonymous "
        "clients can enumerate and modify the entire C:\\ drive.",
        "The IOC requires BOTH conditions simultaneously — the `C` share existing AND the registry "
        "value equal to `1`. Either alone is insufficient; the check script gates on both.",
    ],
    remediation_steps=[
        "Reset the LSA registry value to deny anonymous access: "
        "`Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Lsa' "
        "-Name EveryoneIncludesAnonymous -Value 0`.",
        "Remove the malicious `C` share (this does NOT touch the built-in `C$` admin share): "
        "`Remove-SmbShare -Name C -Force`.",
        "Verify both changes: `Get-SmbShare -Name C` should return nothing, and "
        "`Get-ItemPropertyValue 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Lsa' "
        "EveryoneIncludesAnonymous` should return `0`.",
        "This IOC has no persistence mechanism — removing the share and resetting the registry "
        "value is sufficient. No reboot is required for the SMB share removal to take effect.",
    ],
    artifacts=[
        Artifact(
            path="HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Lsa\\EveryoneIncludesAnonymous",
            description="DWORD set to 1; grants anonymous users the Everyone group token",
        ),
        Artifact(
            path="C",
            description="SMB share named 'C' (distinct from built-in C$) rooted at C:\\ "
            "with ChangeAccess granted to Everyone",
        ),
    ],
)
