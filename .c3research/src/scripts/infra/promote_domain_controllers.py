#!/usr/bin/env python3
"""
Promote win1 boxes to Domain Controllers and join win2 boxes to the domain.

Each team's win1 box is sysprepped (new SID), promoted to a forest root DC for
domain TEAMX.C3. win1's DNS forwarder is pointed at pfSense, and win2's DNS
client is pointed at win1 so it can resolve the domain before joining.

Usage:
    python scripts/infra/promote_domain_controllers.py [--dry-run] [--verify]
                                                  [--team TEAM_NUM]
                                                  [--step {sysprep,rename,promote,forwarder,dns,join,all}]
                                                  [--workers N]

Default (no flags): run all steps for all teams, then verify.
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import winrm
import yaml

REPO_ROOT      = Path(__file__).resolve().parent.parent.parent
INVENTORY_PATH = Path(os.environ.get("INVENTORY_FILE", REPO_ROOT / "config" / "inventory.yml"))

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"

SYSPREP_WAIT = 120   # seconds to wait after sysprep reboot
RENAME_WAIT  = 180   # seconds to wait after rename reboot
PROMOTE_WAIT = 300   # seconds to wait after DC promotion reboot
JOIN_WAIT    = 120   # seconds to wait after domain-join reboot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _winrm_session(ip: str, username: str, password: str,
                   op_timeout: int = 60, read_timeout: int = 120) -> winrm.Session:
    return winrm.Session(
        f"http://{ip}:5985/wsman",
        auth=(username, password),
        transport="ntlm",
        operation_timeout_sec=op_timeout,
        read_timeout_sec=read_timeout,
    )


def _clean_ps(raw: bytes) -> list[str]:
    """Return non-empty stdout lines, stopping before any CLIXML block."""
    lines = []
    for line in raw.decode(errors="replace").splitlines():
        s = line.strip()
        if s.startswith("#< CLIXML") or s.startswith("<Objs") or s.startswith("</Objs"):
            break
        if s:
            lines.append(s)
    return lines


def _is_connection_drop(err: str) -> bool:
    low = err.lower()
    return any(k in low for k in ("timed out", "connection", "forcibly closed", "reset"))


def _wait_for_winrm(ip: str, username: str, password: str,
                    timeout: int = 600, interval: int = 15) -> bool:
    """Poll until WinRM responds or timeout expires. Returns True if reachable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = _winrm_session(ip, username, password, op_timeout=10, read_timeout=12)
            r = s.run_ps("echo ok")
            if r.status_code == 0:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def rename_box(ip: str, username: str, password: str,
               new_name: str, dry_run: bool) -> tuple[bool, str]:
    """Rename Windows box to inventory hostname + reboot.

    Runs when sysprep set a machine SID but left the placeholder ComputerName
    (WIN-XXXX...). Install-ADDSForest and Add-Computer bake ComputerName, so
    the rename must land before either. Idempotent: skipped when current name
    already matches. Reboots the box on rename.
    """
    ps = (
        f"$new = '{new_name}'; "
        f"$cur = $env:COMPUTERNAME; "
        f"if ($cur -ieq $new) {{ Write-Output ('already ' + $cur); exit 0 }}; "
        f"Rename-Computer -NewName $new -Force -ErrorAction Stop; "
        f"Write-Output ('renamed ' + $cur + ' -> ' + $new); "
        f"Restart-Computer -Force"
    )
    if dry_run:
        return True, f"[DRY RUN] rename {ip} -> {new_name}"
    try:
        s = _winrm_session(ip, username, password)
        r = s.run_ps(ps)
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        return r.status_code == 0, out or "ok"
    except Exception as e:
        err = str(e)
        if _is_connection_drop(err):
            return True, f"connection dropped after rename (expected reboot): {err}"
        return False, err


def get_machine_sid(ip: str, username: str, password: str) -> str:
    s = _winrm_session(ip, username, password)
    r = s.run_ps("(Get-LocalUser Administrator).SID.AccountDomainSid.ToString()")
    out = (r.std_out + r.std_err).decode(errors="replace").strip()
    return out.splitlines()[0].strip() if out else ""


# Optional per-team domain-name/NetBIOS overrides read from inventory.yml's
# `domain:` / `netbios:` team keys (see load_teams). Keyed by team_num rather
# than threaded through every function that currently takes team_num (dozens
# of call sites across this file) — a lookup populated once is far less
# invasive than changing every signature to pass the whole team dict.
_TEAM_OVERRIDES: dict[int, dict[str, str]] = {}

# Optional inventory-wide domain/netbios *templates* (top-level `domain_pattern:`
# / `netbios_pattern:` keys, e.g. "DOMAIN{team_num}.COM") — one line covers
# every team instead of repeating a per-team `domain:` override N times. A
# per-team explicit override (above) still wins if both are set. See load_teams.
_DOMAIN_PATTERN: str | None = None
_NETBIOS_PATTERN: str | None = None


def _domain_for_team(team_num: int) -> str:
    override = _TEAM_OVERRIDES.get(team_num, {}).get("domain")
    if override:
        return override
    if _DOMAIN_PATTERN:
        return _DOMAIN_PATTERN.format(team_num=team_num)
    return f"TEAM{team_num}.C3"


def _netbios_for_team(team_num: int) -> str:
    override = _TEAM_OVERRIDES.get(team_num, {}).get("netbios")
    if override:
        return override
    if _NETBIOS_PATTERN:
        return _NETBIOS_PATTERN.format(team_num=team_num)
    return f"TEAM{team_num}"


# ---------------------------------------------------------------------------
# Sysprep
# ---------------------------------------------------------------------------

def sysprep_box(ip: str, username: str, password: str,
                hostname: str, dry_run: bool) -> tuple[bool, str]:
    unattend_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>{hostname}</ComputerName>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <NetworkLocation>Work</NetworkLocation>
        <ProtectYourPC>1</ProtectYourPC>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
      <UserAccounts>
        <AdministratorPassword>
          <Value>Password1!</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
      </UserAccounts>
    </component>
  </settings>
