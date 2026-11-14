from iocs.base import Artifact, IOC, ScriptCheck

# IMPORTANT: Replace the placeholder key below with the actual red-team public key before competition.
# The private key must NEVER be committed. The comment "red_team@c3rberus" is the grep anchor
# used by check and remediate scripts — keep it unchanged.
_RED_TEAM_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILytoI19adkUx3TPcOyoN9x6cQQHmeiPsY82z6vUcm92"
    " red_team@c3rberus"
)

linux_ssh_authorized_keys = IOC(
    name="linux_ssh_authorized_keys",
    description="Appends red-team SSH public key to root's authorized_keys enabling passwordless SSH",
    category="remote_access",
    difficulty=2,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_ssh_authorized_keys_check.sh"),
    discovery="Check `cat /root/.ssh/authorized_keys` for unexpected public keys. "
    "Look for the comment 'red_team@c3rberus'.",
    deploy_script="deploy_scripts/linux/linux_ssh_authorized_keys_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_ssh_authorized_keys_remediate.sh",
    detection_steps=[
        "Run `cat /root/.ssh/authorized_keys` — IOC key has comment `red_team@c3rberus`.",
        "Run `grep red_team /root/.ssh/authorized_keys` to confirm.",
    ],
    remediation_steps=[
        "Remove: `sed -i '/red_team@c3rberus/d' /root/.ssh/authorized_keys`.",
        "Verify: `grep red_team /root/.ssh/authorized_keys` should return nothing.",
    ],
    artifacts=[
        Artifact(
            path="/root/.ssh/authorized_keys",
            description="Red-team SSH public key (comment: red_team@c3rberus) appended to root's authorized_keys",
        ),
    ],
)
