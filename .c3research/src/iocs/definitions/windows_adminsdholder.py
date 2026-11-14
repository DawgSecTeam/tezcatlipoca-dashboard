from iocs.base import Artifact, IOC, ScriptCheck

windows_adminsdholder = IOC(
    name="windows_adminsdholder",
    description="Adds EVERYONE:GenericAll ACE to AdminSDHolder in AD; SDProp propagates it to all protected groups",
    category="users",
    difficulty=4,
    os="windows",
    # Only DCs carry the AdminSDHolder object + the ActiveDirectory PowerShell
    # module the deploy script Import-Modules. A member-server run would fail
    # with "module 'ActiveDirectory' was not loaded because no valid module
    # file was found" — a real failure, but one that just clutters deploy_all
    # output on the members it was never meant for.
    requires_dc=True,
    check=ScriptCheck(script="check_scripts/windows/windows_adminsdholder_check.ps1"),
    discovery="On the DC: inspect AdminSDHolder ACL in AD: "
    "`Get-Acl 'AD:\\CN=AdminSDHolder,CN=System,DC=...' | Select-Object -ExpandProperty Access`.",
    deploy_script="deploy_scripts/windows/windows_adminsdholder_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_adminsdholder_remediate.ps1",
    detection_steps=[
        "On the DC, run: `Import-Module ActiveDirectory; "
        "$path = 'AD:\\CN=AdminSDHolder,CN=System,' + (Get-ADDomain).DistinguishedName; "
        "(Get-Acl $path).Access | Where-Object { $_.IdentityReference -match 'Everyone' }`",
        "IOC present when EVERYONE (or S-1-1-0) has GenericAll on AdminSDHolder.",
        "SDProp runs every 60 min and will propagate this ACE to Domain Admins, "
        "Enterprise Admins, and other protected groups — check those ACLs too.",
    ],
    remediation_steps=[
        "Remove the ACE from AdminSDHolder: "
        "`$path = 'AD:\\CN=AdminSDHolder,CN=System,' + (Get-ADDomain).DistinguishedName; "
        "$acl = Get-Acl $path; "
        "$rule = $acl.Access | Where-Object { $_.IdentityReference -match 'Everyone' }; "
        "$acl.RemoveAccessRule($rule); Set-Acl $path $acl`",
        "Force SDProp to run immediately: "
        "`$rootDSE = [ADSI]'LDAP://RootDSE'; $rootDSE.runProtectAdminGroupsTask = 1; $rootDSE.CommitChanges()`",
        "Verify the ACE is gone and protected group ACLs have been cleaned up.",
    ],
    artifacts=[
        Artifact(
            path="CN=AdminSDHolder,CN=System",
            description="EVERYONE:GenericAll ACE on the AdminSDHolder object in Active Directory",
        ),
    ],
)