</unattend>"""

    if dry_run:
        return True, f"[DRY RUN] sysprep_box {ip} hostname={hostname} (WinRM only, no static IP)"

    try:
        s = _winrm_session(ip, username, password)

        # Step 1: write SetupComplete files (enable WinRM on first boot after sysprep)
        # Static IP is NOT set here — the router assigns the correct IP via DHCP reservation,
        # and Phase 3 (configure_dc_network / configure_win2_network) applies the permanent static config.
        ps1_content = (
            "Start-Transcript -Path 'C:\\Windows\\Temp\\c3r_setup.log' -Append -Force\n"
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "winrm quickconfig -q\n"
            "winrm set winrm/config/service '@{AllowUnencrypted=\"true\"}'\n"
            'netsh advfirewall firewall add rule name="WinRM-HTTP" dir=in action=allow protocol=TCP localport=5985\n'
            "Stop-Transcript\n"
        )
        ps1_b64 = __import__("base64").b64encode(ps1_content.encode("utf-16-le")).decode()
        r = s.run_ps(
            "New-Item -ItemType Directory -Force -Path 'C:\\Windows\\Setup\\Scripts' | Out-Null\n"
            f"[IO.File]::WriteAllBytes('C:\\Windows\\Setup\\Scripts\\c3r_setup.ps1', [Convert]::FromBase64String('{ps1_b64}'))\n"
            "Set-Content -Path 'C:\\Windows\\Setup\\Scripts\\SetupComplete.cmd' -Encoding ASCII "
            "-Value 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\Windows\\Setup\\Scripts\\c3r_setup.ps1'"
        )
        if r.status_code != 0:
            return False, (r.std_out + r.std_err).decode(errors="replace").strip()

        # Step 2: write unattend.xml
        xml_b64 = __import__("base64").b64encode(unattend_xml.encode("utf-8")).decode()
        r = s.run_ps(
            f"[IO.File]::WriteAllBytes('C:\\Windows\\System32\\Sysprep\\unattend.xml', [Convert]::FromBase64String('{xml_b64}'))"
        )
        if r.status_code != 0:
            return False, (r.std_out + r.std_err).decode(errors="replace").strip()

        # Step 3: remove WinGet source package (blocks sysprep /generalize) then run sysprep
        r = s.run_ps(
            "Get-AppxPackage -AllUsers *Winget.Source* | Remove-AppxPackage -AllUsers -ErrorAction SilentlyContinue\n"
            "Get-AppxProvisionedPackage -Online | Where-Object { $_.DisplayName -like '*Winget.Source*' } "
            "| Remove-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue\n"
            "Start-Process -FilePath 'C:\\Windows\\System32\\Sysprep\\sysprep.exe' "
            "-ArgumentList '/generalize /oobe /reboot /quiet /unattend:C:\\Windows\\System32\\Sysprep\\unattend.xml' "
            "-NoNewWindow -Wait"
        )
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        return r.status_code == 0, out or "ok"
    except Exception as e:
        err = str(e)
        if _is_connection_drop(err):
            return True, f"connection dropped after sysprep (expected): {err}"
        return False, err


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------

def install_ad_feature(ip: str, username: str, password: str,
                       dry_run: bool) -> tuple[bool, str]:
    script = "Install-WindowsFeature AD-Domain-Services -IncludeManagementTools"
    if dry_run:
        return True, f"[DRY RUN] winrm PS:\n{script}"
    try:
        # Feature install can take several minutes
        s = _winrm_session(ip, username, password, op_timeout=600, read_timeout=610)
        r = s.run_ps(script)
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        return r.status_code == 0, out or "ok"
    except Exception as e:
        return False, str(e)


def promote_to_dc(ip: str, username: str, password: str,
                  domain: str, netbios: str, dry_run: bool) -> tuple[bool, str]:
    # SafeMode/DSRM password reuses the inventory-provided local Administrator
    # password so we don't have a second secret to track post-promotion.
    esc_pw = password.replace("'", "''")
    script = f"""
Import-Module ADDSDeployment
Install-ADDSForest `
    -DomainName '{domain}' `
    -DomainNetbiosName '{netbios}' `
    -SafeModeAdministratorPassword (ConvertTo-SecureString '{esc_pw}' -AsPlainText -Force) `
    -InstallDns `
    -Force `
    -NoRebootOnCompletion:$false
