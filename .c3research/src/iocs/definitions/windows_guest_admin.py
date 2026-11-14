from iocs.base import IOC, Artifact, ScriptCheck

windows_guest_admin = IOC(
    name="windows_guest_admin",
    description="Guest account elevated to admin",
    category="users",
    difficulty=1,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_guest_admin_check.ps1"),
    discovery="Check what users are within each group",
    deploy_script="deploy_scripts/windows/windows_guest_admin_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_guest_admin_remediate.ps1",
    detection_steps=[
        "On a workstation or member server, list the local Administrators group: "
        "`Get-LocalGroupMember -Group Administrators | Select-Object Name, ObjectClass`. "
        "Look for an entry whose Name ends in `\\Guest`.",
        "On a Domain Controller (ProductType 2), query the Domain Admins group: "
        "`Get-ADGroupMember -Identity 'Domain Admins' -Recursive | "
        "Where-Object { $_.SamAccountName -eq 'Guest' }`. "
        "Any result confirms the IOC is present.",
        "Verify the Guest account is enabled (deploy script calls `Enable-LocalUser`): "
        "`Get-LocalUser -Name Guest | Select-Object Name, Enabled`. "
        "An enabled Guest in Administrators is the full IOC footprint.",
        "Confirm the product type to use the right check path: "
        "`(Get-CimInstance Win32_OperatingSystem).ProductType` — "
        "`1` = workstation, `2` = DC, `3` = member server.",
    ],
    remediation_steps=[
        "On a workstation or member server, remove Guest from the local Administrators group: "
        "`Remove-LocalGroupMember -Group Administrators -Member Guest`.",
        "On a Domain Controller, remove Guest from Domain Admins: "
        "`Remove-ADGroupMember -Identity 'Domain Admins' -Members Guest -Confirm:$false`.",
        "Disable the Guest account to restore its default locked-out state: "
        "`Disable-LocalUser -Name Guest`.",
        "Verify remediation: re-run `Get-LocalGroupMember -Group Administrators` "
        "(workstation/server) or the `Get-ADGroupMember` query (DC) and confirm Guest "
        "no longer appears in the output.",
    ],
    artifacts=[
        Artifact(
            path="Guest",
            description="Built-in Guest account enabled and added to Administrators "
            "(workstation/member server) or Domain Admins (DC); no files dropped",
        ),
        Artifact(
            path="Administrators",
            description="Local Administrators group (workstation/member server) — "
            "Guest added as member",
        ),
        Artifact(
            path="Domain Admins",
            description="Active Directory Domain Admins group (DC only) — "
            "Guest added as member when box is a domain controller",
        ),
    ],
)
