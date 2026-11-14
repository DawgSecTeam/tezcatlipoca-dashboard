from iocs.base import Artifact, IOC, ScriptCheck

# IMPORTANT: Replace the placeholder key below with the actual red-team public key before competition.
# The comment "red_team@c3rberus" is the grep anchor used by check and remediate scripts.
_RED_TEAM_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILytoI19adkUx3TPcOyoN9x6cQQHmeiPsY82z6vUcm92"
    " red_team@c3rberus"
)

windows_ssh_authorized_keys = IOC(
    name="windows_ssh_authorized_keys",
    description="Appends red-team SSH public key to administrators_authorized_keys for passwordless admin SSH",
    category="remote_access",
    difficulty=2,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_ssh_authorized_keys_check.ps1"),
    discovery="Check `C:\\ProgramData\\ssh\\administrators_authorized_keys` for 'red_team@c3rberus'.",
    deploy_script="deploy_scripts/windows/windows_ssh_authorized_keys_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_ssh_authorized_keys_remediate.ps1",
    detection_steps=[
        "View the file: `Get-Content 'C:\\ProgramData\\ssh\\administrators_authorized_keys'`.",
        "IOC key has comment `red_team@c3rberus`.",
        "Also check `$env:USERPROFILE\\.ssh\\authorized_keys` for per-user keys.",
    ],
    remediation_steps=[
        "Remove the IOC line: filter out `red_team@c3rberus` and re-write the file.",
        "PowerShell: `(Get-Content 'C:\\ProgramData\\ssh\\administrators_authorized_keys') "
        "| Where-Object { $_ -notmatch 'red_team@c3rberus' } "
        "| Set-Content 'C:\\ProgramData\\ssh\\administrators_authorized_keys'`",
    ],
    artifacts=[
        Artifact(
            path="C:\\ProgramData\\ssh\\administrators_authorized_keys",
            description="Red-team SSH public key (comment: red_team@c3rberus) appended for admin SSH access",
        ),
    ],
)
