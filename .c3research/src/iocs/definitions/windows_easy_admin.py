from iocs.base import Artifact, IOC, ScriptCheck

windows_easy_admin = IOC(
    name="windows_easy_admin",
    description="Pool-driven obvious malicious admin users (up to 5 per box)",
    category="users",
    difficulty=1,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_easy_admin_check.ps1"),
    discovery="Check Users and Computers, Domain users; look for unexpected accounts in Administrators",
    deploy_script="deploy_scripts/windows/windows_easy_admin_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_easy_admin_remediate.ps1",
    # Up to 5 pool-driven usernames injected as space-separated C3R_USER_NAMES
    pooled_name_lists=[("USER_NAMES", "user", 5)],
    sa_highlight={},
    detection_steps=[
        "On a Domain Controller (ProductType 2), open Active Directory Users and Computers "
        "or run: `Get-ADGroupMember -Identity 'Domain Admins' -Recursive` — look for unexpected "
        "accounts that are not legitimate admin accounts.",
        "On a workstation or member server, check for unexpected local administrator accounts: "
        "`Get-LocalGroupMember -Group 'Administrators'`.",
        "Cross-reference the SA dashboard's local_users_groups panel — any unfamiliar accounts "
        "in the Administrators or Domain Admins group are IOC candidates.",
        "Check the C3rberus task details or deploy_params for the exact list of planted usernames.",
    ],
    remediation_steps=[
        "On a Domain Controller: remove each account from Domain Admins then delete it — "
        "`Remove-ADGroupMember -Identity 'Domain Admins' -Members <name> -Confirm:$false`; "
        "`Remove-ADUser -Identity <name> -Confirm:$false`.",
        "On a workstation or member server: remove each account from the local Administrators group — "
        "`Remove-LocalGroupMember -Group 'Administrators' -Member <name>`; "
        "then delete: `Remove-LocalUser -Name <name>`.",
        "Verify the accounts are gone: re-run the detection queries and confirm no IOC accounts remain.",
        "Check for any active sessions running as these accounts and terminate them.",
    ],
    artifacts=[
        Artifact(
            dynamic=True,
            description="Up to 5 pool-driven AD domain users (DC) or local accounts (member/workstation) in Administrators/Domain Admins",
        ),
    ],
)
