#!/usr/bin/env python3
"""
Linux distro compatibility setup + fingerprinting for the IOC compat matrix.

Prepares the distro test VMs (config/test_distro_inventory.yml) into a known-good
baseline so that when an IOC deploy/check/remediate fails, it is a *script*
incompatibility rather than a missing prerequisite — and fingerprints each box so
those failures can be correlated with the environment (kernel, libc, filesystem,
SELinux/AppArmor, package manager, tool availability).

Modes:
    --fingerprint   Read-only recon. Print per-box facts + derived risk flags.
                    This is the primary "identify the compatibility issues" pass.
    --setup         Apply the baseline prep (mutating). Idempotent. Covers the
                    TODO "Linux distro compatibility test VMs" infra-setup steps:
                      * SSH root password auth
                      * SELinux -> permissive (RHEL family)
                      * AppArmor -> complain (Debian family)
                      * python3 present (installs on CentOS 7 etc.)
                      * SFTP baseline drop-in (distro-correct sftp-server path)
    --install-tools With --setup, also install IOC prerequisites that may be
                    absent on minimal images (e2fsprogs/libcap/cron/curl/wget).
                    OFF by default so their absence stays a fingerprint finding.

If neither --fingerprint nor --setup is given, defaults to --fingerprint.

Usage:
    python scripts/infra/compat_setup.py --fingerprint
    python scripts/infra/compat_setup.py --setup
    python scripts/infra/compat_setup.py --setup --install-tools
    python scripts/infra/compat_setup.py --fingerprint --setup        # before + apply
    python scripts/infra/compat_setup.py --setup --dry-run
    python scripts/infra/compat_setup.py --fingerprint --team 11
    python scripts/infra/compat_setup.py --inventory config/other.yml --fingerprint
"""

import argparse
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_INVENTORY = REPO_ROOT / "config" / "test_distro_inventory.yml"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

NXC_TIMEOUT = 240  # package installs on slow EOL mirrors can be slow

FP_START = "###C3RFP_START###"
FP_END = "###C3RFP_END###"

# Binaries the IOC suite / SA / delivery layer depend on. Presence is reported
# per box; absence drives the risk flags in derive_risks().
PROBE_BINS = [
    "debugfs", "setcap", "getcap", "chattr", "lsattr", "curl", "wget",
    "gcc", "crontab", "ss", "netstat", "iptables", "nft", "journalctl", "base64",
]

_NXC_PREFIX = re.compile(r"^\w+\s+[\d.:]+\s+\d+\s+\S+\s+")


# ───────────────────────────── remote exec ──────────────────────────────────

def _nxc_exec(ip: str, user: str, password: str, command: str,
              timeout: int = NXC_TIMEOUT) -> tuple[bool, str]:
    """Run `command` on a Linux box over NXC SSH. Returns (reached, raw_output)."""
    args = ["nxc", "ssh", ip, "-u", user, "-p", password, "-x", command]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out = r.stdout + r.stderr
        # "[-]" on the auth line (no "[+]") means we never got a shell.
        reached = "[+]" in out
        return reached, out
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except FileNotFoundError:
        print(f"{RED}nxc not found — is NetExec installed and on PATH?{RESET}")
        sys.exit(1)


def _strip_nxc(raw: str) -> list[str]:
    """Strip the per-line `SSH <ip> <port> <host>` prefix nxc adds to -x output."""
    return [_NXC_PREFIX.sub("", ln).rstrip() for ln in raw.splitlines()]


# ───────────────────────────── fingerprint ──────────────────────────────────