"""
    if dry_run:
        return True, f"[DRY RUN] winrm PS:\n{script.strip()}"
    try:
        s = _winrm_session(ip, username, password, op_timeout=600, read_timeout=610)
        r = s.run_ps(script)
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        return r.status_code == 0, out or "ok"
    except Exception as e:
        err = str(e)
        if _is_connection_drop(err):
            return True, f"connection dropped after promotion (expected): {err}"
        return False, err


def promote_win1(ip: str, username: str, password: str,
                 team_num: int, dry_run: bool) -> tuple[bool, str]:
    domain  = _domain_for_team(team_num)
    netbios = _netbios_for_team(team_num)

    ok, out1 = install_ad_feature(ip, username, password, dry_run)
    if not ok:
        return False, f"AD-DS feature install failed: {out1}"

    ok, out2 = promote_to_dc(ip, username, password, domain, netbios, dry_run)
    if dry_run:
        return ok, f"{out1}\n\n{out2}"
    return ok, out2


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

def set_dc_forwarder(dc_ip: str, dom_user: str, dom_pw: str,
                     fw_ip: str, dry_run: bool) -> tuple[bool, str]:
    """Set the DC's DNS upstream forwarder to its team's pfSense LAN IP.

    Runs after Install-ADDSForest promoted the box + it rebooted. Uses the
    inventory-supplied domain admin credential because local SAM is gone
    once the box became a DC. Root hints stay off so unresolved names go
    only to the forwarder.
    """
    script = (
        "Start-Transcript -Path 'C:\\Windows\\Temp\\forwarder.log' -Append -Force\n"
        f"Set-DnsServerForwarder -IPAddress '{fw_ip}' -UseRootHint $false -PassThru | "
        "Select-Object -ExpandProperty IPAddress\n"
        "Stop-Transcript\n"
    )
    if dry_run:
        return True, f"[DRY RUN] set forwarder {dc_ip} -> {fw_ip}"
    try:
        s = _winrm_session(dc_ip, dom_user, dom_pw)
        r = s.run_ps(script)
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        return r.status_code == 0, out or "ok"
    except Exception as e:
        return False, str(e)


def configure_dc_network(ip: str, username: str, password: str,
                         fw_ip: str, dry_run: bool) -> tuple[bool, str]:
    """Point the DC's own DNS client at 127.0.0.1 (its local DNS Server role).

    IP configuration and DNS forwarder are intentionally NOT set here — DHCP
    is left in place and forwarder is out-of-scope for this range.
    """
    script = (
        "Start-Transcript -Path 'C:\\Windows\\Temp\\netsetup.log' -Append -Force\n"
        "$a = (Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and $_.HardwareInterface } | Select-Object -First 1).Name\n"
        'Write-Host "Adapter: $a"\n'
        "netsh interface ipv4 set dns name=$a source=static address=127.0.0.1\n"
        "Stop-Transcript\n"
    )
    if dry_run:
        return True, f"[DRY RUN] winrm PS:\n{script}"
    try:
        s = _winrm_session(ip, username, password)
        r = s.run_ps(script)
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        return r.status_code == 0, out or "ok"
    except Exception as e:
        if _is_connection_drop(str(e)):
            return True, f"connection dropped after IP change (expected): {e}"
        return False, str(e)


def configure_win2_network(ip: str, username: str, password: str,
                           win1_ip: str, fw_ip: str, dry_run: bool) -> tuple[bool, str]:
    """Point win2's DNS client at the DC so it can resolve the AD zone before
    the join. IP configuration is intentionally NOT changed here — DHCP-issued
    addressing is left in place. The `fw_ip` arg is kept in the signature so
    callers don't have to change, but only DNS is set."""
    script = (
        "Start-Transcript -Path 'C:\\Windows\\Temp\\netsetup.log' -Append -Force\n"
        "$a = (Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and $_.HardwareInterface } | Select-Object -First 1).Name\n"
        'Write-Host "Adapter: $a"\n'
        f"netsh interface ipv4 set dns name=$a source=static address={win1_ip}\n"
        "Stop-Transcript\n"
    )
    if dry_run:
        return True, f"[DRY RUN] winrm PS:\n{script}"
    try:
        s = _winrm_session(ip, username, password)
        r = s.run_ps(script)
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        return r.status_code == 0, out or "ok"
    except Exception as e:
        if _is_connection_drop(str(e)):
            return True, f"connection dropped after IP change (expected): {e}"
        return False, str(e)


# ---------------------------------------------------------------------------
# Domain join
# ---------------------------------------------------------------------------

def join_domain(ip: str, username: str, password: str,
                team_num: int, win1_name: str, dry_run: bool,
                domain_admin_user: str | None = None,
                domain_admin_password: str | None = None) -> tuple[bool, str]:
    netbios = _netbios_for_team(team_num)
    # Prefer inventory-supplied domain-admin credential so joins work whether
    # the DC's admin password matches inventory or a hard-coded default. The
    # local-admin `password` arg is the fallback (post-promotion local admin
    # = domain admin).
    dc_user = domain_admin_user or f"{netbios}\\Administrator"
    dc_pw   = domain_admin_password or password
    esc_pw   = dc_pw.replace("'", "''")
    esc_user = dc_user.replace("'", "''")
    script = f"""
$pw   = ConvertTo-SecureString '{esc_pw}' -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential('{esc_user}', $pw)
Add-Computer -DomainName '{netbios}' -Server '{win1_name}' -Credential $cred -Restart -Force
"""
    if dry_run:
        return True, f"[DRY RUN] winrm PS:\n{script.strip()}"
    try:
        s = _winrm_session(ip, username, password)
        r = s.run_ps(script)
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        return r.status_code == 0, out or "ok"
    except Exception as e:
        err = str(e)
        if _is_connection_drop(err):
            return True, f"connection dropped after domain join (expected): {err}"
        return False, err


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify_win1(ip: str, username: str, password: str,
                _team_num: int) -> tuple[bool, str]:
    try:
        s = _winrm_session(ip, username, password)
        r = s.run_ps("$ProgressPreference='SilentlyContinue'; Write-Host (Get-WmiObject Win32_ComputerSystem).DomainRole")
        lines = _clean_ps(r.std_out)
        role_str = lines[-1] if lines else ""
        try:
            role = int(role_str)
        except ValueError:
            return False, f"unexpected DomainRole output: {role_str!r}"
        ok = role in (4, 5)
        return ok, f"DomainRole={role} ({'DC' if ok else 'not a DC, want 4 or 5'})"
    except Exception as e:
        return False, f"connection failed: {e}"


def verify_win2(ip: str, username: str, password: str,
                team_num: int) -> tuple[bool, str]:
    expected = _domain_for_team(team_num)
    try:
        s = _winrm_session(ip, username, password)
        r = s.run_ps("$ProgressPreference='SilentlyContinue'; Write-Host (Get-WmiObject Win32_ComputerSystem).Domain")
        lines = _clean_ps(r.std_out)
        actual = lines[-1] if lines else ""
        ok = actual.upper() == expected.upper()
        return ok, f"domain={actual!r}  (want {expected!r})"
    except Exception as e:
        return False, f"connection failed: {e}"


def verify_dns(win1_ip: str, win1_user: str, win1_pass: str,
               fw_ip: str) -> tuple[bool, str]:
    try:
        s = _winrm_session(win1_ip, win1_user, win1_pass)
        r = s.run_ps("(Get-DnsServerForwarder).IPAddress.IPAddressToString")
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        found = fw_ip in out
        return found, f"forwarders: {out!r}  (want {fw_ip!r})"
    except Exception as e:
        return False, f"connection failed: {e}"


