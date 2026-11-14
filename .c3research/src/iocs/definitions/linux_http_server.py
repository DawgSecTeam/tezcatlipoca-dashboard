from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import HttpServerCron

linux_http_server = IOC(
    name="linux_http_server",
    description="Python HTTP server running on Linux",
    category="exfiltration",
    difficulty=1,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_http_server_check.sh"),
    persistence=HttpServerCron,
    discovery="Check for python http.server process; check port 8080/808x listeners",
    deploy_script="deploy_scripts/linux/linux_http_server_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_http_server_remediate.sh",
    # CRON_NAME scored (persistence check: [ -f /etc/cron.d/$CRON_NAME ]); http.server process check is technique-fixed.
    pooled_names=[("CRON_NAME", "cron")],
    detection_steps=[
        "Check for a running Python HTTP server process: "
        "`pgrep -af 'python.*http\\.server|python.*SimpleHTTP'`. "
        "A match confirms the IOC effect is active.",
        "Confirm which port it is bound to: "
        "`ss -tlnp | grep -E ':808[0-9]'`. "
        "The server is deployed on port 8080 and the check script treats any 808x listener as a hit.",
        "Check for the persistence cron file that acts as a watchdog: "
        "`cat /etc/cron.d/system-http-watchdog`. "
        "It runs every minute as root and restarts the HTTP server if the process is not found.",
        "Also check root's user crontab for a residual entry: `crontab -l`. "
        "Look for any line referencing `http.server` in case the cron.d file was moved.",
        "Verify the server is serving from the filesystem root (`--directory /`), "
        "meaning any readable file on the box is potentially accessible over HTTP.",
    ],
    remediation_steps=[
        "Kill the running Python HTTP server: "
        "`pkill -f 'python.*http\\.server|python.*SimpleHTTP'`. "
        "If the process persists, force-kill with `pkill -9 -f 'python.*http\\.server'`.",
        "Remove the watchdog cron file: `rm -f /etc/cron.d/system-http-watchdog`. "
        "This is the primary persistence path; without it the process will not be restarted.",
        "Check root's user crontab for a backup `http.server` line and remove it if present: "
        "`crontab -e` (or `crontab -l | grep -v http.server | crontab -`).",
        "Wait at least 60 seconds (one cron interval), then re-run "
        "`pgrep -af 'python.*http\\.server'` and `ss -tlnp | grep ':808'`. "
        "If the process reappears, the cron watchdog was not fully removed — "
        "verify both `/etc/cron.d/system-http-watchdog` and root's crontab are clean.",
    ],
    artifacts=[
        Artifact(
            path="/etc/cron.d/system-http-watchdog",
            description="Watchdog cron file (runs every minute as root); restarts python http.server 8080 if killed",
        ),
    ],
)
