from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import FirewallFlushRulesCron

firewall_disable_fw_rules = IOC(
    name="firewall_disable_fw_rules",
    description=(
        "Cron reloads an allow-all pf ruleset every minute, neutralising the "
        "firewall without severing routing. Any operator-side restore is "
        "overwritten within 60s."
    ),
    category="impair_defenses",
    difficulty=2,
    os="firewall",
    check=ScriptCheck(
        script="check_scripts/firewall/firewall_disable_fw_rules_check.sh"
    ),
    persistence=FirewallFlushRulesCron,
    discovery=(
        "Check root crontab for the pfctl_allow_all_ioc marker; check for the "
        "staged allow-all ruleset file at /var/spool/pf_allow_all.conf; "
        "inspect the live pf ruleset for the two-rule pass-all signature."
    ),
    deploy_script="deploy_scripts/firewall/firewall_disable_fw_rules_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_disable_fw_rules_remediate.sh",
    detection_steps=[
        "Inspect root's crontab for the allow-all reload entry: `crontab -l` "
        "(as root, or via SSH). Look for a line containing "
        "`* * * * * /sbin/pfctl -f /var/spool/pf_allow_all.conf` with the "
        "comment marker `# pfctl_allow_all_ioc`.",
        "Verify the crontab spool file directly on pfSense (FreeBSD): "
        "`cat /var/cron/tabs/root`. The malicious line appears there even if "
        "`crontab -l` output is suppressed.",
        "Look for the staged allow-all ruleset file: "
        "`ls -l /var/spool/pf_allow_all.conf`. The file's two lines are "
        "`pass in all` and `pass out all` — that is the ruleset the cron "
        "reloads every minute.",
        "Inspect the live pf ruleset: `pfctl -sr`. A healthy pfSense firewall "
        "normally has 60+ pass/block rules driven by the config. Exactly two "
        "rules — `pass in all` and `pass out all` — with no block rules means "
        "the allow-all IOC is currently in force.",
    ],
    remediation_steps=[
        "Remove the malicious cron line from root's crontab: "
        "`crontab -l | grep -v pfctl_allow_all_ioc | grep -v pfctl_flush_ioc "
        "| crontab -`. Confirm with `crontab -l` — no line should mention "
        "either marker (the flush variant is the older IOC vintage).",
        "Delete the staged ruleset file so a surviving cron tick cannot "
        "re-arm it: `rm -f /var/spool/pf_allow_all.conf`.",
        "Restore the pfSense firewall rules from the running config by "
        "executing `/etc/rc.filter_configure`. This re-applies the ruleset "
        "stored in `/cf/conf/config.xml` without requiring a full reboot.",
        "If `/etc/rc.filter_configure` is absent or fails, load rules from "
        "the debug snapshot: `pfctl -f /tmp/rules.debug`. As a last resort, "
        "reboot pfSense — it will reload rules from `/cf/conf/config.xml` on "
        "boot.",
        "Wait at least one minute and re-check `pfctl -sr` to confirm the "
        "rule count is back to its expected baseline (60+ rules). If the "
        "ruleset drops back to two pass-all rules, the cron entry was not "
        "fully removed — re-inspect `/var/cron/tabs/root` directly.",
    ],
    artifacts=[
        Artifact(
            path="/var/cron/tabs/root",
            description=(
                "Root crontab spool file on pfSense (FreeBSD); contains the "
                "`* * * * * /sbin/pfctl -f /var/spool/pf_allow_all.conf "
                "# pfctl_allow_all_ioc` entry"
            ),
        ),
        Artifact(
            path="/var/spool/pf_allow_all.conf",
            description=(
                "Two-line pf ruleset (`pass in all` / `pass out all`) staged "
                "for the cron to reload every minute"
            ),
        ),
    ],
)
