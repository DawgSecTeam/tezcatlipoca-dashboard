from iocs.base import Artifact, IOC, ScriptCheck

linux_adduser_crontab = IOC(
    name="linux_adduser_crontab",
    description="Root crontab adds N pool-driven users with sudo access every 5 minutes (growing)",
    category="users",
    difficulty=1,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/adduser-crontab-check.sh"),
    discovery="Check /var/spool/cron/* for entries; check for unexpected local users",
    deploy_script="deploy_scripts/linux/adduser-crontab-deploy.sh",
    remediate_script="remediate_scripts/linux/adduser-crontab-remediate.sh",
    # 20 pool-driven usernames injected as space-separated C3R_USER_NAMES
    pooled_name_lists=[("USER_NAMES", "user", 20)],
    sa_highlight={},
    detection_steps=[
        "Read root's crontab: `sudo crontab -l -u root`, or cat the spool file directly "
        "(`/var/spool/cron/crontabs/root` on Debian/Ubuntu, `/var/spool/cron/root` on "
        "RHEL/CentOS). Look for a `*/5 * * * *` entry containing a for-loop over usernames "
        "marked with `# C3R_ADDUSER`.",
        "Identify each planted account by listing unexpected users: "
        "`awk -F: '$3 >= 1000 && $1 != \"nobody\" {print $1}' /etc/passwd` and check "
        "which have `sudo` or `wheel` group membership via `groups <username>`.",
        "Verify cron is actually running (`systemctl is-active cron || systemctl is-active crond`) "
        "— a stopped cron service makes the check read as remediated even if the line is still present.",
    ],
    remediation_steps=[
        "Delete the malicious cron entry from root's crontab by removing any line marked "
        "`# C3R_ADDUSER` from `/var/spool/cron/crontabs/root` and `/var/spool/cron/root`.",
        "Remove each planted account: `sudo userdel -r <username>` for each user listed "
        "in the cron entry's for-loop.",
        "Drop each account from privileged groups if accounts are kept: "
        "`sudo gpasswd -d <username> sudo; sudo gpasswd -d <username> wheel`.",
        "Wait one 5-minute cron interval and re-check — if any user reappears, the crontab "
        "line was not fully removed.",
    ],
    artifacts=[
        Artifact(
            path="/var/spool/cron/crontabs/root",
            description="Debian/Ubuntu root crontab holding the malicious */5 for-loop useradd entry (# C3R_ADDUSER)",
        ),
        Artifact(
            path="/var/spool/cron/root",
            description="RHEL/CentOS root crontab holding the same entry",
        ),
        Artifact(
            dynamic=True,
            description="N pool-driven local accounts (each: password=username) recreated every 5 min and added to sudo/wheel",
        ),
    ],
)
