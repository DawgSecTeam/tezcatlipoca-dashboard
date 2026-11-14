"""
Seed dummy data for frontend development and testing.
Reads teams and boxes from inventory.yml and populates every UI surface.

Run from the project root with the venv active:
    python scripts/testing/seed_dummy_data.py --clear

Options:
    --clear          Wipe all seeded data before seeding (recommended)
    --checks N       Number of IOC check rounds per team (default: 10)
    --inventory PATH Path to inventory YAML (default: config/inventory.yml)
"""

import argparse
import datetime
import json
import random
import sys
import uuid
from pathlib import Path

import yaml
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session


def _make_engine(db_url: str):
    """Create a SQLite engine with WAL + busy_timeout so seed scripts coexist
    with a backend subprocess writing the same DB file (pytest-xdist scenario)."""
    eng = create_engine(db_url, connect_args={"timeout": 30, "check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_conn, _conn_record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return eng

_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi_backend.database.models import (  # noqa: E402  # path append above
    BlueTeams,
    Boxes,
    CheckInstance,
    Credentials,
    DevBeacon,
    DevSession,
    ImplantBuilds,
    IOCCheckResult,
    PivotSessions,
    PortfwdMappings,
    SAResult,
)

DB_PATH = _PROJECT_ROOT / "db" / "database.db"
DEFINITIONS_DIR = _PROJECT_ROOT / "iocs" / "definitions"
ENGINE = _make_engine(f"sqlite:///{DB_PATH}")

# Teams that get active pivot sessions seeded (indexes into team list)
PIVOT_TEAM_INDICES = {1, 2, 3}  # teams 2, 3, and 4 (0-indexed); team 1 reserved for e2e pivot test

# SA placeholder data per OS
_WIN_PROCESSES = [
    {"name": "lsass.exe",    "pid": 680,  "cmdline": "",                  "mem": "8192 K"},
    {"name": "svchost.exe",  "pid": 848,  "cmdline": "-k netsvcs",        "mem": "4096 K"},
    {"name": "svchost.exe",  "pid": 1024, "cmdline": "-k localservice",   "mem": "2048 K"},
    {"name": "explorer.exe", "pid": 3520, "cmdline": "",                  "mem": "65536 K"},
    {"name": "powershell.exe","pid": 4488,"cmdline": "-ExecutionPolicy Bypass", "mem": "32768 K"},
]
_LIN_PROCESSES = [
    {"user": "root",   "pid": 1,    "cpu": "0.0", "mem": "0.1", "name": "systemd",     "cmdline": "/sbin/init"},
    {"user": "root",   "pid": 512,  "cpu": "0.0", "mem": "0.2", "name": "sshd",        "cmdline": "/usr/sbin/sshd -D"},
    {"user": "root",   "pid": 1024, "cpu": "0.1", "mem": "1.2", "name": "python3",     "cmdline": "python3 /opt/agent/agent.py"},
    {"user": "www-data","pid": 2048,"cpu": "0.3", "mem": "2.4", "name": "nginx",       "cmdline": "nginx: worker process"},
    {"user": "root",   "pid": 4096, "cpu": "0.0", "mem": "0.1", "name": "cron",        "cmdline": "/usr/sbin/cron -f"},
]

def _win_sa(_box_name: str) -> dict[str, dict]:
    now = datetime.datetime.now(datetime.UTC).strftime("%a %b %d %H:%M:%S %Y")
    return {
        "users":             {"users": [{"username": "Administrator", "session": "console", "state": "Active", "logon_time": now}]},
        "processes":         {"processes": _WIN_PROCESSES},
        "ports":             {"ports": [{"local_addr": "0.0.0.0:445", "pid": "4"}, {"local_addr": "0.0.0.0:3389", "pid": "1532"}, {"local_addr": "0.0.0.0:5985", "pid": "4"}]},
        "history":           {"history": ["Get-Process", "ipconfig /all", "whoami /priv", "net localgroup administrators"]},
        "services":          {"services": [{"name": "W32Time", "display_name": "Windows Time", "status": "Running"}, {"name": "Spooler", "display_name": "Print Spooler", "status": "Running"}, {"name": "WinRM", "display_name": "Windows Remote Management", "status": "Running"}]},
        "active_connections":{"connections": [{"local_addr": "192.168.1.x:5985", "remote_addr": "192.168.1.100:54321", "pid": 4, "process": "System"}]},
        "local_users_groups":{"users": [{"username": "Administrator", "enabled": "True", "last_logon": now}], "admins": ["Administrator"], "groups": []},
        "firewall_rules":    {"rules": ["Inbound Allow: Windows Remote Management (HTTP-In)", "Inbound Allow: File and Printer Sharing (SMB-In)"]},
        "scheduled_tasks":   {"tasks": [{"name": "\\Microsoft\\Windows\\WindowsUpdate\\Scheduled Start", "schedule": "Daily", "command": "%systemroot%\\system32\\sc.exe start wuauserv", "state": "Ready"}]},
        "auth_log":          {"entries": [f"Security Event 4624 - Logon: Administrator from 192.168.1.100 at {now}"]},
    }

def _lin_sa(box_name: str) -> dict[str, dict]:
    now = datetime.datetime.now(datetime.UTC).strftime("%a %b %d %H:%M:%S %Y")
    return {
        "users":             {"users": [{"username": "root", "session": "pts/0", "state": "", "logon_time": now}]},
        "processes":         {"processes": _LIN_PROCESSES},
        "ports":             {"ports": [{"local_addr": "0.0.0.0:22", "process": "sshd"}, {"local_addr": "0.0.0.0:80", "process": "nginx"}, {"local_addr": "127.0.0.1:5432", "process": "postgres"}]},
        "history":           {"history": ["ls -la /root", "cat /etc/passwd", "id", "uname -a", "ps aux | grep python"]},
        "services":          {"services": [{"name": "sshd.service", "display_name": "OpenBSD Secure Shell server", "status": "running"}, {"name": "nginx.service", "display_name": "A high performance web server", "status": "running"}]},
        "active_connections":{"connections": [{"local_addr": "0.0.0.0:22", "remote_addr": "192.168.1.100:54322", "pid": 512, "process": "sshd"}]},
        "local_users_groups":{"users": [{"username": "root", "uid": "0", "home": "/root", "shell": "/bin/bash"}], "groups": [{"name": "sudo", "gid": "27", "members": ["root"]}]},
        "firewall_rules":    {"rules": ["-A INPUT -p tcp --dport 22 -j ACCEPT", "-A INPUT -p tcp --dport 80 -j ACCEPT", "-A INPUT -j DROP"]},
        "scheduled_tasks":   {"tasks": [{"name": "root cron", "schedule": "0 * * * *", "command": "/usr/bin/apt-get update -q", "state": "active"}]},
        "auth_log":          {"entries": [f"Apr 21 08:00:01 {box_name} sshd[512]: Accepted publickey for root from 192.168.1.100 port 54322 ssh2"]},
        "auth_keys":         {"content": "", "hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
        "config_changes":    {"files": ["/etc/ssh/sshd_config", "/etc/hosts"]},
        "sudo_rules":        {"content": "root ALL=(ALL:ALL) ALL\n%sudo ALL=(ALL:ALL) ALL\n"},
    }

def _fw_sa(_box_name: str) -> dict[str, dict]:
    now = datetime.datetime.now(datetime.UTC).strftime("%a %b %d %H:%M:%S %Y")
    return {
        "users":             {"users": [{"username": "admin", "session": "pts/0", "state": "", "logon_time": now}]},
        "processes":         {"processes": [{"user": "root", "pid": 1, "cpu": "0.0", "mem": "0.2", "name": "init", "cmdline": "/sbin/init"}, {"user": "root", "pid": 512, "cpu": "0.0", "mem": "0.5", "name": "sshd", "cmdline": "/usr/sbin/sshd -D"}]},
        "ports":             {"ports": [{"local_addr": "0.0.0.0:22", "process": "sshd"}, {"local_addr": "0.0.0.0:443", "process": "nginx"}, {"local_addr": "0.0.0.0:51820", "process": "wg"}]},
        "history":           {"history": ["pfctl -sr", "ifconfig", "netstat -rn"]},
        "auth_keys":         {"content": "", "hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
        "config_changes":    {"files": ["/etc/pf.conf", "/etc/rc.conf"]},
        "sudo_rules":        {"content": "root ALL=(ALL:ALL) ALL\n"},
    }


def _load_inventory(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text())
    return data.get("teams", [])


def _load_iocs() -> list[tuple[str, int, str]]:
    from fastapi_backend.core.ioc_definition import IOCDefinitionLoader
    loader = IOCDefinitionLoader()
    loader.load_ioc_definitions()
    return [(ioc.name, ioc.difficulty, ioc.os) for ioc in loader.ioc_definitions.values()]


def _clear_all(session: Session) -> None:
    for table in [
        "ioc_check_result", "check_instance",
        "dev_sessions", "dev_beacons",
        "sa_results", "credentials",
        "portfwd_mappings", "pivot_sessions",
        "implant_builds", "boxes",
    ]:
        session.execute(text(f"DELETE FROM {table}"))
    session.execute(text("UPDATE blue_teams SET last_check_id = NULL, total_score = 0"))
    session.commit()
    print("Cleared all seeded data.")


def _seed_blue_teams(session: Session, teams: list[dict]) -> None:
    for team in teams:
        t = session.get(BlueTeams, team["team_num"])
        if not t:
            session.add(BlueTeams(team_num=team["team_num"], last_check_id=None, total_score=0))
    session.commit()


def _seed_boxes_and_creds(session: Session, teams: list[dict]) -> None:
    import os as _os
    infra_username = _os.getenv("RED_SCORING_USERNAME", "red_scoring")
    infra_password_override = _os.getenv("RED_SCORING_PASSWORD", "")
    for team in teams:
        for box in team["boxes"]:
            b = session.execute(select(Boxes).where(Boxes.box_ip == box["ip"])).scalar_one_or_none() or Boxes()
            b.box_ip   = box["ip"]
            b.box_name = box["name"]
            b.os       = box["os"]
            b.team_num = team["team_num"]
            # Domain defaults must match inventory_manager._parse_config so the
            # cred_harvester's DC + golden ticket paths can find their targets
            # after the seeder runs (conftest calls seed(clear=True) AFTER the
            # backend's orchestrator load, which would otherwise wipe the
            # is_domain_controller flag set during inventory parsing).
            is_windows = box["os"].lower() == "windows"
            default_dc = is_windows and box["name"].endswith("-1")
            default_domain = f"team{team['team_num']}.c3" if is_windows else None
            b.is_domain_joined     = box.get("is_domain_joined", is_windows)
            b.is_domain_controller = box.get("is_domain_controller", default_dc)
            b.domain               = box.get("domain", default_domain)
            session.merge(b)
            session.flush()

            os_ = box["os"]
            if os_ == "firewall":
                protocol = "ssh"
                session.add(Credentials(username=box["username"], password=box["password"],
                                        box_ip=box["ip"], protocol=protocol, is_default=True, valid=True))
            elif os_ == "windows":
                protocol = "smb"
                session.add(Credentials(username=box["username"], password=box["password"],
                                        box_ip=box["ip"], protocol=protocol, is_default=True, valid=True))
            elif os_ == "linux":
                protocol = "ssh"
                session.add(Credentials(username=box["username"], password=box["password"],
                                        box_ip=box["ip"], protocol=protocol, is_default=True, valid=True))
            else:
                protocol = "ssh"

            # Mirror orchestrator._seed_scoring_creds so the IOC check pipeline
            # (_best_credential with RED_SCORING_ENABLED=True) has an
            # is_infrastructure=True row after _clear_all() wipes the table.
            domain_joined = box.get("is_domain_joined", is_windows)
            if is_windows and domain_joined:
                du = box.get("domain_username", "")
                netbios = du.split("\\")[0] if "\\" in du else ""
                infra_user = f"{netbios}\\{infra_username}" if netbios else infra_username
                infra_pw = infra_password_override or box.get("domain_password") or box["password"]
            else:
                infra_user = infra_username
                infra_pw = infra_password_override or box["password"]
            session.add(Credentials(username=infra_user, password=infra_pw,
                                    box_ip=box["ip"], protocol=protocol,
                                    is_default=False, is_infrastructure=True, valid=True))
    session.commit()
    print(f"  Seeded {sum(len(t['boxes']) for t in teams)} boxes and credentials.")


def _seed_sa_results(session: Session, teams: list[dict]) -> None:
    now = datetime.datetime.now(datetime.UTC)
    count = 0
    for team in teams:
        for box in team["boxes"]:
            os_ = box["os"]
            sa_map = _win_sa(box["name"]) if os_ == "windows" else (_fw_sa(box["name"]) if os_ == "firewall" else _lin_sa(box["name"]))
            for category, data in sa_map.items():
                session.add(SAResult(box_ip=box["ip"], category=category,
                                     collected_at=now, data=json.dumps(data)))
                count += 1
    session.commit()
    print(f"  Seeded {count} SA result entries.")


def _seed_ioc_checks(session: Session, teams: list[dict], iocs: list, num_checks: int) -> None:
    now = datetime.datetime.now(datetime.UTC)
    for team in teams:
        boxes = [(b["ip"], b["os"]) for b in team["boxes"]]
        last_check_id = None
        for check_round in range(num_checks):
            ts = now - datetime.timedelta(minutes=5 * (num_checks - check_round))
            check = CheckInstance(blue_team_num=team["team_num"], timestamp=ts, score=0)
            session.add(check)
            session.flush()
            for ioc_name, difficulty, ioc_os in iocs:
                matching = [ip for ip, os_ in boxes if os_ == ioc_os]
                for box_ip in matching:
                    remediation_chance = check_round / num_checks
                    roll = random.random()
                    if roll < 0.05:
                        status = -1
                    elif roll < 0.05 + remediation_chance * 0.6:
                        status = 0
                    else:
                        status = 1
                    session.add(IOCCheckResult(
                        check_instance_id=check.check_id, box_ip=box_ip,
                        ioc_name=ioc_name, difficulty=difficulty, status=status,
                        error="Simulated check error" if status == -1 else None,
                    ))
            last_check_id = check.check_id
        t = session.get(BlueTeams, team["team_num"])
        if t:
            t.last_check_id = last_check_id
        session.commit()
    print(f"  Seeded IOC check history for {len(teams)} teams ({num_checks} rounds each).")


def _seed_dev_sessions_beacons(session: Session, teams: list[dict], pivot_team_nums: set[int]) -> None:
    now_unix = int(datetime.datetime.now(datetime.UTC).timestamp())
    sessions_seeded = beacons_seeded = 0

    for team in teams:
        fw_box = next((b for b in team["boxes"] if b["os"] == "firewall"), None)
        non_fw = [b for b in team["boxes"] if b["os"] != "firewall"]

        for box in non_fw:
            roll = random.random()
            if roll < 0.35:
                continue  # no implant
            is_dead = roll > 0.85  # top ~15% are dead implants
            is_pivot = team["team_num"] in pivot_team_nums
            last_ago = random.randint(60, 300)  # 1–5 min ago

            if box["os"] == "linux" or random.random() < 0.4:
                # Seed a session
                remote_addr = box["ip"] + ":44321"
                if is_pivot and fw_box:
                    remote_addr = f"{fw_box['ip']}:9898->{box['ip']}:44321->TCP"
                session.add(DevSession(
                    id=str(uuid.uuid4()),
                    name=f"IMPLANT_{box['name'].upper().replace('-', '_')}",
                    hostname=box["name"],
                    os="windows" if box["os"] == "windows" else "linux",
                    arch="amd64",
                    pid=random.randint(1000, 9999),
                    remote_address=remote_addr,
                    transport=random.choice(["mtls", "https"]),
                    is_dead=is_dead,
                    last_checkin=datetime.datetime.fromtimestamp(
                        now_unix - last_ago, tz=datetime.UTC
                    ).isoformat(),
                    box_name=box["name"],
                    team_num=team["team_num"],
                    box_ip=box["ip"],
                ))
                sessions_seeded += 1
            else:
                # Seed a beacon
                session.add(DevBeacon(
                    id=str(uuid.uuid4()),
                    name=f"BEACON_{box['name'].upper().replace('-', '_')}",
                    hostname=box["name"],
                    os="windows" if box["os"] == "windows" else "linux",
                    arch="amd64",
                    pid=random.randint(1000, 9999),
                    remote_address=box["ip"] + ":44322",
                    transport="mtls",
                    interval=60_000_000_000,  # 60s in nanoseconds
                    jitter=0.1,
                    last_checkin=now_unix - last_ago,
                    next_checkin=now_unix - last_ago + 60,
                    is_dead=is_dead,
                    box_name=box["name"],
                    team_num=team["team_num"],
                    box_ip=box["ip"],
                ))
                beacons_seeded += 1

    session.commit()
    print(f"  Seeded {sessions_seeded} dev sessions, {beacons_seeded} dev beacons.")


def _seed_pivot_sessions(session: Session, teams: list[dict], pivot_team_nums: set[int]) -> None:
    count = 0
    for team in teams:
        if team["team_num"] not in pivot_team_nums:
            continue
        fw_box = next((b for b in team["boxes"] if b["os"] == "firewall"), None)
        if not fw_box:
            continue
        pivot = PivotSessions(
            team_num=team["team_num"],
            pfsense_session_id=str(uuid.uuid4()),
            pfsense_box_ip=fw_box["ip"],
            pfsense_pivot_ip="127.0.0.1",
            tcp_pivot_port=9898,
            created_at=datetime.datetime.now(datetime.UTC),
            active=True,
        )
        session.add(pivot)
        session.flush()
        internal_boxes = [b for b in team["boxes"] if b["os"] != "firewall"]
        for idx, box in enumerate(sorted(internal_boxes, key=lambda b: b["ip"])):
            session.add(PortfwdMappings(
                pivot_session_id=pivot.id,
                box_ip=box["ip"],
                smb_local_port=10000 + idx * 2,
                ssh_local_port=10000 + idx * 2 + 1,
                created_at=datetime.datetime.now(datetime.UTC),
            ))
        count += 1
    session.commit()
    print(f"  Seeded {count} pivot session(s).")


def _seed_implant_builds(session: Session) -> None:
    now = datetime.datetime.now(datetime.UTC)
    builds = [
        ("windows", "beacon",  "amd64", "exe", "mtls://192.168.1.100:8888", "implant_windows_beacon_amd64.exe"),
        ("linux",   "session", "amd64", "elf", "mtls://192.168.1.100:8888", "implant_linux_session_amd64"),
        ("windows", "session", "amd64", "exe", "https://192.168.1.100:443", "implant_windows_session_amd64.exe"),
        ("linux",   "beacon",  "amd64", "elf", "http://192.168.1.100:80",   "implant_linux_beacon_amd64"),
    ]
    for os_, itype, arch, fmt, c2_url, filename in builds:
        session.add(ImplantBuilds(
            id=str(uuid.uuid4()),
            os=os_, implant_type=itype, arch=arch, format=fmt,
            c2_url=c2_url, filename=filename,
            created_at=now - datetime.timedelta(hours=random.randint(1, 48)),
        ))
    session.commit()
    print(f"  Seeded {len(builds)} implant builds.")


def seed(
    inventory_path: Path | None = None,
    num_checks: int = 10,
    clear: bool = False,
    db_path: Path | None = None,
    # Legacy kwargs accepted by conftest — ignored in new logic
    num_teams: int | None = None,  # noqa: ARG001
) -> None:
    resolved_db = db_path or DB_PATH
    if not resolved_db.exists():
        print(f"Database not found at {resolved_db}. Start the app first to initialize it.")
        sys.exit(1)

    resolved_inventory = inventory_path or (_PROJECT_ROOT / "config" / "inventory.yml")
    teams = _load_inventory(resolved_inventory)
    if not teams:
        print(f"No teams found in {resolved_inventory}. Aborting.")
        sys.exit(1)
    print(f"Loaded {len(teams)} teams from {resolved_inventory}.")

    iocs = _load_iocs()
    if not iocs:
        print("No IOC definitions found. Skipping IOC check seeding.")

    pivot_team_nums = {teams[i]["team_num"] for i in PIVOT_TEAM_INDICES if i < len(teams)}

    engine = _make_engine(f"sqlite:///{resolved_db}")
    with Session(engine) as session:
        if clear:
            _clear_all(session)

        _seed_blue_teams(session, teams)
        _seed_boxes_and_creds(session, teams)
        _seed_sa_results(session, teams)
        if iocs:
            _seed_ioc_checks(session, teams, iocs, num_checks)
        _seed_dev_sessions_beacons(session, teams, pivot_team_nums)
        _seed_pivot_sessions(session, teams, pivot_team_nums)
        _seed_implant_builds(session)

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed dummy data for frontend development")
    parser.add_argument("--clear",     action="store_true", help="Wipe all seeded data first")
    parser.add_argument("--checks",    type=int, default=10, help="IOC check rounds per team (default: 10)")
    parser.add_argument("--inventory", type=str, default="config/inventory.yml",
                        help="Path to inventory YAML (default: config/inventory.yml)")
    parser.add_argument("--db",        type=str, default=None,
                        help="SQLite DB path to seed (default: db/database.db)")
    args = parser.parse_args()

    seed(
        inventory_path=Path(args.inventory),
        num_checks=args.checks,
        clear=args.clear,
        db_path=Path(args.db) if args.db else None,
    )
