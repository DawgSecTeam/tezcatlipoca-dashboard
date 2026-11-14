#!/usr/bin/env python3
"""
Check NXC access to all boxes in the inventory.

  Windows  → SMB, per-team domain credentials
  Linux    → SSH, shared root credentials
  pfSense  → SSH, shared admin credentials

Exits 0 if all boxes are accessible, 1 if any failed or were unreachable.
"""

import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# INVENTORY_FILE env var (from .env, the single source of truth — see
# scripts/run.sh) overrides the default so this tool checks/reboots whatever
# inventory the backend is actually running against (e.g. live-fire boxes),
# not always the generic dev inventory.
INVENTORY_PATH = Path(os.environ["INVENTORY_FILE"]) if os.environ.get("INVENTORY_FILE") else REPO_ROOT / "config" / "inventory.yml"

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

NXC_TIMEOUT = 120  # seconds per group


def _dial(box: dict) -> str:
    """Address to DIAL: a firewall's wan_ip if set, else its LAN ip. nxc prefixes
    every output line with the DIALED ip, so the demux below (targets + ip_to_name)
    keys on _dial() too — keying on the LAN ip while dialing WAN would drop every
    firewall result (docs/PLAN_firewall_wan_dial_standalone.md)."""
    return box.get("wan_ip") or box["ip"]


def _run_nxc(protocol: str, targets: list[str], username: str, password: str) -> str:
    cmd = ["nxc", protocol, *targets, "-u", username, "-p", password]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=NXC_TIMEOUT)
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        print(f"{RED}nxc not found — is NetExec installed and on PATH?{RESET}")
        sys.exit(1)


def _parse(output: str, target_ips: set[str]) -> tuple[set[str], set[str], set[str]]:
    """Return (success, auth_failed, unreachable) IP sets from NXC output."""
    success: set[str] = set()
    auth_failed: set[str] = set()
    seen: set[str] = set()

    for line in output.splitlines():
        parts = line.split()
        # NXC output: PROTOCOL   IP   PORT   HOSTNAME   [+/-/!/⚠] ...
        if len(parts) < 5:
            continue
        ip = parts[1]
        if ip not in target_ips:
            continue
        seen.add(ip)
        if "[+]" in line:
            success.add(ip)
        elif "[-]" in line or "[!]" in line:
            auth_failed.add(ip)

    unreachable = target_ips - seen
    return success, auth_failed, unreachable


def _check_group(header: str, protocol: str, boxes: list[dict],
                 username: str, password: str) -> tuple[int, int]:
    """Run NXC for a group, print per-box results. Returns (passed, failed)."""
    if not boxes:
        return 0, 0

    targets    = [_dial(b) for b in boxes]
    ip_to_name = {_dial(b): b["name"] for b in boxes}

    print(f"\n{BOLD}{header}{RESET}")
    print("─" * len(header))

    output = _run_nxc(protocol, targets, username, password)
    success, auth_failed, unreachable = _parse(output, set(targets))

    for ip in sorted(success):
        print(f"  {GREEN}[+]{RESET} {ip_to_name[ip]:<22} {ip}")
    for ip in sorted(auth_failed):
        print(f"  {RED}[-]{RESET} {ip_to_name[ip]:<22} {ip}  — auth failed")
    for ip in sorted(unreachable):
        print(f"  {YELLOW}[?]{RESET} {ip_to_name[ip]:<22} {ip}  — unreachable")

    return len(success), len(auth_failed) + len(unreachable)


def main() -> None:
    inventory = yaml.safe_load(INVENTORY_PATH.read_text())
    teams     = inventory["teams"]

    total_pass = 0
    total_fail = 0

    # ── Windows (SMB, per-team domain creds) ──────────────────────────────────
    print(f"\n{BOLD}{'='*50}{RESET}")
    print(f"{BOLD}  WINDOWS (SMB){RESET}")
    print(f"{BOLD}{'='*50}{RESET}")

    for team in teams:
        win_boxes = [b for b in team["boxes"] if b["os"].lower() == "windows"]
        if not win_boxes:
            continue
        domain_user = win_boxes[0]["domain_username"]
        domain_pass = win_boxes[0]["domain_password"]
        header = f"Team {team['team_num']}  ({domain_user})"
        p, f = _check_group(header, "smb", win_boxes, domain_user, domain_pass)
        total_pass += p
        total_fail += f

    # ── Linux (SSH) ────────────────────────────────────────────────────────────
    linux_boxes = [b for t in teams for b in t["boxes"] if b["os"].lower() == "linux"]
    if linux_boxes:
        print(f"\n{BOLD}{'='*50}{RESET}")
        print(f"{BOLD}  LINUX (SSH){RESET}")
        print(f"{BOLD}{'='*50}{RESET}")
        p, f = _check_group(
            f"All Linux  ({linux_boxes[0]['username']})",
            "ssh", linux_boxes,
            linux_boxes[0]["username"], linux_boxes[0]["password"],
        )
        total_pass += p
        total_fail += f

    # ── pfSense (SSH) ──────────────────────────────────────────────────────────
    fw_boxes = [b for t in teams for b in t["boxes"] if b["os"].lower() == "firewall"]
    if fw_boxes:
        print(f"\n{BOLD}{'='*50}{RESET}")
        print(f"{BOLD}  PFSENSE (SSH){RESET}")
        print(f"{BOLD}{'='*50}{RESET}")
        p, f = _check_group(
            f"All pfSense  ({fw_boxes[0]['username']})",
            "ssh", fw_boxes,
            fw_boxes[0]["username"], fw_boxes[0]["password"],
        )
        total_pass += p
        total_fail += f

    # ── Summary ────────────────────────────────────────────────────────────────
    total = total_pass + total_fail
    print(f"\n{'='*50}")
    print(f"  {GREEN}{total_pass}/{total} accessible{RESET}", end="")
    if total_fail:
        print(f"   {RED}{total_fail} failed or unreachable{RESET}")
    else:
        print(f"   {GREEN}all clear{RESET}")
    print(f"{'='*50}\n")

    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()