def _fingerprint_snippet() -> str:
    bins = " ".join(PROBE_BINS)
    return f"""
echo '{FP_START}'
. /etc/os-release 2>/dev/null
echo "distro=${{ID:-unknown}}"
echo "version=${{VERSION_ID:-?}}"
echo "pretty=${{PRETTY_NAME:-?}}"
echo "id_like=${{ID_LIKE:-}}"
echo "kernel=$(uname -r)"
echo "arch=$(uname -m)"
echo "glibc=$(ldd --version 2>/dev/null | head -1 | awk '{{print $NF}}')"
echo "root_fs=$(df -T / 2>/dev/null | tail -1 | awk '{{print $2}}')"
echo "selinux=$(getenforce 2>/dev/null || echo none)"
if command -v aa-status >/dev/null 2>&1 && aa-status --enabled >/dev/null 2>&1; then
  echo "apparmor=enabled"
else
  echo "apparmor=absent"
fi
if command -v apt-get >/dev/null 2>&1; then echo "pkgmgr=apt"
elif command -v dnf >/dev/null 2>&1; then echo "pkgmgr=dnf"
elif command -v yum >/dev/null 2>&1; then echo "pkgmgr=yum"
else echo "pkgmgr=?"; fi
echo "python3=$(command -v python3 2>/dev/null || echo MISSING)"
echo "python3_ver=$(python3 --version 2>&1 | awk '{{print $2}}')"
echo "python2=$(command -v python2 2>/dev/null || command -v python 2>/dev/null || echo -)"
for b in {bins}; do
  if command -v "$b" >/dev/null 2>&1; then echo "bin_$b=1"; else echo "bin_$b=0"; fi
done
sftp=$(command -v sftp-server 2>/dev/null || ls /usr/libexec/openssh/sftp-server /usr/lib/openssh/sftp-server /usr/lib/ssh/sftp-server 2>/dev/null | head -1)
echo "sftp_server=${{sftp:-MISSING}}"
if grep -qiE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\\.d' /etc/ssh/sshd_config 2>/dev/null; then
  echo "sshd_include=yes"
else
  echo "sshd_include=no"
fi
cron_svc=none
systemctl is-enabled cron >/dev/null 2>&1 && cron_svc=cron
systemctl is-enabled crond >/dev/null 2>&1 && cron_svc=crond
echo "cron_service=$cron_svc"
if [ -d /var/spool/cron/crontabs ]; then echo "cron_spool=/var/spool/cron/crontabs"
elif [ -d /var/spool/cron ]; then echo "cron_spool=/var/spool/cron"
else echo "cron_spool=?"; fi
if [ -f /var/log/auth.log ]; then echo "auth_log=/var/log/auth.log"
elif [ -f /var/log/secure ]; then echo "auth_log=/var/log/secure"
else echo "auth_log=?"; fi
echo '{FP_END}'
"""


def parse_fingerprint(raw: str) -> dict[str, str] | None:
    """Pull key=value pairs emitted between the FP sentinels. None if not reached."""
    lines = _strip_nxc(raw)
    if FP_START not in lines or FP_END not in lines:
        return None
    facts: dict[str, str] = {}
    inside = False
    for ln in lines:
        if ln == FP_START:
            inside = True
            continue
        if ln == FP_END:
            break
        if inside and "=" in ln:
            k, v = ln.split("=", 1)
            facts[k.strip()] = v.strip()
    return facts


def kernel_supports_memfd(kernel: str) -> bool:
    """memfd_create needs kernel >= 3.17 (CentOS 7 ships 3.10 -> False)."""
    m = re.match(r"(\d+)\.(\d+)", kernel or "")
    if not m:
        return True  # unknown -> don't raise a false blocker
    major, minor = int(m.group(1)), int(m.group(2))
    return (major, minor) >= (3, 17)


