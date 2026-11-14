from iocs.base import Artifact, IOC, ScriptCheck

# IMPORTANT: Replace the placeholder key below with the actual red-team public key before competition.
# The comment "red_team@c3rberus" is the grep anchor used by check and remediate scripts.
_RED_TEAM_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILytoI19adkUx3TPcOyoN9x6cQQHmeiPsY82z6vUcm92"
    " red_team@c3rberus"
)

firewall_ssh_authorized_keys = IOC(
    name="firewall_ssh_authorized_keys",
    description="Appends red-team SSH public key to pfSense admin's authorized_keys for passwordless SSH",
    category="remote_access",
    difficulty=2,
    os="firewall",
    check=ScriptCheck(script="check_scripts/firewall/firewall_ssh_authorized_keys_check.sh"),
    discovery="Check `cat /root/.ssh/authorized_keys` for 'red_team@c3rberus'.",
    deploy_script="deploy_scripts/firewall/firewall_ssh_authorized_keys_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_ssh_authorized_keys_remediate.sh",
    detection_steps=[
        "Run `grep red_team /root/.ssh/authorized_keys` — IOC present if key with comment `red_team@c3rberus` exists.",
    ],
    remediation_steps=[
        "Mount read-write: `mount -uw /`.",
        "Remove: `sed -i '' '/red_team@c3rberus/d' /root/.ssh/authorized_keys`.",
    ],
    artifacts=[
        Artifact(
            path="/root/.ssh/authorized_keys",
            description="Red-team SSH public key (comment: red_team@c3rberus) appended to pfSense root authorized_keys",
        ),
    ],
)
