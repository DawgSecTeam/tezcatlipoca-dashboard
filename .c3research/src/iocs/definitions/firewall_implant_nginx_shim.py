from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import NginxShimRC

firewall_implant_nginx_shim = IOC(
    name="firewall_implant_nginx_shim",
    description="Sliver beacon spawned via nginx LD_PRELOAD shim on pfSense (FreeBSD)",
    category="remote_access",
    difficulty=2,
    os="firewall",
    check=SliverCheck(preferred_type="beacon"),
    implant_spec=ImplantSpec(is_beacon=True, persistence_name="NginxShimRC"),
    deploy_script="deploy_scripts/firewall/firewall_implant_nginx_shim_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_implant_nginx_shim_remediate.sh",
    persistence=NginxShimRC,
    payloads=["beacon_freebsd"],
    discovery="Check /etc/rc.local for LD_PRELOAD; check for beacon process; check Sliver sessions",
    detection_steps=[
        "Check for the persistence anchor: `cat /etc/rc.local` — pfSense ships no "
        "/etc/rc.local by default, so the file's mere presence is anomalous. A positive "
        "hit contains a line invoking `nginx -v` with "
        "`LD_PRELOAD=/usr/local/lib/libssl_util.so`; /etc/rc.d/local sources rc.local "
        "at the end of every boot, which re-spawns the beacon via the shim SO constructor.",
        "Confirm the shim library is present on disk: "
        "`ls -la /usr/local/lib/libssl_util.so`.",
        "Look for the running beacon process (p_comm matches the binary's basename): "
        "`pgrep -l .sysdb` — a match means the beacon is currently active in memory.",
        "Verify the beacon binary is on disk: `ls -la /usr/local/bin/.sysdb`.",
        "In the Sliver C2 console, run `beacons` and look for a FreeBSD beacon calling back "
        "from the pfSense management IP — this is the session the scoring system tracks.",
    ],
    remediation_steps=[
        "Kill the running beacon process before removing files so it does not hold the binary "
        "open: `pkill -9 '.sysdb'`; wait a second and confirm `pgrep .sysdb` returns nothing.",
        "Remount the root filesystem read-write (pfSense mounts / read-only): "
        "`/sbin/mount -uw /`.",
        "Strip the persistence line from /etc/rc.local so the shim is not re-spawned on the "
        "next boot: `sed -i '' '\\|LD_PRELOAD=/usr/local/lib/libssl_util.so|d' /etc/rc.local`. "
        "If rc.local now holds only `#!/bin/sh` (or is empty), delete the file — pfSense ships "
        "no rc.local by default, so an orphan file is itself an IOC: `rm -f /etc/rc.local`.",
        "Delete the shim library and beacon binary: "
        "`rm -f /usr/local/lib/libssl_util.so /usr/local/bin/.sysdb`.",
        "Wait one scoring cycle and confirm `grep -F LD_PRELOAD=/usr/local/lib/libssl_util.so "
        "/etc/rc.local` returns no output (or the file is gone) and `pgrep .sysdb` finds no "
        "process — both must be clean for the IOC to score as remediated.",
    ],
    artifacts=[
        Artifact(
            path="/usr/local/bin/.sysdb",
            description="Sliver beacon binary (FreeBSD amd64); hidden name mimics sysdb utility",
        ),
        Artifact(
            path="/usr/local/lib/libssl_util.so",
            description="LD_PRELOAD shim SO; constructor forks beacon at nginx load time",
        ),
        Artifact(
            path="/etc/rc.local",
            description="Boot line `LD_PRELOAD=/usr/local/lib/libssl_util.so /usr/local/sbin/nginx -v` "
            "sourced by /etc/rc.d/local at end of boot; this is the persistence trigger. pfSense "
            "ships no rc.local by default, so the file itself is anomalous.",
        ),
        Artifact(
            dynamic=True,
            description="Live Sliver beacon in C2 sessions list; name is build-specific "
            "and assigned at implant generation time",
        ),
    ],
)