def derive_risks(facts: dict[str, str]) -> list[tuple[str, str]]:
    """Map fingerprint facts to (severity, message) compatibility flags."""
    risks: list[tuple[str, str]] = []

    if not kernel_supports_memfd(facts.get("kernel", "")):
        risks.append((
            "CRIT",
            f"kernel {facts.get('kernel')} < 3.17 — memfd_create unavailable; "
            "LD_PRELOAD memfd shim _exit(2)s, Sliver beacon won't spawn",
        ))

    if facts.get("selinux", "").lower() == "enforcing":
        risks.append((
            "HIGH",
            "SELinux enforcing — LD_PRELOAD into sshd_t likely denied; "
            "run --setup to set permissive",
        ))

    if facts.get("python3") == "MISSING":
        risks.append((
            "HIGH",
            "python3 absent — python3-dependent IOCs fail "
            "(linux_fake_false, *_http_server); --setup installs it",
        ))

    if facts.get("root_fs") == "xfs":
        risks.append((
            "MED",
            "/ is XFS — timestomp btime skipped (mtime+atime only); "
            "confirm scoring accepts btime=null",
        ))

    if facts.get("bin_debugfs") == "0":
        risks.append(("MED", "debugfs absent (e2fsprogs) — btime timestomp unavailable on ext4"))

    if facts.get("bin_setcap") == "0" or facts.get("bin_getcap") == "0":
        risks.append(("MED", "setcap/getcap absent (libcap) — linux_mega_cap deploy/check fails"))

    if facts.get("bin_chattr") == "0":
        risks.append(("MED", "chattr absent — immutable-flag IOCs (kill_shim, udev_uinput) can't set +i"))

    if facts.get("sftp_server") == "MISSING":
        risks.append(("MED", "no sftp-server binary — linux_sftp_server + NXC put_file may fail"))

    if facts.get("bin_curl") == "0" and facts.get("bin_wget") == "0":
        risks.append(("LOW", "neither curl nor wget — script-wrapper fetch falls back to base64 inline"))

    if facts.get("bin_iptables") == "0" and facts.get("bin_nft") == "1":
        risks.append(("LOW", "nftables-only (no iptables) — SA iptables-save returns empty"))

    return risks


_SEV_ORDER = {"CRIT": 0, "HIGH": 1, "MED": 2, "LOW": 3}
_SEV_COLOR = {"CRIT": RED, "HIGH": RED, "MED": YELLOW, "LOW": DIM}


def fingerprint_box(box: dict) -> tuple[dict, dict[str, str] | None]:
    reached, raw = _nxc_exec(box["ip"], box["username"], box["password"],
                             _fingerprint_snippet())
    return box, (parse_fingerprint(raw) if reached else None)


def run_fingerprint(boxes: list[dict], workers: int) -> int:
    print(f"\n{BOLD}{'='*78}{RESET}")
    print(f"{BOLD}  FINGERPRINT — {len(boxes)} box(es){RESET}")
    print(f"{BOLD}{'='*78}{RESET}\n")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fingerprint_box, b): b for b in boxes}
        results: list[tuple[dict, dict[str, str] | None]] = [
            fut.result() for fut in as_completed(futures)
        ]

    results.sort(key=lambda r: r[0]["name"])

    # ── summary table ────────────────────────────────────────────────────────
    hdr = f"  {'BOX':<16}{'DISTRO':<18}{'KERNEL':<22}{'FS':<7}{'SELINUX':<11}{'PY3':<5}"
    print(BOLD + hdr + RESET)
    print(f"  {'─'*74}")
    for box, facts in results:
        name = box["name"]
        if facts is None:
            print(f"  {name:<16}{RED}unreachable / no shell{RESET}")
            continue
        distro = f"{facts.get('distro','?')} {facts.get('version','?')}"
        kernel = facts.get("kernel", "?")
        if len(kernel) > 21:
            kernel = kernel[:20] + "…"
        fs = facts.get("root_fs", "?")
        se = facts.get("selinux", "?")
        py3 = "yes" if facts.get("python3", "MISSING") != "MISSING" else f"{RED}NO{RESET}"
        se_col = RED if se.lower() == "enforcing" else RESET
        print(f"  {name:<16}{distro:<18}{kernel:<22}{fs:<7}{se_col}{se:<11}{RESET}{py3:<5}")

    # ── per-box risk flags ───────────────────────────────────────────────────
    print(f"\n{BOLD}  RISK FLAGS{RESET}")
    print(f"  {'─'*74}")
    any_crit = False
    for box, facts in results:
        name = box["name"]
        if facts is None:
            print(f"\n  {BOLD}{name}{RESET}  ({box['ip']})")
            print(f"    {RED}● unreachable — fingerprint failed{RESET}")
            any_crit = True
            continue
        risks = sorted(derive_risks(facts), key=lambda r: _SEV_ORDER[r[0]])
        print(f"\n  {BOLD}{name}{RESET}  ({box['ip']})  {DIM}{facts.get('pretty','')}{RESET}")
        if not risks:
            print(f"    {GREEN}● no compatibility flags — full support expected{RESET}")
            continue
        for sev, msg in risks:
            if sev == "CRIT":
                any_crit = True
            col = _SEV_COLOR[sev]
            print(f"    {col}● [{sev}]{RESET} {msg}")

    print(f"\n{BOLD}{'='*78}{RESET}")
    print(f"{DIM}  Tip: run with --setup to clear SELinux/AppArmor/python3/sftp prerequisites,{RESET}")
    print(f"{DIM}  then run the IOC suite and record results in docs/COMPAT_MATRIX.md.{RESET}")
    print(f"{BOLD}{'='*78}{RESET}\n")
    return 1 if any_crit else 0