def verify_win1_resolution(ip: str, username: str, password: str) -> tuple[bool, str]:
    """Confirm win1 (DC) can resolve an external name, proving the forwarder chain works."""
    try:
        s = _winrm_session(ip, username, password)
        r = s.run_ps(
            "$ProgressPreference='SilentlyContinue'; "
            "Write-Host (Resolve-DnsName google.com -Type A -ErrorAction Stop | Select-Object -First 1 -ExpandProperty IPAddress)"
        )
        lines = _clean_ps(r.std_out)
        addr = lines[-1] if lines else ""
        resolved = bool(addr)
        return resolved, f"google.com → {addr!r}"
    except Exception as e:
        return False, f"connection failed: {e}"


def verify_win2_domain_auth(ip: str, team_num: int, password: str) -> tuple[bool, str]:
    """Confirm win2 accepts domain credentials — proves the domain join is fully functional."""
    dc_user = f"{_netbios_for_team(team_num)}\\Administrator"
    try:
        s = _winrm_session(ip, dc_user, password)
        r = s.run_ps("(Get-WmiObject Win32_ComputerSystem).UserName")
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        ok = r.status_code == 0
        return ok, f"domain auth as {dc_user!r}: {'ok' if ok else out}"
    except Exception as e:
        return False, f"domain auth failed: {e}"


def verify_ip(ip: str, username: str, password: str,
              expected_ip: str) -> tuple[bool, str]:
    """Confirm the box has the expected static IP configured."""
    try:
        s = _winrm_session(ip, username, password)
        r = s.run_ps(
            "$ProgressPreference='SilentlyContinue'; "
            "Write-Host (Get-NetIPAddress -AddressFamily IPv4 "
            "| Where-Object { $_.PrefixOrigin -eq 'Manual' } "
            "| Select-Object -First 1 -ExpandProperty IPAddress)"
        )
        lines = _clean_ps(r.std_out)
        actual = lines[-1] if lines else ""
        ok = actual.strip() == expected_ip
        return ok, f"static IP={actual!r}  (want {expected_ip!r})"
    except Exception as e:
        return False, f"connection failed: {e}"


def verify_win2_dns(ip: str, username: str, password: str,
                    win1_ip: str, domain: str) -> tuple[bool, str]:
    """Confirm win2 can resolve both the domain name and external names via the DC."""
    script = f"""
$ext = Resolve-DnsName google.com -Type A -Server {win1_ip} -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty IPAddress
$dom = Resolve-DnsName {domain} -Type A -Server {win1_ip} -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty IPAddress
Write-Host "ext=$ext dom=$dom"
"""
    try:
        s = _winrm_session(ip, username, password)
        r = s.run_ps(script)
        out = (r.std_out + r.std_err).decode(errors="replace").strip()
        last = out.splitlines()[-1].strip() if out else ""
        ext_ok = "ext=" in last and last.split("ext=")[1].split()[0] not in ("", "None")
        dom_ok = "dom=" in last and last.split("dom=")[1] not in ("", "None")
        return ext_ok and dom_ok, f"dns check: {last!r}  (want ext+dom resolved)"
    except Exception as e:
        return False, f"connection failed: {e}"


def verify_dc_has_member(win1_ip: str, username: str, password: str,
                         win2_name: str) -> tuple[bool, str]:
    """Query the DC to confirm win2's computer object exists in AD."""
    try:
        s = _winrm_session(win1_ip, username, password)
        r = s.run_ps(
            f"$ProgressPreference='SilentlyContinue'; "
            f"$c = Get-ADComputer -Filter {{Name -eq '{win2_name}'}} -ErrorAction SilentlyContinue; "
            f"Write-Host ($c.Name)"
        )
        lines = _clean_ps(r.std_out)
        found = lines[-1].strip() if lines else ""
        ok = found.upper() == win2_name.upper()
        return ok, f"AD computer object: {found!r}  (want {win2_name!r})"
    except Exception as e:
        return False, f"connection failed: {e}"


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------

def _validate_pattern(pattern: str, key: str) -> None:
    # Fail fast on a malformed pattern (e.g. `{teamnum}` typo'd instead of
    # `{team_num}`) at load time, not on the first team_num that hits it deep
    # inside a phase runner.
    try:
        pattern.format(team_num=0)
    except (KeyError, IndexError, ValueError) as e:
        print(f"{RED}Invalid {key!r} in inventory.yml: {pattern!r} ({e}). "
              f"Use a plain Python str.format() placeholder, e.g. 'TEAM{{team_num}}.C3'.{RESET}")
        sys.exit(1)


def load_teams(team_filter: list[int] | None) -> list[dict]:
    global _DOMAIN_PATTERN, _NETBIOS_PATTERN
    inventory = yaml.safe_load(INVENTORY_PATH.read_text())
    teams = inventory["teams"]
    if inventory.get("domain_pattern"):
        _validate_pattern(inventory["domain_pattern"], "domain_pattern")
        _DOMAIN_PATTERN = inventory["domain_pattern"]
    if inventory.get("netbios_pattern"):
        _validate_pattern(inventory["netbios_pattern"], "netbios_pattern")
        _NETBIOS_PATTERN = inventory["netbios_pattern"]
    for t in teams:
        override = {k: t[k] for k in ("domain", "netbios") if t.get(k)}
        if override:
            _TEAM_OVERRIDES[t["team_num"]] = override
    if team_filter:
        teams = [t for t in teams if t.get("team_num") in team_filter]
        if not teams:
            print(f"{RED}No teams found for team_num(s): {team_filter}{RESET}")
            sys.exit(1)
    return teams


def fw_for_team(team: dict) -> dict | None:
    for box in team["boxes"]:
        if box["os"].lower() == "firewall":
            return box
    return None


