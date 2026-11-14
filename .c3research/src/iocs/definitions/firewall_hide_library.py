from iocs.base import IOC, Artifact, ScriptCheck

firewall_hide_library = IOC(
    name="firewall_hide_library",
    description=(
        "Unified BSD hiding library that hooks sysctl() to filter processes and TCP "
        "connections from sockstat/netstat/ps, hooks opendir/readdir to hide service "
        "rc.d scripts and devd/cron entries, and protects itself via an unlink hook "
        "and extattr protection hooks (validates incoming blob writes, blocks deletion "
        "of the config extattr). Activated via /etc/libmap.conf stanza remapping "
        "libcap_sysctl.so.2 for sockstat. Config stored as a USER extattr (net_opts) "
        "on /etc/hostid. Teaches defenders that pfSense built-in tools are not "
        "authoritative under libmap.conf hijacking, that kern.proc output is filterable, "
        "and that FreeBSD extattr storage is a steganographic channel."
    ),
    category="impair_defenses",
    difficulty=4,
    os="firewall",
    check=ScriptCheck(script="check_scripts/firewall/firewall_hide_library_check.sh"),
    deploy_script="deploy_scripts/firewall/firewall_hide_library_deploy.sh",
    remediate_script="remediate_scripts/firewall/firewall_hide_library_remediate.sh",
    payloads=["hide_library_bsd.so"],
    # C3R_SO_NAME = pool-driven .so basename (scored)
    # C3R_DROP_DIR = pool-driven drop directory (scored)
    pooled_names=[("SO_NAME", "so")],
    discovery=(
        "cat /usr/local/etc/libmap.d/*.conf; "
        "getextattr -s user net_opts /etc/hostid 2>/dev/null; "
        "env -i /usr/bin/sockstat -4 | diff - <(sockstat -4); "
        "grep LD_PRELOAD /root/.cshrc"
    ),
    detection_steps=[
        "Check /usr/local/etc/libmap.d/ for foreign stanza files: "
        "`ls /usr/local/etc/libmap.d/`. Any file not from a pfSense package listing "
        "libcap_sysctl.so.2 is the IOC.",
        "Check for XOR-encrypted config in extattr: "
        "`getextattr -s user net_opts /etc/hostid` — presence confirms active config.",
        "Bypass the hook by copying sockstat outside the libmap stanza: "
        "`cp /usr/bin/sockstat /tmp/s && /tmp/s -4` vs `sockstat -4`. "
        "Differences confirm active network hiding.",
        "Confirm kern.proc filtering: run `ps aux` in bypass shell vs normal shell.",
        "Check watched directories for hidden rc.d scripts: "
        "`env -i ls /usr/local/etc/rc.d /etc/rc.d /usr/local/etc/devd`.",
    ],
    remediation_steps=[
        "Set bypass env var: `export _SYS_AUDIT_TOKEN=a3f2e891-7c4b-4d9a-b5e6-1f2a3c4d5e6f`.",
        "Remount root rw: `/sbin/mount -uw /`.",
        "Remove the libmap.d conf file: `rm -f /usr/local/etc/libmap.d/<so_name>.conf`.",
        "Remove the .so: `rm -f <drop_dir>/<so_name>`.",
        "Remove the extattr config: `rmextattr -s user net_opts /etc/hostid`.",
        "Remove LD_PRELOAD from /root/.cshrc if present: `sed -i '' '/LD_PRELOAD/d' /root/.cshrc`.",
    ],
    artifacts=[
        Artifact(
            dynamic=True,
            description="Pool-driven .so dropped to C3R_DROP_DIR/C3R_SO_NAME; "
            "registered via libmap.d stanza and optionally /root/.cshrc LD_PRELOAD",
        ),
        Artifact(
            dynamic=True,
            description="libmap.d conf file at /usr/local/etc/libmap.d/<so_name>.conf; "
            "remaps libcap_sysctl.so.2 → shim for /usr/bin/sockstat",
        ),
        Artifact(
            path="/etc/hostid",
            description="Config host; net_opts USER extattr holds XOR-encrypted hide config",
        ),
    ],
    sa_highlight={"processes": ["syslogd"]},
)