# ─────────────────────────────── setup ──────────────────────────────────────

def _setup_snippet(install_tools: bool) -> str:
    base = """
. /etc/os-release 2>/dev/null
FAMILY=rhel
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "*|*" ubuntu "*) FAMILY=debian ;;
esac
echo "STEP:family:ok:$FAMILY ${ID:-?} ${VERSION_ID:-?}"

# ── SSH root password auth ────────────────────────────────────────────────
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null
grep -qE '^PasswordAuthentication yes' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config 2>/dev/null
grep -qE '^PermitRootLogin yes' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config
echo "STEP:ssh_root:ok:enabled"

# ── SELinux -> permissive (RHEL family) ───────────────────────────────────
if command -v getenforce >/dev/null 2>&1; then
  was=$(getenforce 2>/dev/null)
  setenforce 0 2>/dev/null
  [ -f /etc/selinux/config ] && sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config
  echo "STEP:selinux:ok:was=$was now=$(getenforce 2>/dev/null)"
else
  echo "STEP:selinux:skip:not-present"
fi

# ── AppArmor -> complain (Debian family) ──────────────────────────────────
if command -v aa-status >/dev/null 2>&1 && aa-status --enabled >/dev/null 2>&1; then
  if command -v aa-complain >/dev/null 2>&1; then
    aa-complain /etc/apparmor.d/* >/dev/null 2>&1
    echo "STEP:apparmor:ok:complain-mode"
  else
    echo "STEP:apparmor:skip:aa-complain-absent"
  fi
else
  echo "STEP:apparmor:skip:not-enabled"
fi

# ── python3 present ───────────────────────────────────────────────────────
if command -v python3 >/dev/null 2>&1; then
  echo "STEP:python3:ok:$(command -v python3)"
else
  if command -v dnf >/dev/null 2>&1; then dnf install -y python3 >/dev/null 2>&1
  elif command -v yum >/dev/null 2>&1; then yum install -y python3 >/dev/null 2>&1
  elif command -v apt-get >/dev/null 2>&1; then apt-get update >/dev/null 2>&1; apt-get install -y python3 >/dev/null 2>&1
  fi
  if command -v python3 >/dev/null 2>&1; then echo "STEP:python3:ok:installed $(command -v python3)"
  else echo "STEP:python3:fail:install-failed"; fi
fi

# ── SFTP baseline (only when NONE exists — a duplicate 'Subsystem sftp' is
#    FATAL on OpenSSH <= 8.3, e.g. Fedora 32, and cruft on newer) ───────────
EXISTING_SFTP=$(grep -rhiE '^[[:space:]]*Subsystem[[:space:]]+sftp' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/ 2>/dev/null | head -1)
if [ -n "$EXISTING_SFTP" ]; then
  echo "STEP:sftp_baseline:skip:already-defined"
else
  SFTP=$(command -v sftp-server 2>/dev/null || ls /usr/libexec/openssh/sftp-server /usr/lib/openssh/sftp-server /usr/lib/ssh/sftp-server 2>/dev/null | head -1)
  [ -z "$SFTP" ] && SFTP=internal-sftp
  if grep -qiE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\\.d' /etc/ssh/sshd_config 2>/dev/null; then
    mkdir -p /etc/ssh/sshd_config.d
    printf '# Baseline SFTP configuration\\nSubsystem sftp %s\\n' "$SFTP" > /etc/ssh/sshd_config.d/00-baseline.conf
    echo "STEP:sftp_baseline:ok:dropin $SFTP"
  else
    echo "Subsystem sftp $SFTP" >> /etc/ssh/sshd_config
    echo "STEP:sftp_baseline:ok:mainconfig-appended $SFTP"
  fi
fi
"""

    tools = """
# ── IOC prerequisite tools (opt-in) ───────────────────────────────────────
if command -v dnf >/dev/null 2>&1; then
  dnf install -y e2fsprogs libcap curl wget cronie >/dev/null 2>&1
elif command -v yum >/dev/null 2>&1; then
  yum install -y e2fsprogs libcap curl wget cronie >/dev/null 2>&1
elif command -v apt-get >/dev/null 2>&1; then
  apt-get update >/dev/null 2>&1
  apt-get install -y e2fsprogs libcap2-bin curl wget cron >/dev/null 2>&1
fi
echo "STEP:install_tools:ok:debugfs=$(command -v debugfs >/dev/null 2>&1 && echo 1 || echo 0) setcap=$(command -v setcap >/dev/null 2>&1 && echo 1 || echo 0)"
"""

    reload = """
# ── reload sshd, but VALIDATE FIRST ───────────────────────────────────────
# A SIGHUP reload re-execs sshd; a fatal config error there kills the running
# daemon (a duplicate Subsystem once took Fedora 32 offline). Never reload an
# invalid config, and never blind-restart — leave the working daemon alone.
SSHD_BIN=$(command -v sshd 2>/dev/null || ls /usr/sbin/sshd 2>/dev/null || echo sshd)
if "$SSHD_BIN" -t 2>/dev/null; then
  systemctl reload sshd 2>/dev/null || systemctl reload ssh 2>/dev/null \\
    || service sshd reload 2>/dev/null || service ssh reload 2>/dev/null
  echo "STEP:ssh_reload:ok:validated+reloaded"
else
  echo "STEP:ssh_reload:fail:sshd-config-test-failed-NOT-reloaded"
fi
"""

    return base + (tools if install_tools else "") + reload


