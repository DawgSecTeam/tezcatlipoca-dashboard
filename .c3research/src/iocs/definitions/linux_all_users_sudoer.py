from iocs.base import Artifact, IOC, ScriptCheck

linux_all_users_sudoer = IOC(
    name="linux_all_users_sudoer",
    description="Writes an /etc/sudoers.d drop-in granting ALL users passwordless sudo",
    category="users",
    difficulty=2,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_all_users_sudoer_check.sh"),
    discovery="Check `ls /etc/sudoers.d/` for suspicious files. "
    "Run `sudo -l` as a normal user — if NOPASSWD ALL appears without explicit configuration, IOC is present.",
    deploy_script="deploy_scripts/linux/linux_all_users_sudoer_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_all_users_sudoer_remediate.sh",
    detection_steps=[
        "List `/etc/sudoers.d/`: `ls -la /etc/sudoers.d/` — look for unexpected files.",
        "Inspect `99-admins` (or similar): `cat /etc/sudoers.d/99-admins`.",
        "The IOC line reads: `ALL ALL=(ALL:ALL) NOPASSWD: ALL`.",
    ],
    remediation_steps=[
        "Remove the drop-in file: `rm /etc/sudoers.d/99-admins`.",
        "Verify with `visudo -c` — should report 'parsed OK'.",
        "Test: a non-root user should no longer have unlimited sudo without a password.",
    ],
    artifacts=[
        Artifact(
            path="/etc/sudoers.d/99-admins",
            description="sudoers drop-in: ALL ALL=(ALL:ALL) NOPASSWD: ALL",
        ),
    ],
)