def win1_for_team(team: dict) -> dict | None:
    """The team's domain controller. Priority: explicit `is_domain_controller:
    true` on a box (works regardless of naming/order) > name ends in '-1'
    (this lab's convention) > first Windows box in the list (order-dependent
    last resort — this used to be the ONLY rule, which silently broke for any
    inventory not listing the DC first)."""
    windows = [b for b in team["boxes"] if b["os"].lower() == "windows"]
    flagged = [b for b in windows if b.get("is_domain_controller")]
    if flagged:
        return flagged[0]
    suffixed = [b for b in windows if b["name"].endswith("-1")]
    if suffixed:
        return suffixed[0]
    return windows[0] if windows else None


def win2_for_team(team: dict) -> dict | None:
    """The Windows box to be joined to the domain — the first Windows box
    that isn't the DC (not simply "index 1"; that broke once win1 could be
    picked by flag/suffix rather than list order)."""
    windows = [b for b in team["boxes"] if b["os"].lower() == "windows"]
    dc = win1_for_team(team)
    others = [b for b in windows if b is not dc]
    return others[0] if others else None


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------

def _print_result(label: str, ok: bool, detail: str, mode: str,
                  dry_run: bool = False) -> None:
    color  = GREEN if ok else RED
    status = ("MATCH" if ok else "MISMATCH") if mode == "verify" else ("OK" if ok else "FAIL")
    print(f"  {color}[{status}]{RESET} {label}")
    if mode == "verify" or not ok or dry_run:
        for line in detail.strip().splitlines():
            print(f"        {line}")


def run_sysprep(teams: list[dict], dry_run: bool, workers: int) -> tuple[bool, dict[str, str]]:
    print(f"[Phase 1] Sysprepping {len(teams)} team(s) (win1 + win2){'  [DRY RUN]' if dry_run else ''}...\n")
    items = []
    for team in teams:
        fw = fw_for_team(team)
        if not fw:
            print(f"{YELLOW}[WARN] Team {team['name']} has no firewall — skipping sysprep.{RESET}")
            continue
        items.extend(
            (box, fw["ip"])
            for box in [win1_for_team(team), win2_for_team(team)]
            if box
        )

    old_sids: dict[str, str] = {}
    if not dry_run:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            sid_futures = {ex.submit(get_machine_sid, b["ip"], b["username"], b["password"]): b["ip"]
                           for b, _ in items}
            for f in as_completed(sid_futures):
                ip = sid_futures[f]
                try:
                    old_sids[ip] = f.result()
                except Exception:
                    old_sids[ip] = ""

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(sysprep_box, b["ip"], b["username"], b["password"],
                      b["name"], dry_run): b
            for b, fw_ip in items
        }
        for f in as_completed(futures):
            b = futures[f]
            ok, out = f.result()
            label = f"{b['name']} ({b['ip']})"
            results.append(ok)
            _print_result(label, ok, out, "configure", dry_run)

    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{GREEN}{passed} succeeded{RESET}, {RED}{failed} failed{RESET}")
    return failed == 0, old_sids


def run_sid_check(teams: list[dict], old_sids: dict[str, str], workers: int) -> bool:
    print("\n[SID Check] Verifying sysprep changed machine SIDs...\n")
    items = []
    for t in teams:
        items.extend(
            (box, old_sids[box["ip"]])
            for box in [win1_for_team(t), win2_for_team(t)]
            if box and box["ip"] in old_sids
        )

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(get_machine_sid, b["ip"], b["username"], b["password"]): (b, old_sid)
                   for b, old_sid in items}
        for f in as_completed(futures):
            b, old_sid = futures[f]
            try:
                new_sid = f.result()
            except Exception:
                new_sid = ""
            ok = bool(new_sid) and new_sid != old_sid
            label = f"{b['name']} ({b['ip']})"
            detail = f"old={old_sid}  new={new_sid}"
            results.append(ok)
            _print_result(label, ok, detail, "verify")

    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{GREEN}{passed} re-SID'd{RESET}, {RED}{failed} unchanged{RESET}")
    return failed == 0


def run_rename(teams: list[dict], dry_run: bool, workers: int) -> bool:
    """Rename every windows box in `teams` to its inventory hostname.

    Both HQ (DC-to-be) and SKELD (member) get renamed since sysprep placeholder
    names would break DC promotion (Install-ADDSForest bakes ComputerName) and
    the domain-join `-Server '{win1_name}'` lookup that follows.
    """
    print(f"\n[Phase 1.5] Renaming {len(teams) * 2} windows box(es) to inventory names"
          f"{'  [DRY RUN]' if dry_run else ''}...\n")
    items = []
    for team in teams:
        for box in (win1_for_team(team), win2_for_team(team)):
            if box:
                items.append(box)

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(rename_box, b["ip"], b["username"], b["password"],
                      b["name"], dry_run): b
            for b in items
        }
        for f in as_completed(futures):
            b = futures[f]
            ok, out = f.result()
            label = f"{b['name']} ({b['ip']})"
            results.append(ok)
            _print_result(label, ok, out, "configure", dry_run)

    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{GREEN}{passed} succeeded{RESET}, {RED}{failed} failed{RESET}")
    return failed == 0


def run_promote(teams: list[dict], dry_run: bool, workers: int) -> bool:
    print(f"\n[Phase 2] Promoting {len(teams)} win1 box(es) to DC{'  [DRY RUN]' if dry_run else ''}...\n")
    items = []
    for team in teams:
        box = win1_for_team(team)
        if not box:
            continue
        items.append((box, team["team_num"]))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(promote_win1, b["ip"], b["username"], b["password"],
                      team_num, dry_run): b
            for b, team_num in items
        }
        for f in as_completed(futures):
            b = futures[f]
            ok, out = f.result()
            label = f"{b['name']} ({b['ip']})"
            results.append(ok)
            _print_result(label, ok, out, "configure", dry_run)

    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{GREEN}{passed} succeeded{RESET}, {RED}{failed} failed{RESET}")
    return failed == 0