def parse_steps(raw: str) -> list[tuple[str, str, str]]:
    """Pull STEP:name:status:detail tuples from setup output."""
    steps: list[tuple[str, str, str]] = []
    for ln in _strip_nxc(raw):
        if ln.startswith("STEP:"):
            parts = ln.split(":", 3)
            if len(parts) >= 3:
                name, status = parts[1], parts[2]
                detail = parts[3] if len(parts) > 3 else ""
                steps.append((name, status, detail))
    return steps


def setup_box(box: dict, install_tools: bool, dry_run: bool) -> tuple[dict, bool, object]:
    snippet = _setup_snippet(install_tools)
    if dry_run:
        return box, True, snippet
    reached, raw = _nxc_exec(box["ip"], box["username"], box["password"], snippet)
    if not reached:
        last = next((ln for ln in reversed(_strip_nxc(raw)) if ln), "no output")
        return box, False, f"unreachable: {last}"
    return box, True, parse_steps(raw)


def run_setup(boxes: list[dict], install_tools: bool, dry_run: bool, workers: int) -> int:
    tag = "  [DRY RUN]" if dry_run else ("  [+install-tools]" if install_tools else "")
    print(f"\n{BOLD}{'='*78}{RESET}")
    print(f"{BOLD}  SETUP — {len(boxes)} box(es){tag}{RESET}")
    print(f"{BOLD}{'='*78}{RESET}")

    if dry_run:
        box, _, snippet = setup_box(boxes[0], install_tools, dry_run=True)
        print(f"\n{DIM}Command applied to each box (shown once):{RESET}\n")
        print(snippet)
        return 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(setup_box, b, install_tools, False): b for b in boxes}
        results = [fut.result() for fut in as_completed(futures)]

    results.sort(key=lambda r: r[0]["name"])
    failed = 0
    for box, reached, payload in results:
        name = box["name"]
        if not reached:
            print(f"\n  {RED}[FAIL]{RESET} {BOLD}{name}{RESET} ({box['ip']}) — {payload}")
            failed += 1
            continue
        steps = payload
        step_fail = any(s[1] == "fail" for s in steps)
        marker = f"{RED}[PART]{RESET}" if step_fail else f"{GREEN}[OK]{RESET}"
        if step_fail:
            failed += 1
        print(f"\n  {marker} {BOLD}{name}{RESET} ({box['ip']})")
        for sname, status, detail in steps:
            col = {"ok": GREEN, "skip": DIM, "fail": RED}.get(status, RESET)
            print(f"      {col}{status:<5}{RESET} {sname:<15} {DIM}{detail}{RESET}")

    print(f"\n{BOLD}{'='*78}{RESET}")
    ok = len(results) - failed
    print(f"  {GREEN}{ok} ok{RESET}" + (f"   {RED}{failed} failed/partial{RESET}" if failed else f"   {GREEN}all clear{RESET}"))
    print(f"{BOLD}{'='*78}{RESET}\n")
    return 1 if failed else 0


