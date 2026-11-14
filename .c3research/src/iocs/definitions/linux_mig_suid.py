from iocs.base import IOC, Artifact, ScriptCheck

linux_mig_suid = IOC(
    name="linux_mig_suid",
    description="su binary with SUID bit set",
    category="impair_defenses",
    difficulty=1,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_mig_suid_check.sh"),
    discovery=(
        "Check for files with extra permissions set, such as the SUID bit. "
        "The find utility can be used to search for all such files in a given path."
    ),
    deploy_script="deploy_scripts/linux/linux_mig_suid_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_mig_suid_remediate.sh",
    sa_highlight={"config_changes": ["/bin/mig"]},
    detection_steps=[
        "Search for the planted binary: `ls -la /bin/mig /usr/bin/mig 2>/dev/null` "
        "(on modern Linux `/bin` is a symlink to `/usr/bin`, so both paths resolve to the same file).",
        "Confirm the SUID bit is set: `stat -c '%A %n' /usr/bin/mig` — the fourth character of "
        "the permissions field will be `s` (e.g. `-rwsr-xr-x`) when compromised.",
        "Alternatively scan for unexpected SUID binaries across the filesystem: "
        "`find /usr/bin /bin -perm -4000 -type f -ls` and compare against a known-good baseline; "
        "`mig` should not appear.",
        "Verify the file is a copy of `su`: `file /usr/bin/mig` will show an ELF executable, "
        "and `md5sum /usr/bin/mig /bin/su` (or `/usr/bin/su`) will produce matching hashes.",
    ],
    remediation_steps=[
        "Remove the binary from both possible paths: `sudo rm -f /bin/mig /usr/bin/mig` "
        "(removing both is safe — one may be a symlink to the other on modern distros).",
        "Confirm the file is gone: `ls /bin/mig /usr/bin/mig 2>&1` should return "
        "'No such file or directory' for both paths.",
        "Re-run the SUID scan to verify no other unexpected SUID files were added: "
        "`find /usr/bin /bin -perm -4000 -type f -ls`.",
    ],
    artifacts=[
        Artifact(
            path="/bin/mig",
            description="Copy of /bin/su with the SUID bit set; also visible as /usr/bin/mig "
            "on distros where /bin is a symlink to /usr/bin",
        ),
    ],
)