def run_forwarder(teams: list[dict], dry_run: bool, workers: int) -> bool:
    """Point every promoted DC's DNS forwarder at its team's pfSense LAN IP.

    Requires each team's DC to be up + reachable via WinRM as its
    domain-admin (inventory `domain_username`/`domain_password`). Run after
    `--step promote` completes and the DC has rebooted.
    """
    print(f"\n[Phase 2.5] Setting DC DNS forwarder to per-team pfSense{'  [DRY RUN]' if dry_run else ''}...\n")
    items = []
    for team in teams:
        w1 = win1_for_team(team)
        fw = fw_for_team(team)
        if not w1 or not fw:
            print(f"{YELLOW}[WARN] Team {team['name']} missing win1 or firewall — skipping.{RESET}")
            continue
        dom_user = w1.get("domain_username") or w1["username"]
        dom_pw   = w1.get("domain_password") or w1["password"]
        items.append((w1, dom_user, dom_pw, fw["ip"]))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(set_dc_forwarder, w1["ip"], du, dp, fw_ip, dry_run): w1
            for w1, du, dp, fw_ip in items
        }
        for f in as_completed(futures):
            w1 = futures[f]
            ok, out = f.result()
            label = f"{w1['name']} ({w1['ip']}) forwarder"
            results.append(ok)
            _print_result(label, ok, out, "configure", dry_run)

    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{GREEN}{passed} succeeded{RESET}, {RED}{failed} failed{RESET}")
    return failed == 0


def run_dns(teams: list[dict], dry_run: bool, workers: int) -> bool:
    print(f"\n[Phase 3] Configuring network/DNS for {len(teams)} team(s){'  [DRY RUN]' if dry_run else ''}...\n")
    items = []
    for team in teams:
        fw = fw_for_team(team)
        w1 = win1_for_team(team)
        w2 = win2_for_team(team)
        if not fw or not w1:
            print(f"{YELLOW}[WARN] Team {team['name']} missing fw or win1 — skipping.{RESET}")
            continue
        items.append((w1, w2, fw["ip"]))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures: dict = {}
        for w1, w2, fw_ip in items:
            next(t["team_num"] for t in teams if win1_for_team(t) and win1_for_team(t)["ip"] == w1["ip"])
            futures[ex.submit(configure_dc_network, w1["ip"], w1["domain_username"],
                              w1["domain_password"], fw_ip, dry_run)] = f"{w1['name']} ({w1['ip']}) DNS→127.0.0.1"
            if w2:
                futures[ex.submit(configure_win2_network, w2["ip"], w2["username"],
                                  w2["password"], w1["ip"], fw_ip, dry_run)] = f"{w2['name']} ({w2['ip']}) DNS→DC ({w1['ip']})"
        for f in as_completed(futures):
            label = futures[f]
            ok, out = f.result()
            results.append(ok)
            _print_result(label, ok, out, "configure", dry_run)

    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{GREEN}{passed} succeeded{RESET}, {RED}{failed} failed{RESET}")
    return failed == 0


def run_join(teams: list[dict], dry_run: bool, workers: int) -> bool:
    print(f"\n[Phase 4] Joining {len(teams)} win2 box(es) to domain{'  [DRY RUN]' if dry_run else ''}...\n")
    items = []
    for team in teams:
        box = win2_for_team(team)
        w1  = win1_for_team(team)
        if not box:
            print(f"{YELLOW}[WARN] Team {team['name']} has no win2 box — skipping.{RESET}")
            continue
        if not w1:
            print(f"{YELLOW}[WARN] Team {team['name']} has no win1/DC box — skipping join.{RESET}")
            continue
        # Domain-admin credential for Add-Computer: prefer the DC box's
        # inventory-provided domain_username / domain_password (which after
        # promotion is the local-admin-turned-domain-admin), fall back to
        # the DC's local admin creds when domain_* isn't declared.
        dom_user = w1.get("domain_username") or w1["username"]
        dom_pw   = w1.get("domain_password") or w1["password"]
        items.append((box, team["team_num"], w1["name"], dom_user, dom_pw))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(join_domain, b["ip"], b["username"], b["password"],
                      team_num, win1_name, dry_run, dom_user, dom_pw): b
            for b, team_num, win1_name, dom_user, dom_pw in items
        }
        for f in as_completed(futures):
            b = futures[f]
            ok, out = f.result()
            label = f"{b['name']} ({b['ip']})"
            results.append(ok)
            _print_result(label, ok, out, "configure", dry_run)

    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{GREEN}{passed} succeeded{RESET}, {RED}{failed} failed{RESET}")
    return failed == 0


def run_verify(teams: list[dict], workers: int) -> bool:
    print(f"\n[Verify] Checking {len(teams)} team(s)...\n")
    items = []
    for team in teams:
        fw = fw_for_team(team)
        w1 = win1_for_team(team)
        w2 = win2_for_team(team)
        items.append((team["team_num"], fw, w1, w2))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {}
        for team_num, fw, w1, w2 in items:
            if w1:
                futures[ex.submit(verify_win1, w1["ip"], w1["domain_username"], w1["domain_password"], team_num)] = \
                    (f"{w1['name']} ({w1['ip']}) DC role", "win1")
                futures[ex.submit(verify_win1_resolution, w1["ip"], w1["domain_username"], w1["domain_password"])] = \
                    (f"{w1['name']} ({w1['ip']}) external DNS", "win1-dns")
                futures[ex.submit(verify_ip, w1["ip"], w1["domain_username"], w1["domain_password"], w1["ip"])] = \
                    (f"{w1['name']} ({w1['ip']}) static IP", "win1-ip")
            if w2:
                futures[ex.submit(verify_win2, w2["ip"], w2["domain_username"], w2["domain_password"], team_num)] = \
                    (f"{w2['name']} ({w2['ip']}) domain member", "win2")
                futures[ex.submit(verify_win2_domain_auth, w2["ip"], team_num, w2["domain_password"])] = \
                    (f"{w2['name']} ({w2['ip']}) domain auth", "win2-auth")
                futures[ex.submit(verify_ip, w2["ip"], w2["domain_username"], w2["domain_password"], w2["ip"])] = \
                    (f"{w2['name']} ({w2['ip']}) static IP", "win2-ip")
            if w1 and w2:
                domain = _domain_for_team(team_num)
                futures[ex.submit(verify_win2_dns, w2["ip"], w2["domain_username"], w2["domain_password"],
                                  w1["ip"], domain)] = \
                    (f"{w2['name']} ({w2['ip']}) DNS resolution", "win2-dns")
            if w1 and fw:
                futures[ex.submit(verify_dns, w1["ip"], w1["domain_username"], w1["domain_password"], fw["ip"])] = \
                    (f"{w1['name']} ({w1['ip']}) DNS forwarder", "dns")

        failed_labels: list[str] = []
        for f in as_completed(futures):
            label, _kind = futures[f]
            ok, detail = f.result()
            results.append(ok)
            _print_result(label, ok, detail, "verify")
            if not ok:
                failed_labels.append(label)

    return _verify_summary(results, failed_labels)


