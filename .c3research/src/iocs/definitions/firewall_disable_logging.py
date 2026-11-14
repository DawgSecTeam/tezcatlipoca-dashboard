from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import FirewallClearLogCron

firewall_disable_logging = IOC(
    name="firewall_disable_logging",
    description="Cron periodically clears syslog on pfSense",
    category="impair_defenses",
    difficulty=2,
    os="firewall",
    check=ScriptCheck(
        script="check_scripts/firewall/firewall_disable_logging_check.sh"
    ),
    persistence=FirewallClearLogCron,
    discovery="Check root crontab for syslog clear entry; check /var/log/system.log entry count",
    deploy_script="deploy_scripts/firewall/firewall_disable_logging_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_disable_logging_remediate.sh",
    detection_steps=[
        "List root's crontab on the pfSense box: `crontab -l` (run as root via SSH). "
        "Look for a line containing `system.log`, `syslog`, or `truncate.*log` — "
        "specifically the planted entry: "
        "`*/30 * * * * /usr/bin/truncate -s 0 /var/log/system.log >/dev/null 2>&1 # syslog_clear_ioc`.",
        "Confirm the log has been wiped: `wc -l /var/log/system.log` — a zero or near-zero "
        "line count on an active pfSense box is a strong indicator the log was just truncated.",
        "Check when the file was last modified: `ls -la /var/log/system.log`. A very recent "
        "mtime on an otherwise-quiet firewall suggests periodic truncation is occurring.",
        "Cross-reference with pfSense's GUI: Status > System Logs > Firewall. An empty or "
        "suspiciously sparse log while traffic is flowing confirms the IOC is active.",
    ],
    remediation_steps=[
        "Remove the malicious cron entry from root's crontab. On pfSense (FreeBSD) the safest "
        "method is: `crontab -l | grep -vE 'system\\.log|syslog|truncate.*log' | crontab -`.",
        "Verify the entry is gone: `crontab -l` should return no lines matching the pattern. "
        "If output is empty that is acceptable — pfSense root crontab may have had no other entries.",
        "Restore syslog so new events are captured: pfSense will begin writing to "
        "`/var/log/system.log` automatically once the truncation job is removed — no manual "
        "action is needed to restart syslogd.",
        "Wait at least 30 minutes (one cron interval) and re-run `wc -l /var/log/system.log`. "
        "If the count remains near zero or drops to zero again, the cron entry was not fully "
        "removed. Re-inspect `crontab -l` for any remaining match.",
    ],
    artifacts=[
        Artifact(
            path="/var/log/system.log",
            description="pfSense syslog file; truncated to 0 bytes immediately on deploy "
            "and every 30 minutes thereafter by the cron job",
        ),
        Artifact(
            path="root crontab (FreeBSD — inspect with `crontab -l`)",
            description="Contains the planted entry: "
            "`*/30 * * * * /usr/bin/truncate -s 0 /var/log/system.log >/dev/null 2>&1 "
            "# syslog_clear_ioc`; this single entry is both the effect and the persistence "
            "mechanism (FirewallClearLogCron)",
        ),
    ],
)
