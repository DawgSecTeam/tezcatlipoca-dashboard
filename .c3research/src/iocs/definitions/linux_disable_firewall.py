from iocs.base import Artifact, IOC, ScriptCheck

linux_disable_firewall = IOC(
    name="linux_disable_firewall",
    description="Stops and disables ufw/firewalld leaving the host with no active firewall",
    category="impair_defenses",
    difficulty=1,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_disable_firewall_check.sh"),
    discovery="Run `ufw status` (should show inactive) and `systemctl is-active firewalld` (should show inactive). "
    "Also check `iptables -L INPUT` for DROP/REJECT rules.",
    deploy_script="deploy_scripts/linux/linux_disable_firewall_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_disable_firewall_remediate.sh",
    detection_steps=[
        "Check ufw: `ufw status` → 'Status: inactive' is the IOC signal.",
        "Check firewalld: `systemctl is-active firewalld` → 'inactive'.",
        "Check iptables: `iptables -L INPUT -n` — absence of DROP/REJECT rules "
        "combined with both services inactive indicates this IOC.",
    ],
    remediation_steps=[
        "Re-enable ufw: `ufw enable && systemctl enable --now ufw`.",
        "Or re-enable firewalld: `systemctl enable --now firewalld`.",
        "Verify: `ufw status` shows 'active' or `systemctl is-active firewalld` shows 'active'.",
    ],
    artifacts=[
        Artifact(
            dynamic=False,
            path="systemd unit: ufw / firewalld",
            description="Both ufw and firewalld services stopped and disabled; iptables INPUT default ACCEPT",
        ),
    ],
)
