from iocs.base import IOC, Artifact, ScriptCheck

firewall_implant_webshell = IOC(
    name="firewall_implant_webshell",
    description="PHP webshell in /usr/local/www/ for persistent root RCE via HTTPS",
    category="remote_access",
    difficulty=2,
    os="firewall",
    check=ScriptCheck(
        script="check_scripts/firewall/firewall_implant_webshell_check.sh"
    ),
    discovery="Check /usr/local/www/ for diag_traffic_stats.php",
    deploy_script="deploy_scripts/firewall/firewall_implant_webshell_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_implant_webshell_remediate.sh",
    sa_highlight={"config_changes": ["diag_traffic_stats.php"]},
    detection_steps=[
        "SSH into the pfSense firewall as admin and check for the webshell: "
        "`ls -la /usr/local/www/diag_traffic_stats.php`. A present file confirms the IOC.",
        "Inspect the file contents to verify the malicious PHP: "
        "`cat /usr/local/www/diag_traffic_stats.php`. Look for `passthru($_REQUEST['cmd'])` — "
        "the shell passes any `cmd` query parameter directly to the OS.",
        "Confirm the file is not part of the standard pfSense install by checking the pkg database: "
        "`pkg info -l pfsense-base 2>/dev/null | grep diag_traffic_stats`. "
        "No output (empty result) confirms the file is NOT in any pfSense package — it is the implant.",
        "Review pfSense web-server access logs for exploitation evidence: "
        "`grep diag_traffic_stats /var/log/nginx/access.log` (or lighttpd equivalent). "
        "Requests containing `?cmd=` indicate the shell has been used.",
    ],
    remediation_steps=[
        "Delete the webshell: `rm -f /usr/local/www/diag_traffic_stats.php`.",
        "Verify removal: `ls /usr/local/www/diag_traffic_stats.php` should return "
        "`No such file or directory`. The check script will also return `status=0` on the "
        "next scoring cycle if the file is gone.",
        "Rotate any credentials or secrets that may have been exfiltrated via the shell, "
        "and review firewall rules and pfSense configuration for secondary changes the "
        "attacker may have made through the webshell.",
    ],
    artifacts=[
        Artifact(
            path="/usr/local/www/diag_traffic_stats.php",
            description="PHP webshell masquerading as a pfSense diagnostic page; "
            "executes arbitrary OS commands via passthru($_REQUEST['cmd'])",
        ),
    ],
)
