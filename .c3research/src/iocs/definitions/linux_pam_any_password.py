from iocs.base import Artifact, IOC, ScriptCheck

linux_pam_any_password = IOC(
    name="linux_pam_any_password",
    description="Inserts pam_permit.so at the top of /etc/pam.d/common-auth so any password succeeds",
    category="users",
    difficulty=3,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_pam_any_password_check.sh"),
    discovery="Grep PAM config for pam_permit: `grep -r pam_permit /etc/pam.d/`.",
    deploy_script="deploy_scripts/linux/linux_pam_any_password_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_pam_any_password_remediate.sh",
    detection_steps=[
        "Run `grep -n pam_permit /etc/pam.d/common-auth /etc/pam.d/sshd /etc/pam.d/login 2>/dev/null`.",
        "IOC line: `auth sufficient pam_permit.so # C3R_PAM_ANY`.",
        "Line must appear BEFORE the first `auth required` or `auth [success=...]` module to be effective.",
    ],
    remediation_steps=[
        "Remove the IOC line: `sed -i '/C3R_PAM_ANY/d' /etc/pam.d/common-auth`.",
        "Verify with `grep pam_permit /etc/pam.d/common-auth` — should return nothing.",
        "Test SSH login with wrong password — it should now fail.",
    ],
    artifacts=[
        Artifact(
            path="/etc/pam.d/common-auth",
            description="auth sufficient pam_permit.so line inserted at top of common-auth stack",
        ),
    ],
)
