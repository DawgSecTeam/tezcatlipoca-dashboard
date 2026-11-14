#!/usr/bin/env python3
"""
Configure pfSense outbound NAT + inbound firewall so member boxes remain
identifiable by their real LAN IP when they talk to the C2.

Default outbound NAT rewrites every packet leaving the pfSense WAN to the
firewall's own WAN IP. That collapses every team member's identity to the
firewall — script server tokens (HMAC-bound to client IP) come back 401, and
Sliver sessions can't be attributed to the box that owns them.

This script switches each team firewall to Hybrid Outbound NAT, adds a
"do not NAT" rule for LAN -> C2 traffic (preserving the real source IP),
opens a WAN pass rule for C2 -> LAN so Kali-initiated dials aren't dropped,
and unblocks RFC1918 on WAN (default assumes WAN = internet).

The team's LAN /24 is derived from the firewall's own LAN IP (last octet
replaced with 0), so nothing subnet-specific is hardcoded. The C2 IP is
pulled from the inventory's `c2_settings.c2_ip` block.

Usage:
    python scripts/infra/configure_scoring_nat.py [--inventory PATH]
                                                  [--dry-run] [--verify]
                                                  [--team N] [--exclude N ...]
                                                  [--workers N]
"""

import argparse
import base64
import os
import re
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

REPO_ROOT               = Path(__file__).resolve().parent.parent.parent
DEFAULT_INVENTORY_PATH  = REPO_ROOT / "config" / "inventory.yml"
DESCR                   = "Enable box tracking for scoring infra."
DESCR_MATCH             = "box tracking for scoring"  # dedup grep token
SYSTEMD_UNIT_PATH       = Path("/etc/systemd/system/c3rberus-scoring-routes.service")

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"

_PREFIX_RE = re.compile(r"^\w+\s+[\d.:]+\s+\d+\s+\S+\s+")


def _nxc(protocol: str, ip: str, username: str, password: str, command: str) -> tuple[bool, str]:
    args = ["nxc", protocol, ip, "-u", username, "-p", password, "-x", command]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=90)
        output = result.stdout + result.stderr
        success = "[+]" in output and "[-]" not in output.split("[+]")[1] if "[+]" in output else False
        return success, output
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def _strip_nxc(output: str) -> list[str]:
    lines = []
    for line in output.splitlines():
        stripped = _PREFIX_RE.sub("", line).strip()
        if stripped and not stripped.startswith("[") and not stripped.startswith("#<") and not stripped.startswith("<"):
            lines.append(stripped)
    return lines


def subnet_from_lan_ip(lan_ip: str) -> str:
    """Firewall LAN IP -> /24 CIDR. `10.20.30.1` -> `10.20.30.0/24`."""
    octets = lan_ip.strip().split(".")
    if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
        raise ValueError(f"invalid IPv4: {lan_ip!r}")
    octets[3] = "0"
    return ".".join(octets) + "/24"


