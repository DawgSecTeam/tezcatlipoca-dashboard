from iocs.base import HidingEntry, IOC, Artifact, ImplantSpec, SliverCheck
from iocs.persistence import UdevUinput

linux_implant_udev = IOC(
    name="linux_implant_udev",
    description="Linux TCP-pivot session persisted via udev/uinput ticker; dials back through pfSense pivot",
    category="remote_access",
    difficulty=4,
    os="linux",
    check=SliverCheck(preferred_type="session"),
    implant_spec=ImplantSpec(
        # MUST be a session. Sliver pivots are session-only ("Pivots can only be
        # used in 'session mode'" — BishopFox/sliver wiki), and a beacon built
        # against tcppivot:// never connects: it busy-loops at ~99% CPU on the
        # target with no socket open. This IOC ran as a beacon until 2026-08-09
        # and scored ZERO on every team for that reason.
        is_beacon=False,
        persistence_name="UdevUinput",
        apply_immutable=True,
        immutable_scope="binary_and_persistence",
        pivot_set_name="devd_kqueue_pivot",
        jitter=20,
    ),
    persistence=UdevUinput,
    backup_copy=True,
    pooled_names=[("TICKER_NAME", "service")],
    wants_hiding=[HidingEntry(type="process_name", value_env_key="TICKER_NAME")],
    timestomp=True,
    timestomp_paths=[
        "/usr/local/bin/acpi_handler",
        "/etc/udev/rules.d/99-uinput.rules",
    ],
    payloads=["beacon_linux", "acpi_handler"],
    discovery="Check /etc/udev/rules.d/99-uinput.rules; check for acpi_handler process; check Sliver sessions",
    detection_steps=[
        "Check for the udev rule file: `ls -la /etc/udev/rules.d/99-uinput.rules` and inspect "
        "its contents with `cat /etc/udev/rules.d/99-uinput.rules`. The file is set immutable "
        "(`chattr +i`) — confirm with `lsattr /etc/udev/rules.d/99-uinput.rules` (expect `----i`). "
        "Presence of this file is the primary persistence indicator.",
        "Check for the ticker process: `grep -r acpi_handler /proc/*/comm 2>/dev/null` or "
        "`ps aux | grep acpi_handler`. The ticker is a copy of `/bin/sleep` renamed to "
        "`acpi_handler` — both `ps` and `/proc/<pid>/comm` will show `acpi_handler`. "
        "It runs as a long-lived background sleep (`nohup acpi_handler 99999 &`); "
        "no process-name spoofing is performed.",
        "Confirm the ticker binary on disk: `ls -la /usr/local/bin/acpi_handler`. "
        "Check `lsattr /usr/local/bin/acpi_handler` for the immutable flag. "
        "The binary is a copy of `/bin/sleep` renamed so `/proc/<pid>/comm` reads `acpi_handler`.",
        "Look for the live Sliver session process: `ls -la /proc/*/exe 2>/dev/null | grep -v deleted` "
        "and compare against known-good binaries. The Sliver implant is deployed under a "
        "plausible system-daemon name (e.g. `networkd`, `systemd-resolve`, `dbus-broker`) "
        "to `/tmp` or another configured directory. Check `/proc/*/maps` for unusual "
        "executables in `/tmp` or world-writable directories.",
        "Verify the ticker is still active by cross-referencing comm and cmdline: "
        '`for f in /proc/*/comm; do [ "$(cat $f 2>/dev/null)" = acpi_handler ] && '
        'echo "PID: ${f%/comm}"; done`. Confirm the binary is a renamed sleep: '
        "`strings /usr/local/bin/acpi_handler | grep -i sleep` should output sleep strings. "
        "No pidfile is left on disk by this mechanism.",
        "Check Sliver C2 for an active session from this host. In the C3rberus UI navigate to "
        "C2 > Targets and look for a session entry from the box IP. Session presence confirms "
        "the implant is dialing out through the pfSense TCP-pivot.",
    ],
    remediation_steps=[
        "Kill the ticker process by comm name, then remove its immutable flag and binary. "
        'Kill first: `for f in /proc/*/comm; do [ "$(cat $f 2>/dev/null)" = '
        'acpi_handler ] && kill "${f%/comm}" ; done`. '
        "Remove immutable flag before deleting: "
        "`chattr -i /usr/local/bin/acpi_handler && rm -f /usr/local/bin/acpi_handler`.",
        "Remove the immutable flag from the udev rule before deleting it: "
        "`chattr -i /etc/udev/rules.d/99-uinput.rules` then "
        "`rm -f /etc/udev/rules.d/99-uinput.rules` and reload udev: "
        "`udevadm control --reload-rules`.",
        "Locate and kill the Sliver session process. Identify it via "
        "`ls -la /proc/*/exe 2>/dev/null | grep '/tmp/'` or by checking for executables with "
        "plausible daemon names in `/tmp` or `DEPLOY_PATH_POOL_LINUX` paths. Kill the process "
        "and delete the binary: `kill <PID> && rm -f <path>`.",
        "Verify remediation is complete: re-run `ls /etc/udev/rules.d/99-uinput.rules` "
        "(should not exist), `grep -r acpi_handler /proc/*/comm` (no output), and confirm "
        "the Sliver session no longer appears in C3rberus > C2 > Targets. "
        "Wait one persistence interval (~5 min) and recheck — if the rule or process reappears, "
        "the udev rule was not fully removed before udev was reloaded.",
    ],
    artifacts=[
        Artifact(
            path="/etc/udev/rules.d/99-uinput.rules",
            description='Udev rule that sets permissions on /dev/uinput (KERNEL=="uinput", '
            'MODE="0666", GROUP="input"); serves as the persistent indicator — '
            "set immutable with chattr +i by the deploy harness (binary_and_persistence scope)",
        ),
        Artifact(
            path="/usr/local/bin/acpi_handler",
            description="Ticker binary (copy of /bin/sleep renamed); started as a long-running "
            "background sleep (`nohup acpi_handler 99999`); set immutable with chattr +i "
            "by the deploy harness (binary_and_persistence scope)",
        ),
        Artifact(
            dynamic=True,
            description="Sliver session ELF binary dropped under a plausible daemon name "
            "(e.g. networkd, systemd-resolve, dbus-broker) in /tmp or a configured "
            "DEPLOY_PATH_POOL_LINUX directory; name chosen at deploy time from the "
            "linux_binaries naming pool",
        ),
    ],
)
