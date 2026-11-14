from iocs.base import IOC, Artifact, ScriptCheck

windows_rogue_trust_da_ace = IOC(
    name="windows_rogue_trust_da_ace",
    description=(
        "Detects the SDProp-propagated GenericAll ACE on the Domain Admins AD "
        "group that grants the rogue-DC user mira.corp.sus\\svc_dfsr (SID "
        "S-1-5-21-2621662996-3783546519-2219607937-1119) full control over the "
        "group. Planted via AdminSDHolder by scripts/infra/rogue_dc_admin_persist.py; "
        "SDProp re-plants it every ~60 min unless the AdminSDHolder ACE (see "
        "windows_adminsdholder IOC) is also removed. Removing this ACE alone "
        "flips the IOC to remediated only until the next SDProp cycle."
    ),
    category="users",
    difficulty=4,
    os="windows",
    requires_dc=True,
    pre_deployed=True,
    deploy_script=None,
    remediate_script=None,
    check=ScriptCheck(
        script="check_scripts/windows/windows_rogue_trust_da_ace_check.ps1",
    ),
    discovery=(
        "On the DC: inspect the Domain Admins group's ACL: "
        "`(Get-Acl 'AD:\\CN=Domain Admins,CN=Users,'+(Get-ADDomain).DistinguishedName).Access "
        "| Where-Object { $_.IdentityReference -match 'S-1-5-21-2621662996' }`"
    ),
    detection_steps=[
        (
            "On the DC, dump the Domain Admins ACL and filter for the rogue SID: "
            "`Import-Module ActiveDirectory; $dn = (Get-ADDomain).DistinguishedName; "
            '(Get-Acl "AD:\\CN=Domain Admins,CN=Users,$dn").Access | Where-Object { '
            "$_.IdentityReference.Value -eq 'S-1-5-21-2621662996-3783546519-2219607937-1119' -and "
            "$_.ActiveDirectoryRights -band [System.DirectoryServices.ActiveDirectoryRights]::GenericAll -and "
            "$_.AccessControlType -eq 'Allow' }`."
        ),
        (
            "IOC present when the query returns a non-null Allow ACE. The ACE arrived "
            "here via SDProp propagating AdminSDHolder's DACL; it will also appear on "
            "Enterprise Admins, BUILTIN\\Administrators, krbtgt, and every user with "
            "adminCount=1. Enumerate those objects' ACLs too — a partial cleanup that "
            "misses AdminSDHolder loses ground on the next SDProp cycle."
        ),
        (
            "Confirm by looking at the FSP for the rogue SID (see IOC "
            "`windows_rogue_trust_fsp`) and the mira.corp.sus trust record "
            "(see IOC `windows_rogue_trust_external`) — all three ship together in "
            "the rogue-DC persistence chain."
        ),
    ],
    remediation_steps=[
        (
            "Remove the ACE from Domain Admins: "
            '`$dn = (Get-ADDomain).DistinguishedName; $path = "AD:\\CN=Domain Admins,CN=Users,$dn"; '
            "$acl = Get-Acl $path; "
            "$sidStr = 'S-1-5-21-2621662996-3783546519-2219607937-1119'; "
            "$acl.Access | Where-Object { $_.IdentityReference.Value -eq $sidStr } | "
            "ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }; "
            "Set-Acl -Path $path -AclObject $acl`."
        ),
        (
            "Repeat the same ACE removal on every other protected principal "
            "(Enterprise Admins, BUILTIN\\Administrators, krbtgt, adminCount=1 users). "
            "Otherwise a red operator with valid rogue-side auth can still exercise "
            "GenericAll on whatever protected object still carries the ACE."
        ),
        (
            "Also clean the AdminSDHolder object itself (see IOC "
            "`windows_adminsdholder` remediation) — if AdminSDHolder still holds "
            "the ACE, SDProp will re-plant it on Domain Admins within 60 min."
        ),
        (
            "Force SDProp to run immediately so propagation state is fresh: "
            "`$root = [ADSI]'LDAP://RootDSE'; $root.Put('RunProtectAdminGroupsTask', 1); $root.SetInfo()`. "
            "Re-run this check afterwards; it should score status=0."
        ),
    ],
    artifacts=[
        Artifact(
            path="CN=Domain Admins,CN=Users",
            description=(
                "GenericAll Allow ACE for SID "
                "S-1-5-21-2621662996-3783546519-2219607937-1119 (rogue "
                "mira.corp.sus\\svc_dfsr) on the Domain Admins group's DACL, "
                "propagated from AdminSDHolder by SDProp"
            ),
        ),
    ],
)
