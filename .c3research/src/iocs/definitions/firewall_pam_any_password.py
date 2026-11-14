from iocs.base import Artifact, IOC, ScriptCheck

firewall_pam_any_password = IOC(
    name="firewall_pam_any_password",
    description="Inserts pam_permit.so into /etc/pam.d/sshd on pfSense so any SSH password is accepted",
    category="users",
    difficulty=3,
    os="firewall",
    check=ScriptCheck(script="check_scripts/firewall/firewall_pam_any_password_check.sh"),
    discovery="Grep PAM sshd config for pam_permit: `grep pam_permit /etc/pam.d/sshd`.",
    deploy_script="deploy_scripts/firewall/firewall_pam_any_password_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_pam_any_password_remediate.sh",
    detection_steps=[
        "Run `grep -n pam_permit /etc/pam.d/sshd` — IOC present if line with C3R_PAM_ANY appears.",
        "The inserted line: `auth sufficient pam_permit.so # C3R_PAM_ANY`.",
        "Test: SSH with wrong password — if accepted, IOC is likely active.",
    ],
    remediation_steps=[
        "Mount read-write: `mount -uw /`.",
        "Remove: `sed -i '' '/C3R_PAM_ANY/d' /etc/pam.d/sshd`.",
        "Restart sshd to reload PAM config: `service sshd restart`.",
    ],
    artifacts=[
        Artifact(
            path="/etc/pam.d/sshd",
            description="auth sufficient pam_permit.so line inserted, accepting any SSH password",
        ),
    ],
)
