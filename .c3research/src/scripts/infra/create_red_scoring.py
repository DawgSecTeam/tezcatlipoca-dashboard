#!/usr/bin/env python3
"""Provision the `red_scoring` infrastructure account on every competition box.

`red_scoring` is the dedicated, operator-hidden credential the C3rberus IOC
pipeline authenticates with (see docs/PLAN_scoring_creds.md). This script CREATES
that account on the live boxes via NXC, with **the same password as each box's
current admin/root account** (read from inventory) so the seeded credential
matches:

  Windows DC     → domain account TEAM<N>\\red_scoring in "Domain Admins"
                   (created on the team DC). PW = domain_password.
  Windows member → the team domain account (above) PLUS a LOCAL red_scoring in
                   local Administrators, so member scoring survives DC flakiness
                   or blue-team deletion of either identity
                   (docs/PLAN_scoring_cred_resilience.md). Local PW = password.
  Linux          → uid-0 root-equivalent local account (NXC runs check scripts
                   directly as the login user — root parity is required). PW = password.
  pfSense        → uid-0 root-equivalent local account (FreeBSD `pw`).  PW = password.

Idempotent: re-running resets the password / re-adds group membership. After
provisioning, every box is re-probed and the result printed.

Usage:
  python scripts/infra/create_red_scoring.py                 # all teams
  python scripts/infra/create_red_scoring.py --team 1 3      # teams 1 and 3
  python scripts/infra/create_red_scoring.py --team 1 --dry-run
  python scripts/infra/create_red_scoring.py --verify-only   # probe, don't create

The account name comes from RED_SCORING_USERNAME (default "red_scoring") or the
inventory `scoring_account.username` stanza.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()  # so RED_SCORING_USERNAME/PASSWORD in .env are visible below,
                # matching what the backend sees (core/config.py also calls this)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INVENTORY_PATH = Path(os.environ.get("INVENTORY_FILE", REPO_ROOT / "config" / "inventory.yml"))

GREEN, RED, YELLOW, BOLD, DIM, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[2m", "\033[0m"
)
NXC_TIMEOUT = 120


def _scoring_username(box: dict | None = None) -> str:
    """Resolve the scoring account username: per-box override, then global default."""
    if box and box.get("scoring_username"):
        return box["scoring_username"]
    inv = yaml.safe_load(INVENTORY_PATH.read_text())
    sa = inv.get("scoring_account") or {}
    return sa.get("username") or os.environ.get("RED_SCORING_USERNAME", "red_scoring")


def _scoring_password(box: dict, *, domain: bool = False) -> str:
    """Resolve the password to SET on the scoring account for this box.

    Three-tier precedence (docs/PLAN_scoring_creds_deploy_manager.md), matching
    orchestrator._seed_scoring_creds exactly so provisioning and backend
    seeding never disagree:
      1. box['scoring_password'] (per-box inventory override)
      2. RED_SCORING_PASSWORD in .env (global default)
      3. this box's own admin/domain password (legacy fallback)
    """
    if box.get("scoring_password"):
        return box["scoring_password"]
    env_pw = os.environ.get("RED_SCORING_PASSWORD", "")
    if env_pw:
        return env_pw
    return box["domain_password"] if domain else box["password"]


def _dial_ip(box: dict) -> str:
    """Address to DIAL a box: its wan_ip if set, else its LAN ip.

    Firewalls (pfSense) are reached at their WAN IP in ranges where the LAN mgmt
    IP is unreachable from this host (docs/PLAN_firewall_wan_dial.md). Normal boxes
    have no wan_ip so this returns box['ip'] unchanged.
    """
    return box.get("wan_ip") or box["ip"]


def _nxc(protocol: str, ip: str, user: str, password: str,
         *, command: str | None = None, extra: list[str] | None = None) -> tuple[bool, str]:
    """Run an nxc auth (or `-x command`) call. Returns (auth_ok, output)."""
    cmd = ["nxc", protocol, ip, "-u", user, "-p", password]
    if extra:
        cmd += extra
    if command is not None:
        cmd += ["-x", command]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=NXC_TIMEOUT)
        out = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return False, "<timeout>"
    except FileNotFoundError:
        print(f"{RED}nxc not found — is NetExec installed and on PATH?{RESET}")
        sys.exit(2)
    auth_ok = "[+]" in out
    # NXC prints "[+] DOM\\user:pass (Guest)" when the credential did NOT map to a
    # real account and the session silently fell back to the Guest token. Auth
    # "succeeds" but the account cannot execute anything, so every IOC check on
    # that box returns status=-1 ("section missing from combined output") and the
    # box scores as if no IOC were ever planted. Never report that as OK.
    if auth_ok and "(Guest)" in out:
        auth_ok = False
    return auth_ok, out


def _dot_local(user: str) -> str:
    r"""Return ``user`` prefixed with ``.\`` so NXC does local-SAM auth WITHOUT the
    ``--local-auth`` flag.

    ``--local-auth`` makes NXC use the box's COMPUTER NAME as the NTLM domain and
    RESOLVE that hostname (``Error resolving hostname C3-WIN25-x-y``) — which fails
    in an offline competition lab (no DNS/WINS for computer names) and manifests as
    ``The NETBIOS connection with the remote host timed out``. The ``.\`` prefix
    pins the domain to ``.`` (literal local) with no name resolution, so it
    authenticates reliably where ``--local-auth`` hangs. Proven: ``.\Administrator``
    Pwn3d! on boxes where ``Administrator --local-auth`` times out 10/10.
    """
    return user if user.startswith(".\\") else f".\\{user}"


def _domain_prefix(domain_username: str | None) -> str | None:
    if domain_username and "\\" in domain_username:
        return domain_username.split("\\", 1)[0]
    return None


def _is_dc(box: dict) -> bool:
    """DC detection: honor an explicit inventory `is_domain_controller` flag,
    else fall back to the `-1`-name heuristic (mirrors db_init's default rule)."""
    if "is_domain_controller" in box:
        return bool(box["is_domain_controller"])
    return str(box.get("name", "")).endswith("-1")


def _is_domain_joined(box: dict) -> bool:
    """Windows boxes default domain-joined unless the inventory says otherwise."""
    if "is_domain_joined" in box:
        return bool(box["is_domain_joined"])
    return box["os"].lower() == "windows"


def _pick_dc(win_boxes: list[dict]) -> dict | None:
    return next((b for b in win_boxes if _is_dc(b)), win_boxes[0] if win_boxes else None)


# ---------------------------------------------------------------------------
# Per-OS provisioning
# ---------------------------------------------------------------------------

def provision_windows_domain(dc_box: dict, account: str, dry_run: bool) -> tuple[bool, str]:
    """Create the domain account + add to Domain Admins, via the team DC.

    Connects (authenticates) as the DC's existing domain admin (``domain_password``)
    but SETS the scoring account's password to ``_scoring_password`` — these differ
    whenever a per-box ``scoring_password`` override or ``.env`` default is set.
    """
    du = dc_box["domain_username"]
    dp_auth = dc_box["domain_password"]
    dp_set = _scoring_password(dc_box, domain=True)
    # `/domain` performs the op against a DC; running on the DC itself is fine.
    steps = [
        f"net user {account} {dp_set} /add /domain",   # create (errors if exists — ok)
        f"net user {account} {dp_set} /domain",        # ensure password matches (idempotent reset)
        f'net group "Domain Admins" {account} /add /domain',
    ]
    if dry_run:
        return True, "  ".join(steps)
    last = ""
    for step in steps:
        ok, out = _nxc("smb", _dial_ip(dc_box), du, dp_auth, command=step)
        last = out
        if not ok:
            return False, out
    return True, last


def provision_windows_local(box: dict, account: str, dry_run: bool) -> tuple[bool, str]:
    r"""Create a LOCAL red_scoring account (local Administrators) on a domain
    MEMBER box, authenticating as the box's local admin via the ``.\`` dot-form.

    Members get a local red_scoring IN ADDITION TO the team domain account so
    scoring survives DC flakiness AND blue-team deletion of either identity
    (docs/PLAN_scoring_cred_resilience.md). The DC itself has no local SAM and
    is handled by ``provision_windows_domain``.
    """
    user = box["username"]
    pw_auth = box["password"]
    pw_set = _scoring_password(box)  # local/tier-3 fallback = box["password"]
    steps = [
        f"net user {account} {pw_set} /add",   # create (errors if exists — ok)
        f"net user {account} {pw_set}",        # ensure password matches (idempotent)
        f"net localgroup administrators {account} /add",
    ]
    if dry_run:
        return True, "  ".join(steps)
    last = ""
    for step in steps:
        # .\-prefixed local auth, NOT --local-auth (which resolves the box's
        # computer name and NETBIOS-times-out in an offline lab — see _dot_local).
        ok, out = _nxc("smb", _dial_ip(box), _dot_local(user), pw_auth, command=step)
        last = out
        if not ok:
            return False, out
    return True, last


def provision_unix_root(box: dict, account: str, dry_run: bool) -> tuple[bool, str]:
    """Create a uid-0 root-equivalent account on Linux / pfSense.

    Connects (authenticates) as the box's existing admin (``box["password"]``)
    but SETS the scoring account's password to ``_scoring_password`` — these
    differ whenever a per-box ``scoring_password`` override or ``.env``
    default is set.
    """
    user = box["username"]
    pw_auth = box["password"]
    pw_set = _scoring_password(box)
    is_fw = box["os"].lower() == "firewall"
    if is_fw:  # FreeBSD / pfSense — root FS is mounted READ-ONLY by default, so
        # `pw` fails with "Read-only file system" unless we remount RW first;
        # restore RO afterwards to match the appliance's original state. pfSense
        # PAM (pam_unix) accepts the uid-0 system user for SSH once it has a
        # master.passwd entry.
        cmd = (
            "/sbin/mount -uw / && "
            f"(pw usershow {account} >/dev/null 2>&1 || "
            f"pw useradd {account} -u 0 -o -g 0 -d /root -s /bin/sh); "
            f"printf '%s' '{pw_set}' | pw usermod {account} -h 0"
        )
    else:  # Linux
        cmd = (
            f"id {account} >/dev/null 2>&1 || "
            f"useradd -o -u 0 -g 0 -M -d /root -s /bin/bash {account}; "
            # Force the shell on every run, not just at creation — an account
            # that pre-existed (base image, earlier partial provisioning) with
            # a non-bash shell (e.g. /bin/sh -> dash) would otherwise keep it
            # forever, since the `id` check above short-circuits useradd. The
            # NXC script-delivery fetch pipes into `bash` explicitly and is
            # invoked through this account's *login* shell over SSH — a
            # dash login shell breaks that pipe silently (see
            # netexec/script_wrapper.py's build_inline_command docstring).
            f"usermod -s /bin/bash {account}; "
            f"printf '%s:%s' '{account}' '{pw_set}' | chpasswd"
        )
    if dry_run:
        return True, cmd
    return _nxc("ssh", _dial_ip(box), user, pw_auth, command=cmd)


def verify(box: dict, account: str) -> tuple[bool, str]:
    """Probe that `account` authenticates with the resolved scoring password.

    Windows domain members carry BOTH a local and a domain red_scoring — the box
    is scoreable if EITHER authenticates, so probe both and pass on the first.
    The DC has only the domain identity; workgroup boxes only the local one.
    """
    os_l = box["os"].lower()
    if os_l == "windows":
        prefix = _domain_prefix(box.get("domain_username"))
        if prefix:
            dlogin = f"{prefix}\\{account}"
            dok, _ = _nxc("smb", _dial_ip(box), dlogin, _scoring_password(box, domain=True))
            if dok:
                return True, dlogin
        # Local identity (members + workgroup) — force local-SAM auth via the
        # .\ dot-form (NOT --local-auth; see _dot_local).
        lok, _ = _nxc("smb", _dial_ip(box), _dot_local(account), _scoring_password(box))
        if lok:
            return True, f"{account} (local)"
        return False, (f"{prefix}\\{account}" if prefix else account)
    pw = _scoring_password(box)
    ok, _ = _nxc("ssh", _dial_ip(box), account, pw)
    return ok, account


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Provision the red_scoring account on competition boxes.")
    ap.add_argument("--team", type=int, nargs="+", help="only these team numbers")
    ap.add_argument("--dry-run", action="store_true", help="print commands, don't execute")
    ap.add_argument("--verify-only", action="store_true", help="only probe red_scoring auth")
    args = ap.parse_args()

    default_account = _scoring_username()
    inv = yaml.safe_load(INVENTORY_PATH.read_text())
    teams = inv["teams"]
    if args.team:
        teams = [t for t in teams if t["team_num"] in set(args.team)]

    print(f"{BOLD}red_scoring provisioning — account '{default_account}' (per-box override may differ) — inventory {INVENTORY_PATH.name}{RESET}")
    if args.dry_run:
        print(f"{YELLOW}DRY RUN — no changes will be made{RESET}")

    created_fail = 0
    verify_fail = 0
    verify_pass = 0

    for team in teams:
        n = team["team_num"]
        boxes = team["boxes"]
        win = [b for b in boxes if b["os"].lower() == "windows"]
        print(f"\n{BOLD}── Team {n} ──{RESET}")

        # Windows: one domain account per team via the DC (covers domain auth on
        # every joined box), PLUS a LOCAL red_scoring on each domain MEMBER so
        # member scoring survives DC flakiness / deletion of either identity
        # (docs/PLAN_scoring_cred_resilience.md).
        if win and not args.verify_only:
            dc = _pick_dc(win)
            ok, out = provision_windows_domain(dc, _scoring_username(dc), args.dry_run)
            tag = (f"{GREEN}[+]{RESET}" if ok else f"{RED}[-]{RESET}")
            print(f"  {tag} domain account via DC {dc['name']} ({dc['ip']})")
            if args.dry_run:
                print(f"      {DIM}{out}{RESET}")
            elif not ok:
                created_fail += 1
                print(f"      {DIM}{out.strip().splitlines()[-1] if out.strip() else ''}{RESET}")

            for b in win:
                if b is dc or not _is_domain_joined(b):
                    continue
                ok, out = provision_windows_local(b, _scoring_username(b), args.dry_run)
                tag = (f"{GREEN}[+]{RESET}" if ok else f"{RED}[-]{RESET}")
                print(f"  {tag} local account   {b['name']:<18} ({b['ip']})")
                if args.dry_run:
                    print(f"      {DIM}{out}{RESET}")
                elif not ok:
                    created_fail += 1
                    print(f"      {DIM}{out.strip().splitlines()[-1] if out.strip() else ''}{RESET}")

        # Linux + pfSense: uid-0 account per box.
        if not args.verify_only:
            for b in boxes:
                if b["os"].lower() in ("linux", "firewall"):
                    ok, out = provision_unix_root(b, _scoring_username(b), args.dry_run)
                    tag = (f"{GREEN}[+]{RESET}" if ok else f"{RED}[-]{RESET}")
                    print(f"  {tag} {b['os']:<8} {b['name']:<18} ({b['ip']})")
                    if args.dry_run:
                        print(f"      {DIM}{out}{RESET}")
                    elif not ok:
                        created_fail += 1
                        print(f"      {DIM}{out.strip().splitlines()[-1] if out.strip() else ''}{RESET}")

        # Verify every box.
        if not args.dry_run:
            for b in boxes:
                ok, login = verify(b, _scoring_username(b))
                if ok:
                    verify_pass += 1
                    print(f"    {GREEN}✓ verify{RESET} {b['name']:<18} ({login})")
                else:
                    verify_fail += 1
                    print(f"    {RED}✗ verify{RESET} {b['name']:<18} ({login}) — red_scoring auth FAILED")

    if args.dry_run:
        return
    print(f"\n{'='*54}")
    print(f"  verify: {GREEN}{verify_pass} ok{RESET}, "
          f"{RED if verify_fail else GREEN}{verify_fail} failed{RESET}"
          f"   |   create errors: {RED if created_fail else GREEN}{created_fail}{RESET}")
    print(f"{'='*54}\n")
    sys.exit(0 if (verify_fail == 0 and created_fail == 0) else 1)


if __name__ == "__main__":
    main()
