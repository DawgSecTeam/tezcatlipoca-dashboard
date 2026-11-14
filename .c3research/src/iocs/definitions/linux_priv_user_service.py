from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import LinuxPrivUserService

linux_priv_user_service = IOC(
    name="linux_priv_user_service",
    description="Hidden systemd service re-adds privileged user svc-backup every 5min",
    category="users",
    difficulty=2,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_priv_user_service_check.sh"),
    persistence=LinuxPrivUserService,
    discovery="Check /etc/passwd for svc-backup; check systemctl for user-account-helper",
    deploy_script="deploy_scripts/linux/linux_priv_user_service_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_priv_user_service_remediate.sh",
    # USER_NAME scored (effect check: id "$USER"); service name "user-account-helper" is fixed.
    pooled_names=[("USER_NAME", "user")],
    added_credentials=[],
    sa_highlight={"local_users_groups": ["svc-backup"]},
    detection_steps=[
        "Check whether the backdoor account exists: `id svc-backup` or `getent passwd svc-backup`.",
        "Confirm its group memberships include sudo or wheel: "
        "`groups svc-backup` — look for `sudo` (Debian/Ubuntu) or `wheel` (RHEL/Rocky).",
        "Inspect the persistence service unit: `systemctl cat user-account-helper` "
        "and `systemctl is-active user-account-helper`. "
        "The unit file lives at `/etc/systemd/system/user-account-helper.service`.",
        "Read the unit's ExecStart line — it runs a `/bin/sh -c` one-liner that recreates "
        "`svc-backup` and re-adds it to sudo/wheel every time the service fires.",
        "Verify the service is enabled (survives reboot): "
        "`systemctl is-enabled user-account-helper` should print `enabled`.",
    ],
    remediation_steps=[
        "Stop and disable the persistence service: "
        "`sudo systemctl stop user-account-helper && sudo systemctl disable user-account-helper`.",
        "Delete the unit file: `sudo rm -f /etc/systemd/system/user-account-helper.service`.",
        "Reload the systemd daemon so the unit disappears from the catalog: "
        "`sudo systemctl daemon-reload && sudo systemctl reset-failed user-account-helper`.",
        "Delete the backdoor account and its home directory: "
        "`sudo userdel -r svc-backup` (falls back to `sudo userdel svc-backup` if home removal fails).",
        "Verify the service is gone: `systemctl cat user-account-helper` must return an error. "
        "Then confirm the account is gone: `id svc-backup` must return 'no such user'. "
        "Wait one check interval (5 min) and re-check — if `svc-backup` reappears the unit file "
        "was not fully removed and the service is still re-triggering.",
    ],
    artifacts=[
        Artifact(
            path="/etc/systemd/system/user-account-helper.service",
            description="Systemd oneshot unit that re-creates svc-backup and adds it to sudo/wheel",
        ),
        Artifact(
            path="svc-backup",
            description="Privileged local account (sudo/wheel member) recreated by the service",
        ),
        Artifact(
            path="/home/svc-backup",
            description="Home directory created for svc-backup by useradd -m",
        ),
    ],
)