def run_verify_promote(teams: list[dict], workers: int) -> bool:
    """Verify win1 boxes were successfully promoted to Domain Controllers."""
    print(f"\n[Verify Promote] Checking DC role for {len(teams)} team(s)...\n")
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {}
        for team in teams:
            w1 = win1_for_team(team)
            if not w1:
                continue
            team_num = team["team_num"]
            futures[ex.submit(verify_win1, w1["ip"], w1["domain_username"], w1["domain_password"], team_num)] = \
                f"{w1['name']} ({w1['ip']}) DC role"
        failed_labels: list[str] = []
        for f in as_completed(futures):
            label = futures[f]
            ok, detail = f.result()
            results.append(ok)
            _print_result(label, ok, detail, "verify")
            if not ok:
                failed_labels.append(label)
    return _verify_summary(results, failed_labels)


def run_verify_join(teams: list[dict], workers: int) -> bool:
    """Verify win2 boxes joined the domain and accept domain credentials."""
    print(f"\n[Verify Join] Checking domain membership for {len(teams)} team(s)...\n")
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {}
        for team in teams:
            w1 = win1_for_team(team)
            w2 = win2_for_team(team)
            if not w2:
                continue
            team_num = team["team_num"]
            futures[ex.submit(verify_win2, w2["ip"], w2["username"], w2["password"], team_num)] = \
                f"{w2['name']} ({w2['ip']}) domain member"
            futures[ex.submit(verify_win2_domain_auth, w2["ip"], team_num, w2["password"])] = \
                f"{w2['name']} ({w2['ip']}) domain auth"
            if w1:
                futures[ex.submit(verify_dc_has_member, w1["ip"], w1["domain_username"], w1["domain_password"], w2["name"])] = \
                    f"{w1['name']} ({w1['ip']}) AD has {w2['name']}"
        failed_labels: list[str] = []
        for f in as_completed(futures):
            label = futures[f]
            ok, detail = f.result()
            results.append(ok)
            _print_result(label, ok, detail, "verify")
            if not ok:
                failed_labels.append(label)
    return _verify_summary(results, failed_labels)


def run_verify_dns(teams: list[dict], workers: int) -> bool:
    """Verify static IPs and DNS forwarder were configured correctly in Phase 3."""
    print(f"\n[Verify DNS] Checking network config for {len(teams)} team(s)...\n")
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {}
        for team in teams:
            fw = fw_for_team(team)
            w1 = win1_for_team(team)
            w2 = win2_for_team(team)
            team_num = team["team_num"]
            if w1:
                futures[ex.submit(verify_ip, w1["ip"], w1["domain_username"], w1["domain_password"], w1["ip"])] = \
                    f"{w1['name']} ({w1['ip']}) static IP"
                if fw:
                    futures[ex.submit(verify_dns, w1["ip"], w1["domain_username"], w1["domain_password"], fw["ip"])] = \
                        f"{w1['name']} ({w1['ip']}) DNS forwarder"
                    futures[ex.submit(verify_win1_resolution, w1["ip"], w1["domain_username"], w1["domain_password"])] = \
                        f"{w1['name']} ({w1['ip']}) external DNS"
            if w2:
                futures[ex.submit(verify_ip, w2["ip"], w2["username"], w2["password"], w2["ip"])] = \
                    f"{w2['name']} ({w2['ip']}) static IP"
                if w1:
                    domain = _domain_for_team(team_num)
                    futures[ex.submit(verify_win2_dns, w2["ip"], w2["username"], w2["password"],
                                      w1["ip"], domain)] = \
                        f"{w2['name']} ({w2['ip']}) resolves {domain}"
        failed_labels: list[str] = []
        for f in as_completed(futures):
            label = futures[f]
            ok, detail = f.result()
            results.append(ok)
            _print_result(label, ok, detail, "verify")
            if not ok:
                failed_labels.append(label)
    return _verify_summary(results, failed_labels)


def _verify_summary(results: list[bool], failed_labels: list[str]) -> bool:
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{GREEN}{passed} matched{RESET}, {RED}{failed} mismatched{RESET}")
    for lbl in failed_labels:
        print(f"  {RED}✗ {lbl}{RESET}")
    return failed == 0