def _build_php(fw_lan_ip: str, c2_ip: str) -> str:
    subnet = subnet_from_lan_ip(fw_lan_ip)
    # PHP heredoc — pfSense config.xml surgery in one atomic pass.
    #  - Switch outbound NAT to hybrid so manual rules are honored.
    #  - Purge any prior rule tagged by DESCR_MATCH (idempotent re-apply).
    #  - Prepend no-NAT rule LAN -> C2 to the outbound-NAT list.
    #  - Append WAN pass rule C2 -> LAN so Kali-initiated dials return.
    #  - Unset $config['interfaces']['wan']['blockpriv'] (default blocks RFC1918).
    return f"""<?php
require_once('/etc/inc/config.lib.inc');
require_once('/etc/inc/filter.inc');
global $config;
$config = parse_config(true);

if (!isset($config['nat']) || !is_array($config['nat'])) {{ $config['nat'] = []; }}
if (!isset($config['nat']['outbound']) || !is_array($config['nat']['outbound'])) {{ $config['nat']['outbound'] = []; }}
$config['nat']['outbound']['mode'] = 'hybrid';

$existing = $config['nat']['outbound']['rule'] ?? [];
$filtered = array_values(array_filter(
    $existing,
    function($r){{ return strpos($r['descr'] ?? '', '{DESCR_MATCH}') === false; }}
));
$nonat_rule = [
    'source'          => ['network' => '{subnet}'],
    'sourceport'      => '',
    'destination'     => ['address' => '{c2_ip}/32'],
    'dstport'         => '',
    'target'          => '',
    'targetip'        => '',
    'targetip_subnet' => '0',
    'interface'       => 'wan',
    'descr'           => '{DESCR}',
    'nonat'           => '',
    'created'         => ['time' => time(), 'username' => 'C3R'],
];
array_unshift($filtered, $nonat_rule);
$config['nat']['outbound']['rule'] = $filtered;

if (!isset($config['filter']['rule']) || !is_array($config['filter']['rule'])) {{ $config['filter']['rule'] = []; }}
$config['filter']['rule'] = array_values(array_filter(
    $config['filter']['rule'],
    function($r){{ return strpos($r['descr'] ?? '', '{DESCR_MATCH}') === false; }}
));
$wan_pass = [
    'type'        => 'pass',
    'interface'   => 'wan',
    'ipprotocol'  => 'inet',
    'source'      => ['address' => '{c2_ip}'],
    'destination' => ['network' => '{subnet}'],
    'statetype'   => 'keep state',
    'tracker'     => time(),
    'descr'       => '{DESCR} (WAN in)',
];
$config['filter']['rule'][] = $wan_pass;

if (isset($config['interfaces']['wan']['blockpriv'])) {{
    unset($config['interfaces']['wan']['blockpriv']);
}}

write_config('C3rberus scoring NAT config');
filter_configure();
echo "done\\n";
"""


def configure_firewall(
    ip: str, username: str, password: str, fw_lan_ip: str, c2_ip: str, dry_run: bool,
) -> tuple[bool, str]:
    php = _build_php(fw_lan_ip, c2_ip)
    b64 = base64.b64encode(php.encode()).decode()
    cmd = f"echo {b64} | base64 -d | php"
    if dry_run:
        return True, (
            f'[DRY RUN] fw={ip} lan={fw_lan_ip} subnet={subnet_from_lan_ip(fw_lan_ip)} '
            f'c2={c2_ip}\n[DRY RUN] ssh -x "{cmd[:120]}..."'
        )
    return _nxc("ssh", ip, username, password, cmd)


def verify_firewall(
    ip: str, username: str, password: str, fw_lan_ip: str, c2_ip: str,
) -> tuple[bool, str]:
    subnet = subnet_from_lan_ip(fw_lan_ip)
    checks = [
        # Outbound NAT mode
        "grep -E '<mode>hybrid</mode>' /conf/config.xml >/dev/null && echo 'mode=hybrid' || echo 'mode=NOT-hybrid'",
        # Descr string present (rule installed)
        f"grep -c '{DESCR_MATCH}' /conf/config.xml || echo 0",
        # Subnet + c2 IP referenced somewhere in config.xml
        f"grep -c '{subnet}' /conf/config.xml || echo 0",
        f"grep -c '{c2_ip}' /conf/config.xml || echo 0",
        # blockpriv should be absent on WAN
        "grep -A2 '<if>wan</if>' /conf/config.xml | grep -c blockpriv || echo 0",
    ]
    cmd = " ; ".join(checks)
    ok, raw = _nxc("ssh", ip, username, password, cmd)
    if not ok:
        return False, raw
    lines = _strip_nxc(raw)
    # Order matches checks[]. Best-effort — bail cleanly if fewer lines than expected.
    if len(lines) < 5:
        return False, "unexpected verify output:\n" + "\n".join(lines)
    mode, descr_ct, subnet_ct, c2_ct, blockpriv_ct = lines[:5]
    all_ok = (
        mode == "mode=hybrid"
        and descr_ct.isdigit() and int(descr_ct) >= 2  # 1x nat rule + 1x filter rule
        and subnet_ct.isdigit() and int(subnet_ct) >= 1
        and c2_ct.isdigit() and int(c2_ct) >= 1
        and blockpriv_ct.strip() == "0"
    )
    detail = (
        f"outbound {mode}\n"
        f"descr matches: {descr_ct} (want >=2)\n"
        f"subnet {subnet} refs: {subnet_ct} (want >=1)\n"
        f"c2 {c2_ip} refs: {c2_ct} (want >=1)\n"
        f"wan blockpriv: {blockpriv_ct} (want 0)"
    )
    return all_ok, detail


