from iocs.base import IOC, Artifact, ScriptCheck

linux_hide_library = IOC(
    name="linux_hide_library",
    description=(
        "Unified LD_PRELOAD hiding library that hooks fopen/fopen64, recvmsg, "
        "opendir/readdir, unlink/rename, and setxattr to conceal a target process's "
        "network connections, /proc/<pid> entries, service/udev/cron artifacts, and "
        "itself from common enumeration tools. Config is stored as an XOR-encrypted "
        "xattr (user.net_opts) on /etc/machine-id and refreshed at runtime; the hook "
        "validates incoming setxattr calls (blocks invalid blobs) and blocks deletion "
        "of the .so, /etc/ld.so.preload, and the config host. Persists via "
        "/etc/ld.so.preload so every dynamically-linked process loads it. Teaches "
        "defenders that netstat, ss, ps, ls, and systemctl output are not authoritative "
        "when LD_PRELOAD is in play and that xattr storage is a steganographic channel."
    ),
    category="impair_defenses",
    difficulty=4,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_hide_library_check.sh"),
    deploy_script="deploy_scripts/linux/linux_hide_library_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_hide_library_remediate.sh",
    payloads=["hide_library_linux.so"],
    # C3R_SO_NAME = pool-driven .so basename (scored)
    # C3R_DROP_DIR = pool-driven drop directory (scored)
    pooled_names=[("SO_NAME", "so")],
    discovery=(
        "cat /etc/ld.so.preload; "
        "getfattr -n user.net_opts /etc/machine-id 2>/dev/null; "
        "LD_PRELOAD= ss -ltnp | diff - <(ss -ltnp); "
        "LD_PRELOAD= ls /proc | diff - <(ls /proc)"
    ),
    detection_steps=[
        "Check /etc/ld.so.preload for unexpected entries: `cat /etc/ld.so.preload`. "
        "Any path containing a non-standard .so is suspicious.",
        "Check for XOR-encrypted config in the xattr store: "
        "`getfattr -n user.net_opts /etc/machine-id` — presence indicates active config.",
        "Bypass the hook and compare network/process visibility: "
        "`LD_PRELOAD= ss -ltnp` vs `ss -ltnp` and `LD_PRELOAD= ls /proc` vs `ls /proc`. "
        "Discrepancies confirm active hiding.",
        "Inspect the .so with `strings <path> | grep -i HIDDEN_COMM` or "
        "`strings <path> | grep -i _SYS_AUDIT` to confirm hook binary.",
        "Check watched directories for hidden service/udev/cron artifacts: "
        "`LD_PRELOAD= ls /etc/systemd/system /etc/udev/rules.d /etc/cron.d`.",
    ],
    remediation_steps=[
        "Set bypass env var before any cleanup to disable protection hooks: "
        "`export _SYS_AUDIT_TOKEN=a3f2e891-7c4b-4d9a-b5e6-1f2a3c4d5e6f`.",
        "Remove /etc/ld.so.preload or strip the .so path from it: "
        "`sed -i '|<so_path>|d' /etc/ld.so.preload` (or delete the file if empty).",
        "Remove the .so itself: `rm -f <so_path>`.",
        "Remove the xattr config: `setfattr -x user.net_opts /etc/machine-id`.",
        "Verify: `cat /etc/ld.so.preload` (absent/empty), "
        "`getfattr -n user.net_opts /etc/machine-id` (no output).",
    ],
    artifacts=[
        Artifact(
            dynamic=True,
            description="Pool-driven .so dropped to C3R_DROP_DIR/C3R_SO_NAME; "
            "registered in /etc/ld.so.preload for system-wide LD_PRELOAD injection",
        ),
        Artifact(
            path="/etc/ld.so.preload",
            description="Global preload list; contains path to hide_library_linux.so",
        ),
        Artifact(
            path="/etc/machine-id",
            description="Config host; user.net_opts xattr holds XOR-encrypted hide config",
        ),
    ],
    sa_highlight={"processes": ["networkd"]},
)
