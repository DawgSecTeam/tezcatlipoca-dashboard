from iocs.base import IOC, Artifact, ScriptCheck

firewall_http_server = IOC(
    name="firewall_http_server",
    description="Python HTTP server running on pfSense",
    category="exfiltration",
    difficulty=1,
    os="firewall",
    check=ScriptCheck(script="check_scripts/firewall/firewall_http_server_check.sh"),
    discovery="Check for python http.server process; check sockstat for port 808x",
    deploy_script="deploy_scripts/firewall/firewall_http_server_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_http_server_remediate.sh",
    sa_highlight={"processes": ["http.server"], "ports": [":8080"]},
    detection_steps=[
        "Check for the running Python HTTP server process: "
        "`pgrep -af 'python.*http\\.server|python.*SimpleHTTP'`. "
        "A match confirms the IOC is active.",
        "Verify the listening port with sockstat: "
        "`sockstat -l | grep ':808'`. "
        "Port 8080 (or another 808x port) open and owned by a python process is the IOC.",
        "Look for the hidden launcher script left on disk: "
        "`ls -la /tmp/.http_srv.sh`. "
        "The deploy script drops this POSIX shell wrapper so the process is reliably detectable "
        "by pgrep.",
        "Confirm no daemon supervisor lingers: `/usr/sbin/daemon -f` forks and exits "
        "immediately, so the python process is the only IOC entry in `ps aux`. "
        "The launch method (`/usr/sbin/daemon -f /bin/sh /tmp/.http_srv.sh`) is recorded "
        "in the wrapper script at `/tmp/.http_srv.sh` — its presence on disk confirms the "
        "deploy vector even after the process is killed.",
    ],
    remediation_steps=[
        "Kill the Python HTTP server: "
        "`pkill -f 'python.*http\\.server'`. "
        "If the process does not die, follow with `pkill -9 -f 'python.*http\\.server'`.",
        "Confirm the process is gone: "
        "`pgrep -f 'python.*http\\.server'` should return no output and exit non-zero.",
        "Remove the hidden launcher script: "
        "`rm -f /tmp/.http_srv.sh`. "
        "Leaving this file does not re-launch the server on its own (no persistence mechanism), "
        "but it is an artifact of the IOC and should be cleaned up.",
        "Verify port 8080 is no longer open: "
        "`sockstat -l | grep ':808'` should return no matches.",
    ],
    artifacts=[
        Artifact(
            path="/tmp/.http_srv.sh",
            description="Hidden POSIX shell wrapper that exec-s the python http.server; "
            "dropped by deploy script and used by daemon to start the server",
        ),
    ],
)
