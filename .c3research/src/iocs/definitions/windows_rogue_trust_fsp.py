from iocs.base import IOC, Artifact, ScriptCheck

windows_rogue_trust_fsp = IOC(
    name="windows_rogue_trust_fsp",
    description=(
        "Detects the ForeignSecurityPrincipal (FSP) object in "
        "CN=ForeignSecurityPrincipals whose Name is the rogue-DC svc_dfsr "
        "SID (S-1-5-21-2621662996-3783546519-2219607937-1119). Windows "
        "auto-creates this FSP when svc_dfsr is added to BUILTIN\\Administrators "
        "via the <SID=X> alt-DN syntax (see rogue_dc_admin_persist.py). "
        "Deleting the FSP breaks the local nesting into Administrators; "
        "unless the trust is also torn down, an operator can re-nest and "
        "regenerate the FSP on the next Set-ADGroup call."
    ),
    category="users",
    difficulty=3,
    os="windows",
    requires_dc=True,
    pre_deployed=True,
    deploy_script=None,
    remediate_script=None,
    check=ScriptCheck(
        script="check_scripts/windows/windows_rogue_trust_fsp_check.ps1",
    ),
    discovery=(
        "On the DC: `Get-ADObject -Identity "
        "'CN=S-1-5-21-2621662996-3783546519-2219607937-1119,CN=ForeignSecurityPrincipals,'"
        "+(Get-ADDomain).DistinguishedName`"
    ),
    detection_steps=[
        (
            "On the DC, look up the FSP by DN: "
            "`Import-Module ActiveDirectory; $dn = (Get-ADDomain).DistinguishedName; "
            'Get-ADObject -Identity "CN=S-1-5-21-2621662996-3783546519-2219607937-1119,'
            'CN=ForeignSecurityPrincipals,$dn"`.'
        ),
        (
            "IOC present when the object exists (returns the AD entry). Absent "
            "queries throw ADIdentityNotFoundException — check the exception "
            "class, not the string, so the script does not false-positive on an "
            "unrelated LDAP error."
        ),
        (
            "Cross-check what still references the FSP before deleting: any group "
            "whose `member` attribute contains the SID, and any ACL granting the "
            "SID rights. Common finding: it is a direct member of BUILTIN\\"
            "Administrators (planted by rogue_dc_admin_persist.py step 1). "
            "`Get-ADGroup -Identity 'CN=Administrators,CN=Builtin,'+$dn -Properties member | "
            "Select-Object -ExpandProperty member`."
        ),
    ],
    remediation_steps=[
        (
            "First remove any group memberships that reference the SID — otherwise "
            "the next Set-ADGroup / Set-Acl call touching that SID re-creates the "
            "FSP. Typical: "
            "`Set-ADGroup -Identity 'CN=Administrators,CN=Builtin,'+((Get-ADDomain).DistinguishedName) "
            "-Remove @{member='<SID=S-1-5-21-2621662996-3783546519-2219607937-1119>'}`."
        ),
        (
            "Then delete the FSP itself: "
            "`$dn = (Get-ADDomain).DistinguishedName; "
            'Remove-ADObject -Identity "CN=S-1-5-21-2621662996-3783546519-2219607937-1119,'
            'CN=ForeignSecurityPrincipals,$dn" -Confirm:$false`.'
        ),
        (
            "Verify: re-run this check — it should score status=0. Also confirm "
            "svc_dfsr no longer authenticates with admin power: "
            "`nxc smb 127.0.0.1 -u svc_dfsr -p '<pw>' -d mira.corp.sus --shares` "
            "should NOT show ADMIN$/C$ as READ,WRITE any more."
        ),
    ],
    artifacts=[
        Artifact(
            path="CN=S-1-5-21-2621662996-3783546519-2219607937-1119,CN=ForeignSecurityPrincipals",
            description=(
                "ForeignSecurityPrincipal object for the rogue-DC svc_dfsr SID. "
                "Auto-created by Windows when the SID was first referenced in a "
                "cross-domain member entry (Set-ADGroup with the <SID=X> alt-DN "
                "syntax) on BUILTIN\\Administrators."
            ),
        ),
    ],
)