# ---------------------------------------------------------------------------
# Kali-side static routes
# ---------------------------------------------------------------------------
#
# pfSense preserves the member's real LAN source IP once the no-NAT rule is in
# place, but Kali only sees the packet if the return-path network between
# pfSense WAN and Kali is willing to carry `<team_lan>/24`. On a lab where
# Kali is on the same L3 as the WAN, adding a static route
# `<team_lan>/24 via <fw_wan_ip>` is enough. On a WireGuard-tunneled range,
# the tunnel headend must also carry the LAN subnet in the Kali peer's
# `AllowedIPs` — that part lives on the headend, not here.


def _run(cmd: list[str], dry_run: bool) -> tuple[bool, str]:
    if dry_run:
        return True, "[DRY RUN] " + " ".join(cmd)
    # Auto-elevate the ip/systemctl calls when not already root.
    if os.geteuid() != 0 and cmd[0] in ("ip", "systemctl"):
        cmd = ["sudo", "-n", *cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def _iface_for(target_ip: str) -> str | None:
    """Return the Kali interface that would carry a packet to `target_ip`."""
    try:
        r = subprocess.run(["ip", "-o", "route", "get", target_ip],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    # e.g. `10.67.100.1 dev dawgsec src 10.50.50.5 uid 1000 \    cache`
    m = re.search(r"\bdev\s+(\S+)", r.stdout)
    return m.group(1) if m else None


def _wg_tunnel(iface: str) -> bool:
    """True when `iface` is a WireGuard tunnel (or, softly, any POINTOPOINT).

    `wg show` needs CAP_NET_ADMIN, so try sudo -n first (a lab kali usually
    has NOPASSWD); on failure fall back to the POINTOPOINT+NOARP heuristic
    (a strong signal for WG/PPP/other tunnels regardless).
    """
    try:
        link = subprocess.run(["ip", "-o", "link", "show", iface],
                              capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    is_ptp = ("POINTOPOINT" in link) and ("NOARP" in link)
    if not is_ptp:
        return False
    for prefix in (["sudo", "-n", "wg"], ["wg"]):
        try:
            r = subprocess.run([*prefix, "show", iface],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return True
        except FileNotFoundError:
            continue
        except Exception:
            continue
    # wg unusable but interface is POINTOPOINT+NOARP — treat as tunnel
    return True


def _kali_route_plan(teams: list[dict]) -> list[dict]:
    """Return one plan entry per team firewall with wan_ip set."""
    plan: list[dict] = []
    for team in teams:
        fw = next((b for b in team["boxes"] if b["os"].lower() == "firewall"), None)
        if fw is None:
            continue
        wan = fw.get("wan_ip")
        if not wan:
            # No wan_ip means Kali is on the same L2 as the LAN (dev shape) —
            # the LAN is already directly reachable, no route needed.
            continue
        try:
            subnet = subnet_from_lan_ip(fw["ip"])
        except ValueError:
            continue
        iface = _iface_for(wan)
        plan.append({
            "team_num": team.get("team_num"),
            "fw_name":  fw["name"],
            "fw_wan":   wan,
            "subnet":   subnet,
            "iface":    iface,
            "is_wg":    _wg_tunnel(iface) if iface else False,
        })
    return plan


def _systemd_unit_body(plan: list[dict]) -> str:
    lines = [f"/sbin/ip route replace {e['subnet']} via {e['fw_wan']}" for e in plan]
    body = "\n".join(f"ExecStart={ln}" for ln in lines)
    return textwrap.dedent(f"""\
        [Unit]
        Description=C3rberus scoring per-team static routes ({DESCR})
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=oneshot
        RemainAfterExit=yes
        {body}

        [Install]
        WantedBy=multi-user.target
    """)


def configure_kali_routes(teams: list[dict], dry_run: bool, persist: bool) -> tuple[bool, list[str]]:
    plan = _kali_route_plan(teams)
    if not plan:
        return True, ["no team firewalls with wan_ip — nothing to route (dev-shape inventory)"]

    log: list[str] = []
    all_ok = True
    for e in plan:
        tag = f"team {e['team_num']} fw {e['fw_name']} ({e['fw_wan']}) -> {e['subnet']}"
        if e["iface"] is None:
            log.append(f"{YELLOW}[WARN]{RESET} {tag}: no Kali iface reaches {e['fw_wan']} — skipping")
            all_ok = False
            continue
        if e["is_wg"]:
            log.append(
                f"{YELLOW}[NOTE]{RESET} {tag}: reaches via WireGuard iface {e['iface']} — "
                "headend AllowedIPs must include the LAN subnet for return traffic to be accepted"
            )
        ok, out = _run(["ip", "route", "replace", e["subnet"], "via", e["fw_wan"]], dry_run)
        marker = f"{GREEN}[OK]{RESET}" if ok else f"{RED}[FAIL]{RESET}"
        log.append(f"{marker} {tag} dev={e['iface']}  {out}")
        if not ok:
            all_ok = False

    if persist:
        body = _systemd_unit_body(plan)
        if dry_run:
            log.append(f"{YELLOW}[DRY RUN]{RESET} would write {SYSTEMD_UNIT_PATH} and systemctl enable it")
            log.append("--- unit body ---")
            log.extend(body.splitlines())
        else:
            try:
                needs_sudo = os.geteuid() != 0
                if needs_sudo:
                    # Write via `tee` under sudo to place under /etc.
                    proc = subprocess.run(
                        ["sudo", "-n", "tee", str(SYSTEMD_UNIT_PATH)],
                        input=body, capture_output=True, text=True, timeout=10,
                    )
                    ok_w = proc.returncode == 0
                else:
                    SYSTEMD_UNIT_PATH.write_text(body)
                    ok_w = True
                if ok_w:
                    _run(["systemctl", "daemon-reload"], dry_run=False)
                    ok_e, _ = _run(["systemctl", "enable", SYSTEMD_UNIT_PATH.name], dry_run=False)
                    ok_s, _ = _run(["systemctl", "start", SYSTEMD_UNIT_PATH.name], dry_run=False)
                    if ok_e and ok_s:
                        log.append(f"{GREEN}[OK]{RESET} persisted {SYSTEMD_UNIT_PATH}, enabled + started")
                    else:
                        log.append(f"{RED}[FAIL]{RESET} systemctl enable/start returned nonzero")
                        all_ok = False
                else:
                    log.append(f"{RED}[FAIL]{RESET} could not write {SYSTEMD_UNIT_PATH}")
                    all_ok = False
            except Exception as e:
                log.append(f"{RED}[FAIL]{RESET} persist error: {e}")
                all_ok = False

    return all_ok, log


def verify_kali_routes(teams: list[dict]) -> tuple[bool, list[str]]:
    plan = _kali_route_plan(teams)
    if not plan:
        return True, ["no team firewalls with wan_ip — nothing to verify"]

    try:
        routes = subprocess.run(["ip", "-o", "route", "show"],
                                capture_output=True, text=True, timeout=5).stdout
    except Exception as e:
        return False, [f"could not read routes: {e}"]

    log: list[str] = []
    all_ok = True
    for e in plan:
        # `10.20.30.0/24 via 10.67.100.1 dev ...` — via clause is what we care about
        pattern = re.compile(
            rf"^{re.escape(e['subnet'])}\s+via\s+{re.escape(e['fw_wan'])}\b", re.MULTILINE
        )
        present = pattern.search(routes) is not None
        marker = f"{GREEN}[OK]{RESET}" if present else f"{RED}[MISSING]{RESET}"
        log.append(f"{marker} route {e['subnet']} via {e['fw_wan']}")
        if not present:
            all_ok = False
    return all_ok, log


def load_inventory(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def c2_ip_from_inventory(inv: dict) -> str:
    c2 = (inv.get("c2_settings") or {}).get("c2_ip")
    if not c2:
        raise SystemExit("inventory missing c2_settings.c2_ip")
    return c2


def firewall_boxes(teams: list[dict]) -> list[tuple[dict, dict]]:
    """List of (team, firewall_box). Skips teams with no firewall."""
    out = []
    for team in teams:
        fw = next((b for b in team["boxes"] if b["os"].lower() == "firewall"), None)
        if fw is None:
            print(f"{YELLOW}[WARN] Team {team.get('name', team.get('team_num'))} has no firewall — skipping.{RESET}")
            continue
        out.append((team, fw))
    return out


def run(
    teams: list[dict], c2_ip: str, mode: str, dry_run: bool, workers: int,
) -> None:
    items = firewall_boxes(teams)
    label = "Verifying" if mode == "verify" else "Configuring"
    tag   = "  [DRY RUN]" if dry_run else ""
    print(f"{label} scoring NAT on {len(items)} firewall(s){tag}...\n")

    results: list[tuple[str, bool, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for team, fw in items:
            label_str = f"team {team.get('team_num', '?')} fw {fw['name']} ({fw['ip']})"
            if mode == "verify":
                fut = executor.submit(verify_firewall, fw["ip"], fw["username"], fw["password"], fw["ip"], c2_ip)
            else:
                fut = executor.submit(configure_firewall, fw["ip"], fw["username"], fw["password"], fw["ip"], c2_ip, dry_run)
            futures[fut] = label_str

        for future in as_completed(futures):
            name = futures[future]
            ok, detail = future.result()
            results.append((name, ok, detail))
            color  = GREEN if ok else RED
            status = ("MATCH" if ok else "MISMATCH") if mode == "verify" else ("OK" if ok else "FAIL")
            print(f"  {color}[{status}]{RESET} {name}")
            if mode == "verify" or not ok or dry_run:
                for line in detail.strip().splitlines():
                    print(f"        {line}")

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    word = "matched" if mode == "verify" else "succeeded"
    bad  = "mismatched" if mode == "verify" else "failed"
    print(f"\n{GREEN}{passed} {word}{RESET}, {RED}{failed} {bad}{RESET}")
    if failed:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Configure pfSense outbound NAT + WAN allow-in so member boxes stay "
            "identifiable by their real LAN IP when talking to the C2. Default "
            "(no flags): apply changes, then verify."
        )
    )
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY_PATH,
                        help=f"Inventory YAML (default: {DEFAULT_INVENTORY_PATH.relative_to(REPO_ROOT)})")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing; skip verify")
    parser.add_argument("--verify",  action="store_true", help="Only verify current pfSense state without changes")
    parser.add_argument("--team",    type=int,            help="Only process this team_num")
    parser.add_argument("--exclude", type=int, nargs="+", metavar="TEAM_NUM",
                        help="Exclude one or more team numbers")
    parser.add_argument("--workers", type=int, default=10, help="Max parallel workers (default 10)")
    parser.add_argument("--skip-kali-routes", action="store_true",
                        help="Do not touch Kali's routing table (pfSense-only apply)")
    parser.add_argument("--kali-routes-only", action="store_true",
                        help="Only configure Kali-side routes; skip pfSense apply/verify")
    parser.add_argument("--persist", action="store_true",
                        help="Persist Kali routes via a systemd oneshot unit "
                             f"({SYSTEMD_UNIT_PATH})")
    args = parser.parse_args()

    inv = load_inventory(args.inventory)
    c2_ip = c2_ip_from_inventory(inv)
    teams = inv.get("teams", [])
    if args.team:
        teams = [t for t in teams if t.get("team_num") == args.team]
        if not teams:
            print(f"{RED}No team with team_num={args.team} found.{RESET}")
            sys.exit(1)
    if args.exclude:
        teams = [t for t in teams if t.get("team_num") not in args.exclude]
        if not teams:
            print(f"{RED}No teams remaining after exclusions.{RESET}")
            sys.exit(1)

    print(f"Inventory : {args.inventory}")
    print(f"C2 IP     : {c2_ip}")
    print()

    def _print_route_log(log: list[str]) -> None:
        for line in log:
            print(f"  {line}")

    if args.verify:
        if not args.kali_routes_only:
            run(teams, c2_ip, "verify", dry_run=False, workers=args.workers)
        if not args.skip_kali_routes:
            print("\nKali-side routes:")
            ok, log = verify_kali_routes(teams)
            _print_route_log(log)
            if not ok:
                sys.exit(1)
        return

    if not args.kali_routes_only:
        run(teams, c2_ip, "configure", dry_run=args.dry_run, workers=args.workers)

    if not args.skip_kali_routes:
        print("\nKali-side routes:")
        ok, log = configure_kali_routes(teams, dry_run=args.dry_run, persist=args.persist)
        _print_route_log(log)
        if not ok and not args.dry_run:
            sys.exit(1)

    if args.dry_run:
        return

    if not args.kali_routes_only:
        print()
        run(teams, c2_ip, "verify", dry_run=False, workers=args.workers)

    if not args.skip_kali_routes:
        print("\nKali-side routes (verify):")
        ok, log = verify_kali_routes(teams)
        _print_route_log(log)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
