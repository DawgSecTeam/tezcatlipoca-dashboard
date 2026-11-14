from iocs.base import IOC, Artifact, ScriptCheck
from iocs.persistence import FakeShellDaemonRC

firewall_fake_shell = IOC(
    name="firewall_fake_shell",
    description="Fake system binary in /usr/local/sbin/ redirects to shell; chflags immutable",
    category="persistence",
    difficulty=2,
    os="firewall",
    check=ScriptCheck(script="check_scripts/firewall/firewall_fake_shell_check.sh"),
    persistence=FakeShellDaemonRC,
    # SERVICE_NAME scored: check keys on the fake binary path.
    pooled_names=[("SERVICE_NAME", "service")],
    discovery="Check /usr/local/sbin/sysstats (default); check rc.d for sysstats_svc (default)",
    deploy_script="deploy_scripts/firewall/firewall_fake_shell_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_fake_shell_remediate.sh",
    detection_steps=[
        "Check whether the fake binary exists: `ls -la /usr/local/sbin/sysstats`. "
        "The file should not be present on a stock pfSense installation.",
        "Inspect its content to confirm it is a shell redirect: "
        '`cat /usr/local/sbin/sysstats`. The planted file executes `exec /bin/sh "$@"`, '
        "making it a covert interactive shell launcher disguised as a stats utility.",
        "Verify the immutable flag is set: `ls -lo /usr/local/sbin/sysstats`. "
        "The `schg` (system immutable) flag prevents deletion without first clearing it.",
        "Check for the rc.d persistence script: `ls -la /usr/local/etc/rc.d/sysstats_svc`. "
        "If present, pfSense will re-launch the fake binary as a daemon on every reboot.",
        "Confirm the service is enabled and running: "
        "`service sysstats_svc status` and check `ps aux | grep sysstats`. "
        "The rc.d script uses `/usr/sbin/daemon` to keep the fake binary alive in the background.",
        "Check the rc.d script's immutable flag as well: "
        "`ls -lo /usr/local/etc/rc.d/sysstats_svc`. Both the binary and the rc.d script "
        "carry `chflags schg`, so both must be unlocked before removal.",
    ],
    remediation_steps=[
        "Stop the running service: `service sysstats_svc stop`. "
        "Kill any remaining daemon process: `kill $(cat /var/run/sysstats_svc.pid 2>/dev/null)` "
        "and remove the PID file: `rm -f /var/run/sysstats_svc.pid`.",
        "Remount the pfSense base filesystem read-write (it is read-only by default): "
        "`/sbin/mount -uw /`.",
        "Clear the immutable flags before attempting removal: "
        "`chflags noschg /usr/local/etc/rc.d/sysstats_svc` and "
        "`chflags noschg /usr/local/sbin/sysstats`.",
        "Delete both planted files: `rm -f /usr/local/etc/rc.d/sysstats_svc` and "
        "`rm -f /usr/local/sbin/sysstats`.",
        "Verify removal: `ls /usr/local/sbin/sysstats /usr/local/etc/rc.d/sysstats_svc` "
        "should both return 'No such file or directory'. Reboot and re-check to confirm the "
        "rc.d service does not recreate the binary on startup.",
    ],
    artifacts=[
        Artifact(
            path="/usr/local/sbin/sysstats",
            description="Fake shell script that exec's /bin/sh; marked schg immutable via chflags",
        ),
        Artifact(
            path="/usr/local/etc/rc.d/sysstats_svc",
            description=(
                "BSD rc.d init script that runs the fake binary via /usr/sbin/daemon on boot; "
                "marked schg immutable via chflags"
            ),
        ),
        Artifact(
            path="/var/run/sysstats_svc.pid",
            description="PID file written by /usr/sbin/daemon for the running fake-shell process",
        ),
    ],
)
