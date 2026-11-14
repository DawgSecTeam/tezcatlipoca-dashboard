from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import LinuxLogCleanCron

linux_disable_logging = IOC(
    name="linux_disable_logging",
    description="Cron periodically clears journalctl logs",
    category="impair_defenses",
    difficulty=2,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_disable_logging_check.sh"),
    persistence=LinuxLogCleanCron,
    discovery="Check root crontab for journalctl vacuum; check journal entry count",
    deploy_script="deploy_scripts/linux/linux_disable_logging_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_disable_logging_remediate.sh",
    # The /etc/cron.d file NAME is pool-driven + scored (effect + persistence
    # checks grep it by name); dir /etc/cron.d is technique-fixed. The crontab
    # journalctl signal stays fixed. docs/PLAN_build_check_deploy_name_pools.md §5B.
    pooled_names=[("CRON_NAME", "cron")],
    detection_steps=[
        "Check the system-wide cron drop-in directory for the planted file: "
        "`ls -la /etc/cron.d/log-cleanup` — if it exists the main IOC artifact is present.",
        "Observe the journal is nearly empty due to the immediate vacuum run at "
        "deploy time: `sudo journalctl --disk-usage` — unusually low usage "
        "(kilobytes rather than megabytes) is the forensic signal.",
        "Confirm the persistence layer: the LinuxLogCleanCron persistence mechanism "
        "injects the same `journalctl` line into root's personal crontab. Run "
        "`sudo crontab -l | grep journalctl` and look for a `*/15 * * * *` line "
        "calling `/usr/bin/journalctl --vacuum-time=1s` — this entry is separate "
        "from `/etc/cron.d/log-cleanup` and must also be removed.",
        "Verify both locations before remediation: `ls /etc/cron.d/log-cleanup` "
        "(main artifact) and `sudo crontab -l | grep journalctl` (persistence "
        "layer) should both match to confirm full scope of the IOC.",
    ],
    remediation_steps=[
        "Delete the system cron drop-in file: `sudo rm -f /etc/cron.d/log-cleanup`.",
        "Remove the journalctl vacuum line from root's personal crontab: "
        "`sudo crontab -l | grep -v journalctl | sudo crontab -`.",
        "Verify both locations are clean: `sudo crontab -l | grep journalctl` "
        "should return nothing, and `ls /etc/cron.d/log-cleanup` should report "
        "no such file.",
        "Wait 15 minutes (one cron interval) and re-check both paths — if the "
        "entry reappears, an additional persistence vector (e.g. the LinuxLogCleanCron "
        "persistence crontab entry) was not fully removed; repeat the `crontab -l` "
        "grep-and-strip step.",
    ],
    artifacts=[
        Artifact(
            path="/etc/cron.d/log-cleanup",
            description="System-wide cron drop-in that runs journalctl --vacuum-time=1s every 15 min",
        ),
        Artifact(
            path="/var/spool/cron/crontabs/root",
            description="Root personal crontab; persistence mechanism appends the same vacuum line here",
        ),
    ],
)