def _wait(seconds: int, label: str) -> None:
    print(f"\nWaiting {seconds}s {label}", end="", flush=True)
    for _ in range(seconds):
        time.sleep(1)
        print(".", end="", flush=True)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global INVENTORY_PATH
    parser = argparse.ArgumentParser(
        description=(
            "Promote win1 boxes to Domain Controllers and join win2 boxes to the domain. "
            "Default (no flags): sysprep → promote+dns → join → verify."
        )
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without executing")
    parser.add_argument("--verify", action="store_true",
                        help="Only verify current state without making changes")
    parser.add_argument("--team",    type=int, nargs="+", metavar="TEAM_NUM",
                        help="Only process these team number(s)")
    parser.add_argument("--exclude", type=int, nargs="+", metavar="TEAM_NUM",
                        help="Exclude one or more team numbers")
    parser.add_argument("--workers", type=int, default=10,
                        help="Max parallel workers (default 10)")
    parser.add_argument("--step",
                        choices=["sysprep", "rename", "promote", "forwarder", "dns", "join", "all"],
                        default="all",
                        help="Run only a specific phase (default: all)")
    parser.add_argument("--inventory", type=Path, default=None,
                        help=f"Path to inventory YAML (default: {INVENTORY_PATH})")
    args = parser.parse_args()

    if args.inventory is not None:
        INVENTORY_PATH = args.inventory

    teams = load_teams(args.team)
    if args.exclude:
        teams = [t for t in teams if t.get("team_num") not in args.exclude]
        if not teams:
            print(f"{RED}No teams remaining after exclusions.{RESET}")
            sys.exit(1)

    if args.verify:
        ok = run_verify(teams, args.workers)
        sys.exit(0 if ok else 1)

    if args.step in ("sysprep", "all"):
        ok, old_sids = run_sysprep(teams, args.dry_run, args.workers)
        if not ok and not args.dry_run:
            print(f"\n{RED}Sysprep phase had failures — aborting.{RESET}")
            sys.exit(1)
        if not args.dry_run and args.step == "all":
            all_boxes = [b for t in teams for b in [win1_for_team(t), win2_for_team(t)] if b]
            print(f"\nWaiting for box(es) to come back up after sysprep (up to {SYSPREP_WAIT}s)...")
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = {ex.submit(_wait_for_winrm, b["ip"], b["username"], b["password"],
                                     SYSPREP_WAIT): b for b in all_boxes}
                for f in as_completed(futures):
                    b = futures[f]
                    if not f.result():
                        print(f"{RED}  [TIMEOUT] {b['name']} ({b['ip']}) did not come back — aborting.{RESET}")
                        sys.exit(1)
                    print(f"{GREEN}  [UP] {b['name']} ({b['ip']}){RESET}")
            ok = run_sid_check(teams, old_sids, args.workers)
            if not ok:
                print(f"\n{RED}SID check failed — sysprep did not re-SID the machine. Aborting.{RESET}")
                sys.exit(1)

    if args.step in ("rename", "all"):
        ok = run_rename(teams, args.dry_run, args.workers)
        if not ok and not args.dry_run:
            print(f"\n{RED}Rename phase had failures — aborting.{RESET}")
            sys.exit(1)
        if not args.dry_run and args.step == "all":
            all_boxes = [b for t in teams for b in [win1_for_team(t), win2_for_team(t)] if b]
            print(f"\nWaiting for box(es) to come back up after rename (up to {RENAME_WAIT}s)...")
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = {ex.submit(_wait_for_winrm, b["ip"], b["username"], b["password"],
                                     RENAME_WAIT): b for b in all_boxes}
                for f in as_completed(futures):
                    b = futures[f]
                    if not f.result():
                        print(f"{RED}  [TIMEOUT] {b['name']} ({b['ip']}) did not come back — aborting.{RESET}")
                        sys.exit(1)
                    print(f"{GREEN}  [UP] {b['name']} ({b['ip']}){RESET}")

    if args.step in ("promote", "all"):
        ok = run_promote(teams, args.dry_run, args.workers)
        if not ok and not args.dry_run:
            print(f"\n{RED}Promote phase had failures — aborting.{RESET}")
            sys.exit(1)
        if not args.dry_run and args.step == "all":
            print(f"\nWaiting for DC(s) to come back up (up to {PROMOTE_WAIT}s)...")
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = {
                    ex.submit(_wait_for_winrm, win1_for_team(t)["ip"],
                              win1_for_team(t)["username"], win1_for_team(t)["password"],
                              PROMOTE_WAIT): win1_for_team(t)
                    for t in teams if win1_for_team(t)
                }
                for f in as_completed(futures):
                    b = futures[f]
                    if not f.result():
                        print(f"{RED}  [TIMEOUT] {b['name']} ({b['ip']}) did not come back — aborting.{RESET}")
                        sys.exit(1)
                    print(f"{GREEN}  [UP] {b['name']} ({b['ip']}){RESET}")
            if not args.dry_run:
                ok = run_verify_promote(teams, args.workers)
                if not ok:
                    print(f"\n{RED}Promote verification failed — aborting.{RESET}")
                    sys.exit(1)

    if args.step in ("forwarder", "all"):
        ok = run_forwarder(teams, args.dry_run, args.workers)
        if not ok and not args.dry_run:
            print(f"\n{RED}Forwarder phase had failures — aborting.{RESET}")
            sys.exit(1)

    if args.step in ("dns", "all"):
        ok = run_dns(teams, args.dry_run, args.workers)
        if not ok and not args.dry_run:
            print(f"\n{RED}DNS/network phase had failures — aborting.{RESET}")
            sys.exit(1)
        if not args.dry_run:
            ok = run_verify_dns(teams, args.workers)
            if not ok:
                print(f"\n{RED}DNS/network verification failed — aborting.{RESET}")
                sys.exit(1)

    if args.step in ("join", "all"):
        if not args.dry_run:
            _wait(300, "for DCs to fully initialize before domain join")
        ok = run_join(teams, args.dry_run, args.workers)
        if not ok and not args.dry_run:
            print(f"\n{RED}Join phase had failures — aborting.{RESET}")
            sys.exit(1)
        if not args.dry_run:
            win2_boxes = [win2_for_team(t) for t in teams if win2_for_team(t)]
            print(f"\nWaiting for win2 box(es) to come back up after domain join (up to {JOIN_WAIT}s)...")
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = {ex.submit(_wait_for_winrm, b["ip"], b["username"], b["password"],
                                     JOIN_WAIT): b for b in win2_boxes}
                for f in as_completed(futures):
                    b = futures[f]
                    if not f.result():
                        print(f"{RED}  [TIMEOUT] {b['name']} ({b['ip']}) did not come back — aborting.{RESET}")
                        sys.exit(1)
                    print(f"{GREEN}  [UP] {b['name']} ({b['ip']}){RESET}")
            ok = run_verify_join(teams, args.workers)
            if not ok:
                print(f"\n{RED}Join verification failed.{RESET}")
                sys.exit(1)

    if args.dry_run:
        return

    if args.step == "all":
        ok = run_verify(teams, args.workers)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