# ─────────────────────────────── main ───────────────────────────────────────

def load_linux_boxes(inventory_path: Path, team: int | None) -> list[dict]:
    inventory = yaml.safe_load(inventory_path.read_text())
    teams = inventory.get("teams", [])
    if team is not None:
        teams = [t for t in teams if t.get("team_num") == team]
        if not teams:
            print(f"{RED}No team with team_num={team} in {inventory_path}{RESET}")
            sys.exit(1)
    return [b for t in teams for b in t.get("boxes", []) if b.get("os", "").lower() == "linux"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Compat-VM setup + fingerprinting for the IOC distro matrix")
    ap.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY,
                    help=f"inventory YAML (default {DEFAULT_INVENTORY.relative_to(REPO_ROOT)})")
    ap.add_argument("--fingerprint", action="store_true", help="read-only environment recon + risk flags")
    ap.add_argument("--setup", action="store_true", help="apply baseline prep (mutating, idempotent)")
    ap.add_argument("--install-tools", action="store_true",
                    help="with --setup, also install IOC prerequisite tools (off by default)")
    ap.add_argument("--dry-run", action="store_true", help="with --setup, print the command without running it")
    ap.add_argument("--team", type=int, help="only this team_num")
    ap.add_argument("--workers", type=int, default=10, help="max parallel workers (default 10)")
    args = ap.parse_args()

    if not args.inventory.exists():
        print(f"{RED}Inventory not found: {args.inventory}{RESET}")
        sys.exit(1)

    # Default to the safe read-only pass when nothing chosen.
    do_fingerprint = args.fingerprint or not args.setup
    do_setup = args.setup

    boxes = load_linux_boxes(args.inventory, args.team)
    if not boxes:
        print(f"{YELLOW}No Linux boxes found in {args.inventory}.{RESET}")
        sys.exit(1)

    rc = 0
    if do_fingerprint:
        rc |= run_fingerprint(boxes, args.workers)
    if do_setup:
        rc |= run_setup(boxes, args.install_tools, args.dry_run, args.workers)

    sys.exit(rc)


if __name__ == "__main__":
    main()
