from iocs.base import IOC, Artifact, ImplantSpec, SliverCheck

linux_implant_bare = IOC(
    name="linux_implant_bare",
    description="Sliver ELF running as standalone binary only",
    category="remote_access",
    difficulty=1,
    os="linux",
    check=SliverCheck(),
    implant_spec=ImplantSpec(is_beacon=False),
    payloads=["beacon_linux"],
    discovery="Check running processes for unsigned ELF in /tmp or /dev/shm",
    detection_steps=[
        "Scan running processes for the implant binary by resolving `/proc/*/exe` symlinks: "
        '`for p in /proc/[0-9]*; do exe=$(readlink "$p/exe" 2>/dev/null); '
        "echo \"$p $exe\"; done | grep -v '^$'`. "
        "Look for a recently started ELF in `/tmp`, `/dev/shm`, `/var/tmp`, "
        "`/usr/local/bin`, `/var/lib`, or `/usr/lib` that does not belong to any "
        "installed package — the implant binary name is Sliver-generated and "
        "will not match any known system binary.",
        "Check for the binary file on disk in the common deploy directories: "
        "`ls -la /tmp/ /dev/shm/ /var/tmp/ /usr/local/bin/ /var/lib/ /usr/lib/ 2>/dev/null`. "
        "Look for a large statically-linked ELF (typically 8–20 MB) with a recent "
        "modification timestamp that has no matching package owner "
        "(`dpkg -S <path>` on Debian/Ubuntu or `rpm -qf <path>` on RHEL returns "
        "'not found').",
        "Confirm the suspect binary is a Sliver ELF: "
        "`file <path>` should report 'ELF 64-bit LSB executable, x86-64 ... statically linked'. "
        "The binary was launched daemonized — check `ss -tnp` or `lsof -i` for an "
        "outbound mTLS connection (TCP port 8888 by default) originating from the process.",
        "Verify the process is running with no controlling terminal (it was started via "
        "`setsid` or backgrounded with `</dev/null >/dev/null 2>&1 &`): "
        "`ps -p <pid> -o pid,tty,comm,args` — TTY column should show `?`.",
        "Confirm via the C3rberus session list that an active Sliver session originates "
        "from this box. The implant has no persistence mechanism; if the process is "
        "not running the IOC will self-correct on the next check cycle.",
    ],
    remediation_steps=[
        "Identify the implant PID by scanning `/proc` exe symlinks: "
        '`for p in /proc/[0-9]*; do exe=$(readlink "$p/exe" 2>/dev/null); '
        '[ -n "$exe" ] && echo "${p##*/} $exe"; done | grep -vE \'^[0-9]+ /usr/bin|^[0-9]+ /bin|^[0-9]+ /sbin|^[0-9]+ /lib\'`. '
        "Note the PID and the full path of the binary.",
        "Kill the implant process: `sudo kill -9 <pid>`. "
        "As a fallback, kill by binary basename: "
        "`sudo pkill -9 -f <binary_name> 2>/dev/null`. "
        "Confirm the process is gone: `ls /proc/<pid> 2>&1` should report "
        "'No such file or directory'.",
        "Delete the implant binary from disk: `sudo rm -f <full_binary_path>`. "
        "Verify removal: `ls <full_binary_path> 2>&1` should report "
        "'No such file or directory'.",
        "Check for any residual outbound connection: `ss -tnp | grep 8888` (or the "
        "configured mTLS port) — should return no rows after the kill.",
        "Confirm the Sliver session is no longer active in the C3rberus session list. "
        "This IOC has no persistence layer; killing the process and removing the binary "
        "is sufficient — the check will score as remediated on the next cycle.",
    ],
    artifacts=[
        Artifact(
            dynamic=True,
            description=(
                "Sliver ELF implant binary: filename is Sliver-generated at build time "
                "(e.g. ADJECTIVE_NOUN_session_linux_amd64.elf); drop directory chosen "
                "at deploy time from pool: /tmp, /dev/shm, /var/tmp, /usr/local/bin, "
                "/var/lib, /usr/lib"
            ),
        ),
    ],
)
