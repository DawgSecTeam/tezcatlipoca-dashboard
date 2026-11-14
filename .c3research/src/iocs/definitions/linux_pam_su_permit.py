from iocs.base import IOC, Artifact, ScriptCheck

linux_pam_su_permit = IOC(
    name="linux_pam_su_permit",
    description="PAM entry blanket-allows su without password",
    category="persistence",
    difficulty=2,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_pam_su_permit_check.sh"),
    discovery=(
        "Check the PAM configurations for suspicious entries. "
        "Check for PAM entries that have 'sufficient' as these can immediately succeed "
        "and stop processing, so they should be careful of what module it is applied to."
    ),
    deploy_script="deploy_scripts/linux/linux_pam_su_permit_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_pam_su_permit_remediate.sh",
    detection_steps=[
        "Inspect the PAM `su` configuration: `cat /etc/pam.d/su`. Look for any line matching "
        "`auth sufficient pam_permit.so` (with any whitespace variation). This line tells PAM to "
        "immediately grant authentication for `su` without verifying a password.",
        "Confirm the exact line with grep: "
        "`grep -E 'auth[[:space:]]+sufficient[[:space:]]+pam_permit\\.so' /etc/pam.d/su`. "
        "A match means the IOC is present; no output means it has been removed.",
        "Verify the impact: the rogue line is inserted near the top of the `su` auth stack "
        "(the exact line number varies by distro). Check the surrounding context with "
        "`grep -n 'pam_permit' /etc/pam.d/su` to confirm the exact location.",
        "Cross-check that no other PAM service files have been tampered with: "
        "`grep -rE 'auth[[:space:]]+sufficient[[:space:]]+pam_permit\\.so' /etc/pam.d/` "
        "to identify any additional files beyond `/etc/pam.d/su`.",
    ],
    remediation_steps=[
        "Remove the malicious line from `/etc/pam.d/su`: "
        "`sudo sed -i '/auth[[:space:]]\\+sufficient[[:space:]]\\+pam_permit\\.so/d' /etc/pam.d/su`.",
        "Verify the line is gone: "
        "`grep -E 'auth[[:space:]]+sufficient[[:space:]]+pam_permit\\.so' /etc/pam.d/su`. "
        "The command should produce no output.",
        "Confirm `su` still functions correctly for legitimate users by checking that the remaining "
        "PAM stack in `/etc/pam.d/su` is intact and ends with the expected `pam_unix.so` or "
        "`pam_wheel.so` entries appropriate for your distribution.",
        "There is no persistence mechanism for this IOC, but re-check `/etc/pam.d/su` after the "
        "next scoring cycle to confirm the line has not been reinserted by another vector.",
    ],
    artifacts=[
        Artifact(
            path="/etc/pam.d/su",
            description="PAM su config file with rogue `auth sufficient pam_permit.so` line inserted",
        ),
    ],
)
