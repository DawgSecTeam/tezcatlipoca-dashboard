from iocs.base import Artifact, IOC, ScriptCheck

firewall_all_users_sudoer = IOC(
    name="firewall_all_users_sudoer",
    description="Writes a sudoers drop-in on pfSense granting ALL users passwordless sudo",
    category="users",
    difficulty=2,
    os="firewall",
    check=ScriptCheck(script="check_scripts/firewall/firewall_all_users_sudoer_check.sh"),
    discovery="Check `ls /usr/local/etc/sudoers.d/` for unexpected files. "
    "Inspect `/usr/local/etc/sudoers` for ALL NOPASSWD lines.",
    deploy_script="deploy_scripts/firewall/firewall_all_users_sudoer_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_all_users_sudoer_remediate.sh",
    detection_steps=[
        "Run `ls /usr/local/etc/sudoers.d/` — look for `99-admins`.",
        "Inspect the file: should contain `ALL ALL=(ALL:ALL) NOPASSWD: ALL`.",
        "Also check `/usr/local/etc/sudoers` for inline insertions.",
    ],
    remediation_steps=[
        "Mount read-write: `mount -uw /`.",
        "Remove: `rm /usr/local/etc/sudoers.d/99-admins`.",
        "Verify with `visudo -c`.",
    ],
    artifacts=[
        Artifact(
            path="/usr/local/etc/sudoers.d/99-admins",
            description="sudoers drop-in: ALL ALL=(ALL:ALL) NOPASSWD: ALL (pfSense path)",
        ),
    ],
)
