from iocs.base import Artifact, IOC, ScriptCheck

linux_clean_firewall = IOC(
    name="linux_clean_firewall",
    description="Flushes iptables/nftables and disables firewall services; cron re-flushes every 5 min",
    category="impair_defenses",
    difficulty=2,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_clean_firewall_check.sh"),
    discovery="Check iptables -L; check ufw/firewalld status; check root crontab for C3R_CLEANFW entry",
    deploy_script="deploy_scripts/linux/linux_clean_firewall_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_clean_firewall_remediate.sh",
    detection_steps=[
        "Check the active firewall rules: `iptables -L INPUT -n` — IOC is present when the INPUT "
        "chain has no DROP or REJECT rules and the default policy is ACCEPT.",
        "Check ufw status: `ufw status` — 'inactive' is suspicious on a hardened host.",
        "Check firewalld: `systemctl is-active firewalld` — 'inactive' on a host expected to run it.",
        "Check root crontab for the C3R_CLEANFW marker: "
        "`crontab -l -u root | grep C3R_CLEANFW` or scan `/var/spool/cron/crontabs/root`.",
    ],
    remediation_steps=[
        "Remove the C3R cron entry from root's crontab: "
        "`crontab -l | grep -v C3R_CLEANFW | crontab -`.",
        "Re-enable the firewall service: `systemctl enable --now ufw` (Debian/Ubuntu) "
        "or `systemctl enable --now firewalld` (RHEL/CentOS).",
        "Restore firewall rules: `ufw enable` (Ubuntu) or `systemctl start firewalld`.",
        "Verify: `iptables -L INPUT` should show DROP/REJECT rules and the cron entry should be gone.",
    ],
    artifacts=[
        Artifact(
            path="/var/spool/cron/crontabs/root",
            description="Root crontab holding the */5 iptables-flush entry marked # C3R_CLEANFW",
        ),
        Artifact(
            dynamic=False,
            path="iptables INPUT ACCEPT",
            description="All INPUT/FORWARD/OUTPUT chains flushed and set to ACCEPT; ufw/firewalld disabled",
        ),
    ],
)
