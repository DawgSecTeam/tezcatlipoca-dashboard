from iocs.base import Artifact, IOC, ScriptCheck

firewall_scheduled_add_user = IOC(
    name="firewall_scheduled_add_user",
    description="Cron adds pool-driven firewall admin users every 30min via rc.d-reinstalled cron job (growing)",
    category="users",
    difficulty=1,
    os="firewall",
    check=ScriptCheck(
        script="check_scripts/firewall/firewall_scheduled_add_user_check.sh"
    ),
    discovery="Use the shell to find the extraneous users, and remove the pipeline adding them.",
    deploy_script="deploy_scripts/firewall/firewall_scheduled_add_user_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_scheduled_add_user_remediate.sh",
    # Up to 5 pool-driven usernames injected as space-separated C3R_USER_NAMES
    pooled_name_lists=[("USER_NAMES", "user", 5)],
    sa_highlight={
        "scheduled_tasks": ["add_domain"],
    },
    detection_steps=[
        "List the root crontab on the pfSense shell: `crontab -l` and look for a "
        "`*/30 * * * * /bin/sh /var/run/add_domain.sh` entry.",
        "Confirm the payload script exists and is executable: `ls -la /var/run/add_domain.sh` "
        "and `cat /var/run/add_domain.sh` to inspect its user list.",
        "Check for the rc.d boot-persistence script: `ls -la /usr/local/etc/rc.d/domain_admin_rc.sh`. "
        "This service (`domain_admin_creator`) reinstalls the cron job on every reboot.",
        "Verify the malicious accounts exist: check each username from the cron script with "
        "`/usr/bin/id <username>` and confirm each is a member of the `admins` group.",
    ],
    remediation_steps=[
        "Remount the pfSense root filesystem read-write: `mount -uw /`.",
        "Stop the rc.d service and delete it: "
        "`sh /usr/local/etc/rc.d/domain_admin_rc.sh stop ; "
        "rm -f /usr/local/etc/rc.d/domain_admin_rc.sh`.",
        "Remove the cron job from root's crontab: "
        "`crontab -l 2>/dev/null | grep -vF /var/run/add_domain.sh | crontab -`.",
        "Delete the payload script: `rm -f /var/run/add_domain.sh`.",
        "Remove each IOC account (check the cron script for the user list): "
        "`pw userdel <username> -r` for each one.",
        "Verify: run `crontab -l` to confirm the `*/30` entry is gone, "
        "then `id <username>` for each account to confirm they no longer exist.",
    ],
    artifacts=[
        Artifact(
            path="/var/run/add_domain.sh",
            description="Cron payload script that creates/restores the IOC accounts every 30 min",
        ),
        Artifact(
            path="/usr/local/etc/rc.d/domain_admin_rc.sh",
            description="rc.d boot script (service domain_admin_creator) that reinstalls the cron job on reboot",
        ),
        Artifact(
            dynamic=True,
            description="Up to 5 pool-driven local pfSense accounts added to the 'admins' group; recreated every 30 min",
        ),
    ],
)
