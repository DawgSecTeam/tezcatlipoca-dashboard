"""Coordinates the competition lifecycle and owns all background tasks."""

import asyncio
import contextlib
import datetime as dt
import logging
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from fastapi_backend.core.playbook_loader import PlaybookConfig  # lazy-load
    from fastapi_backend.netexec.wrapper import NXCWrapper  # lazy-load

from fastapi_backend.c2.alerts import emit_alert
from fastapi_backend.core.check_manager import CheckManager
from fastapi_backend.core.competition_state import CompetitionState, CompetitionStatus
from fastapi_backend.core.config import config
from fastapi_backend.core.deploy_manager import DeployManager
from fastapi_backend.core.inventory_manager import Box, InventoryManager, RedOpsAccount
from fastapi_backend.core.ioc_definition import IOCDefinitionLoader
from fastapi_backend.core.playbook_loader import load_playbook
from fastapi_backend.core.playbook_scheduler import (
    _playbook_tasks,
    clear_playbook_jobs,
    register_playbook_jobs,
)
from fastapi_backend.core.scheduler import CheckScheduler
from fastapi_backend.database.db_init import DatabaseInitializer
from fastapi_backend.database.db_writer import (
    DATABASE_URL,
    create_db_and_tables,
    engine,
)
from fastapi_backend.database.models import (
    Boxes,
    CompetitionConfig,
    Credentials,
    DeployedIOCs,
    assign_blue_team_label,
)
from fastapi_backend.netexec.executor import NXCCheckExecutor
from fastapi_backend.utils.asyncio_tasks import fire_and_forget


def _td_to_hms(td: dt.timedelta | None) -> str | None:
    """Render a non-negative timedelta as HH:MM:SS."""
    if td is None:
        return None
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _scoring_rows_for_box(box: Box, *, default_username: str, cfg_domain: str, env_pw: str) -> list[dict]:
    """Desired red_scoring cred row(s) for one box.

    The SINGLE source of truth for how a box's scoring identity is derived, shared by
    ``_seed_scoring_creds`` (which upserts these into the DB) and ``derive_scoring_cred``
    (which returns one in-memory when the seeded row is transiently absent). Each dict
    is ``{username, password, domain, is_default, protocol}``. Mirrors
    ``scripts/infra/create_red_scoring.py``.
    """
    is_windows = box.os == "windows"
    protocol = "smb" if is_windows else "ssh"
    username = box.scoring_username or default_username

    # LOCAL account row (domain=None) — bare username, local admin password. Used for
    # workgroup Windows, Linux, firewalls, AND (preferred) domain member boxes.
    local_pw = box.scoring_password or env_pw or box.password
    local_row = {
        "username": username, "password": local_pw,
        "domain": None, "is_default": False, "protocol": protocol,
    }
    if is_windows and box.is_domain_joined:
        # Domain account: qualify with the same NetBIOS prefix as the operator
        # domain cred so NXC does domain auth.
        netbios = (
            box.domain_username.split("\\")[0]
            if box.domain_username and "\\" in box.domain_username
            else cfg_domain
        )
        domain_row = {
            "username": f"{netbios}\\{username}" if netbios else username,
            "password": (
                box.scoring_password or env_pw
                or box.domain_password or box.password
            ),
            "domain": netbios or None, "is_default": False, "protocol": protocol,
        }
        if box.is_domain_controller:
            # A DC has no local SAM — domain identity only.
            return [domain_row]
        # Member: seed BOTH. Local is preferred (is_default=True → wins the
        # _get_best_cred scoring tiebreak) so member scoring needs no DC round-trip;
        # the domain row is the fallback when the local account is invalid/removed.
        # The local username carries the `.\` prefix so plain NXC does local-SAM auth
        # instead of resolving the bare name against the domain. See
        # docs/PLAN_scoring_cred_resilience.md.
        local_row["username"] = f".\\{username}"
        local_row["is_default"] = True
        return [local_row, domain_row]
    # Workgroup Windows / Linux / firewall — local identity only.
    return [local_row]


def _red_ops_rows_for_box(box: Box, account: RedOpsAccount) -> list[dict]:
    r"""Rows to seed for one red_ops OPERATOR account on one box.

    Mirrors ``_scoring_rows_for_box``'s local/domain split, but for an
    ``is_infrastructure=False`` operator identity, and — unlike red_scoring —
    also seeds a domain (UPN) row for domain-joined *Linux* members, because
    red_ops accounts are declared to exist as domain users on every
    domain-joined box (matches ``_seed_default_creds``'s Linux-domain handling).
    Each dict is ``{username, password, domain, protocol}``;
    ``is_infrastructure=False`` / ``is_default=False`` / ``source="red_ops"`` are
    applied by the caller. See docs/PLAN_red_ops_accounts.md §5 for the full
    identity matrix.

    Cross-forest override — when ``account.foreign_domain`` is set (e.g.
    ``mira.corp.sus`` for a rogue-DC user reached via an established forest
    trust), the local/NETBIOS matrix is bypassed and a single row targeting
    Windows DCs is emitted with ``domain=<FQDN>`` so NXC dials
    ``-u <user> -d <FQDN>``. Non-DC / non-Windows boxes get no rows for
    foreign accounts: members and Linux/firewall either can't validate the
    cross-realm PAC or wouldn't confer any privilege even if they could
    (see RedOpsAccount docstring).
    """
    # Cross-forest fast-path: bypass the local/NETBIOS matrix entirely.
    if account.foreign_domain:
        if box.os != "windows" or not box.is_domain_joined or not box.is_domain_controller:
            return []
        return [{
            "username": account.username,
            "password": account.password,
            "domain": account.foreign_domain,
            "protocol": "smb",
        }]

    protocol = "smb" if box.os == "windows" else "ssh"
    user, pw = account.username, account.password

    if box.os == "windows":
        netbios = (
            box.domain_username.split("\\")[0]
            if box.domain_username and "\\" in box.domain_username
            else None
        )
        domain_row = {
            "username": f"{netbios}\\{user}" if netbios else user,
            "password": pw, "domain": netbios or None, "protocol": protocol,
        }
        if box.is_domain_joined and box.is_domain_controller:
            # DC has no local SAM → domain identity only (nothing if the account
            # opted out of domain rows or the box has no NetBIOS name).
            return [domain_row] if (account.domain and netbios) else []
        local_row = {
            "username": f".\\{user}", "password": pw,
            "domain": None, "protocol": protocol,
        }
        if box.is_domain_joined and account.domain and netbios:
            return [local_row, domain_row]
        return [local_row]

    # Linux / firewall — local identity always; UPN domain row for Linux members.
    rows = [{"username": user, "password": pw, "domain": None, "protocol": protocol}]
    if box.os == "linux" and box.is_domain_joined and account.domain and box.domain:
        # domain=None so the NXC ssh path adds no `-d` flag; SSSD resolves
        # user@realm (mirrors _seed_default_creds's Linux-domain handling).
        rows.append(
            {"username": f"{user}@{box.domain}", "password": pw,
             "domain": None, "protocol": protocol}
        )
    return rows


class CompetitionOrchestrator:
    """Main orchestrator for the competition.
    
    Thin coordinator that delegates to DeployManager and CheckManager.
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        self.state = CompetitionState()

        # Core components
        self.db_init = DatabaseInitializer(database_url=DATABASE_URL)
        self.inventory_manager = InventoryManager()
        self.ioc_loader = IOCDefinitionLoader()
        self.executor: NXCCheckExecutor | None = None
        self.scheduler = CheckScheduler(
            check_interval_minutes=config.CHECK_INTERVAL_MINUTES
        )
        self.cred_revalidation_task: asyncio.Task | None = None
        self.scoring_cred_probe_task: asyncio.Task | None = None
        # Wall-clock duration of the most recent _revalidate_all_creds() call,
        # surfaced on /admin/status as `cred_revalidation_last_cycle_duration_ms`.
        # None until the first cycle completes.
        self._last_revalidate_duration_ms: float | None = None
        # `_playbook_config` holds the full PlaybookConfig (schedule + entries).
        # `_playbook_entries` retained as a convenience alias to the entries
        # list — many call sites only care about action entries.
        from fastapi_backend.core.playbook_loader import (  # cycle-break
            PlaybookConfig,
        )
        self._playbook_config = PlaybookConfig()
        self._playbook_entries: list = []  # loaded at startup, registered on RUNNING

        # SA collection metrics — updated by main.py background task
        self.sa_stats: dict = {
            "cycles_completed": 0,
            "cycles_failed": 0,
            "last_run_started_at": None,   # ISO string
            "last_run_duration_s": None,   # float
            "last_error": None,            # str
        }

        # Set by _restore_competition_status_on_boot() when a persisted
        # RUNNING/PAUSED status is found on boot. Surfaced on /admin/status
        # as components.competition_restart. All-None/False when this boot
        # was not a resume.
        self._restart_resume_info: dict[str, Any] = {
            "occurred": False,
            "persisted_status": None,
            "resumed_as": None,
            "reason": None,
            "restored_at": None,
        }

        # Optional components
        self.script_manager = None

        # Delegating managers
        self.deploy_manager = DeployManager(self)
        self.check_manager = CheckManager(self)

        # Set orchestrator reference in state
        self.state.orchestrator = self

    async def initialize(self) -> None:
        """Initialize all competition components."""
        try:
            self._set_status(CompetitionStatus.INITIALIZING)
            self.logger.info("Initializing competition orchestrator...")

            # 1. Initialize database
            self.logger.info("Step 1: Initializing database...")
            await self._initialize_database()

            # 2. Load inventory
            self.logger.info("Step 2: Loading inventory...")
            await self._load_inventory()

            # 3. Load IOC definitions + playbook
            self.logger.info("Step 3: Loading IOC definitions...")
            await self._load_iocs()
            self._load_playbook()

            # Pre-deployed IOCs (deploy_script=None, planted out-of-band by
            # infra scripts) auto-register as virtual DeployedIOCs rows so
            # scoring picks them up like any other IOC. Runs AFTER _load_iocs
            # because it iterates the IOC definitions to find pre_deployed=True.
            self._seed_pre_deployed_iocs()

            # 4. Initialize executor with worker threads
            self.logger.info("Step 4: Initializing check executor...")
            await self._initialize_executor()

            # 5. Set up scheduler
            self.logger.info("Step 5: Setting up scheduler...")
            self.scheduler.set_callback(self.run_checks)

            # 6. Start background credential re-validation loop (gated: the
            #    pytest-xdist harness disables it — N per-worker backends each
            #    bare-auth-flooding every domain box is the proven win2 NETBIOS
            #    source; see config.CRED_REVALIDATION_ENABLED).
            if config.CRED_REVALIDATION_ENABLED:
                self.cred_revalidation_task = asyncio.create_task(
                    self._cred_revalidation_loop()
                )
            else:
                self.logger.info(
                    "Credential re-validation loop disabled "
                    "(CRED_REVALIDATION_ENABLED=false)"
                )

            # 6b. Start the dedicated red_scoring liveness probe loop (infra
            # creds are excluded from the operator revalidation loop above).
            self.scoring_cred_probe_task = asyncio.create_task(
                self._scoring_cred_probe_loop()
            )

            # Update state
            self.state.num_teams = len(self.inventory_manager.teams)
            self.state.num_boxes = len(self.inventory_manager.all_boxes)
            self.state.num_iocs = len(self.ioc_loader.ioc_definitions)

            # Restore persisted competition_start_at so playbook scheduler can
            # compute correct run_dates after a mid-competition backend restart.
            self.restore_competition_start_at()
            # Capture the persisted status BEFORE the NOT_STARTED write below
            # overwrites the column — the restore decision below needs the
            # value that was in flight when the process last stopped.
            stored_status = self._read_persisted_status()
            self._set_status(CompetitionStatus.NOT_STARTED)
            # Re-register schedule jobs from CompetitionConfig if armed_start_time
            # is set (may flip state back to PREFLIGHT_ARMED).
            self._restore_schedule_jobs_on_boot()
            await self._restore_competition_status_on_boot(stored_status)

            self.logger.info("Orchestrator initialization complete")

        except Exception as e:
            self.logger.error(f"Failed to initialize orchestrator: {e}")
            self._set_status(CompetitionStatus.ERROR)
            raise

    async def _initialize_database(self) -> None:
        """Initialize database and create tables/views."""
        try:
            create_db_and_tables()
            self.db_init.initialize_database()
        except Exception as e:
            self.logger.error(f"Database initialization failed: {e}")
            raise

    async def reload_inventory(self) -> None:
        """Re-read INVENTORY_FILE and resync Boxes/creds/teams. Requires competition stopped.

        Unlike start_competition(), which never re-reads INVENTORY_FILE, this
        re-runs the same idempotent upsert+prune load used at boot — needed so a
        test worker that rewrote its bound-team inventory subset file (or an
        operator who edited config/inventory.yml) can pick it up without a
        backend restart.
        """
        if self.state.is_active():
            raise RuntimeError("Cannot reload inventory while competition is running")
        # controlled_write: reload is gated on the competition being STOPPED (above),
        # so _load_inventory's box/cred/team/account seeds are single-threaded — no
        # scoring writer runs concurrently. Exempt them from the runtime single-writer
        # guard (the same seeds already carry `# db-actor-exempt` static markers).
        # _load_inventory is await-free, so this thread-scoped flag cannot leak across
        # a coroutine switch.
        from fastapi_backend.database.write_guard import controlled_write  # cycle-break
        with controlled_write("inventory reload"):
            await self._load_inventory()
        self.state.num_teams = len(self.inventory_manager.teams)
        self.state.num_boxes = len(self.inventory_manager.all_boxes)

    async def _load_inventory(self) -> None:
        """Load team and box inventory."""
        try:
            self.inventory_manager.load_from_config()

            # Apply the inventory's `c2_settings:` (script_server_url /
            # sliver_host / c2_ip) onto the config singleton before
            # resolve_c2_ip()/refresh_script_server_url() run — a single
            # inventory file now carries the C2 network identity, so switching
            # inventories no longer means hand-editing those .env keys. Explicit
            # env values still win (see InventoryManager.apply_c2_settings).
            self.inventory_manager.apply_c2_settings()

            # Publish the box_ip -> outbound(wan) IP map so the script-wrapper
            # builders bind fetch tokens to a firewall's WAN source IP, not its
            # LAN box_ip (docs/PLAN_firewall_wan_lan.md).
            from fastapi_backend.netexec.script_wrapper import (
                set_box_fetch_source_ips,  # cycle-break
                set_box_wan_ips,  # cycle-break
            )
            set_box_wan_ips({
                b.ip: b.wan_ip
                for b in self.inventory_manager.all_boxes
                if b.wan_ip
            })
            # Populate the per-box fetch-source map from EXPLICIT inventory
            # `fetch_source_ip` fields only. Firewalls implicitly use their own
            # wan_ip (already covered by _WAN_IP_BY_BOX fallback below); members
            # only need this when a NAT'ing firewall rewrites their source on
            # the way to Kali (see Box.fetch_source_ip docstring for why not
            # every range needs it).
            #
            # Auto-derive-from-fw-wan (the original cad659a behaviour) is
            # deliberately gone: it assumed every non-firewall member NAT'd out
            # via its team firewall, but ranges that expose Kali on the LAN
            # itself (dev inventory) or route Kali directly without NAT (post-
            # rollback testing_inventory 2026-09-11 — 365 signature-mismatch
            # 401s per deploy-all because tokens signed with fw wan_ip while
            # actual client_ip = box LAN IP) broke wholesale. Explicit-only
            # keeps both range shapes working: set fetch_source_ip in inventory
            # when NAT'd, leave unset otherwise (defaults to box_ip).
            _fetch_src: dict[str, str] = {}
            for _b in self.inventory_manager.all_boxes:
                if _b.os == "firewall":
                    # Firewall's own fetch source == its wan_ip (already
                    # covered by the wan_ip map fallback, but set explicitly
                    # for clarity).
                    if _b.wan_ip:
                        _fetch_src[_b.ip] = _b.wan_ip
                    continue
                fs = getattr(_b, "fetch_source_ip", None)
                if fs:
                    _fetch_src[_b.ip] = fs
            set_box_fetch_source_ips(_fetch_src)

            # Upsert boxes into the Boxes table so the C2 layer can see them
            with Session(engine) as session:
                for box in self.inventory_manager.all_boxes:
                    existing = session.execute(
                        select(Boxes).where(Boxes.box_ip == box.ip)
                    ).scalar_one_or_none()
                    if existing:
                        existing.box_name = box.name
                        existing.os = box.os
                        existing.team_num = box.team_num
                        existing.is_domain_joined = box.is_domain_joined
                        existing.is_domain_controller = box.is_domain_controller
                        existing.domain = box.domain
                        existing.wan_ip = box.wan_ip
                    else:
                        session.add(
                            Boxes(
                                box_ip=box.ip,
                                box_name=box.name,
                                os=box.os,
                                team_num=box.team_num,
                                is_domain_joined=box.is_domain_joined,
                                is_domain_controller=box.is_domain_controller,
                                domain=box.domain,
                                wan_ip=box.wan_ip,
                            )
                        )
                session.commit()  # db-actor-exempt: startup seed (single-threaded, pre-serving)

            # Prune boxes no longer in the loaded inventory. Switching
            # INVENTORY_FILE (live-fire -> dev) must not leave stale rows the
            # background health/check/SA loops keep probing — they iterate the
            # `boxes` table, not the inventory file (a dev soak once hit 9 stale
            # live-fire boxes still in the DB).
            self.db_init.prune_stale_boxes(
                [box.ip for box in self.inventory_manager.all_boxes]
            )

            # Seed default operator credentials for each box from inventory.
            self._seed_default_creds()

            # Seed fleet-wide red_ops OPERATOR creds (is_infrastructure=False),
            # declared via the inventory's `red_ops_accounts:` list. Distinct
            # from red_scoring below; operator-usable, IOC-pipeline-invisible.
            self._seed_red_ops_creds()

            # Seed the red_scoring infrastructure cred + per-box default-cred
            # virtual scoring IOCs (both idempotent).
            self._seed_scoring_creds()
            self._seed_default_cred_iocs()

            # Seed teams and user accounts from actual inventory
            team_nums = sorted({
                b.team_num for b in self.inventory_manager.all_boxes
                if b.team_num is not None
            })
            self.db_init.initialize_teams(team_nums)
            self.logger.info("Initializing webpage accounts...")
            self.db_init.initialize_accounts(team_nums)

        except Exception as e:
            self.logger.error(f"Inventory loading failed: {e}")
            raise

    def _seed_default_creds(self) -> None:
        """Seed the default operator credential for each box from inventory.

        Idempotent: skips a box that already has an operator default cred. The
        query MUST exclude infrastructure rows — a domain member's local
        red_scoring cred also carries ``is_default=True`` (its scoring tiebreak),
        so a plain ``box_ip + is_default`` lookup would match two rows and raise
        ``MultipleResultsFound`` on any re-seed / backend restart.
        """
        with Session(engine) as session:
            for box in self.inventory_manager.all_boxes:
                existing = session.execute(
                    select(Credentials).where(
                        Credentials.box_ip == box.ip,
                        Credentials.is_default == True,  # noqa: E712
                        Credentials.is_infrastructure == False,  # noqa: E712
                    )
                ).scalar_one_or_none()
                if not existing:
                    is_windows = box.os == "windows"
                    protocol = "smb" if is_windows else "ssh"
                    if is_windows:
                        username = box.domain_username or box.username
                        password = box.domain_password or box.password
                    elif box.is_domain_joined and box.domain_username:
                        # Linux domain member: authenticate as the domain user.
                        # domain_username holds the UPN form (user@realm); SSH
                        # carries it verbatim and SSSD resolves it. cred.domain
                        # stays None so the NXC ssh path adds no `-d` flag.
                        username = box.domain_username
                        password = box.domain_password or box.password
                    else:
                        username = box.username
                        password = box.password
                    session.add(
                        Credentials(
                            username=username,
                            password=password,
                            box_ip=box.ip,
                            protocol=protocol,
                            is_default=True,
                            valid=None,
                        )
                    )
            session.commit()  # db-actor-exempt: startup seed (single-threaded, pre-serving)

    def _seed_scoring_creds(self) -> None:
        r"""Seed the red_scoring infrastructure credential, one row per box.

        The IOC pipeline authenticates ONLY with these rows (is_infrastructure
        =True). This mirrors exactly what ``scripts/infra/create_red_scoring.py``
        provisions ON the boxes:

        - Windows domain-joined → ``TEAM<N>\<username>`` (NetBIOS prefix taken
          from the operator ``domain_username``). Linux/firewall → bare
          username.
        - Three-tier precedence, same order for username and password
          (docs/PLAN_scoring_creds_deploy_manager.md):
            1. Per-box ``inventory.yml`` override — ``Box.scoring_username`` /
               ``Box.scoring_password``. Lets an environment where the scoring
               account's password differs from the box's admin password
               declare it explicitly, per box.
            2. Global default — inventory's ``scoring_account.username``
               stanza (username only) / ``RED_SCORING_USERNAME`` +
               ``RED_SCORING_PASSWORD`` in ``.env``.
            3. Legacy fallback (password only, kept for zero regression against
               the originally-shipped behavior) — derive from this box's own
               admin password (``box.domain_password`` / ``box.password``),
               i.e. the same password ``create_red_scoring.py`` sets by
               default when no explicit scoring password is configured.

        Idempotent: upserts on (box_ip, protocol, is_infrastructure=True),
        re-syncing username/password/domain so re-runs/.env edits take effect.
        Gated by RED_SCORING_ENABLED and the stanza's ``enabled`` flag.
        """
        sa = self.inventory_manager.scoring_account
        if not config.RED_SCORING_ENABLED or (sa is not None and not sa.enabled):
            self.logger.info("red_scoring seeding disabled — skipping")
            return
        default_username = sa.username if sa and sa.username else config.RED_SCORING_USERNAME
        cfg_domain = sa.domain if sa and sa.domain else config.RED_SCORING_DOMAIN
        env_pw = config.RED_SCORING_PASSWORD  # "" -> no global override (tier 2)
        seeded = 0
        with Session(engine) as session:
            for box in self.inventory_manager.all_boxes:
                protocol = "smb" if box.os == "windows" else "ssh"
                desired = _scoring_rows_for_box(
                    box, default_username=default_username,
                    cfg_domain=cfg_domain, env_pw=env_pw,
                )

                for d in desired:
                    is_local = d["domain"] is None
                    domain_filter = (
                        Credentials.domain.is_(None)
                        if is_local
                        else Credentials.domain.isnot(None)
                    )
                    existing = session.execute(
                        select(Credentials).where(
                            Credentials.box_ip == box.ip,
                            Credentials.protocol == protocol,
                            Credentials.is_infrastructure == True,  # noqa: E712
                            domain_filter,
                        )
                    ).scalar_one_or_none()
                    if existing:
                        existing.username = d["username"]
                        existing.password = d["password"]
                        existing.domain = d["domain"]
                        existing.is_default = d["is_default"]
                    else:
                        session.add(
                            Credentials(
                                username=d["username"],
                                password=d["password"],
                                box_ip=box.ip,
                                protocol=protocol,
                                is_default=d["is_default"],
                                is_infrastructure=True,
                                valid=None,
                                domain=d["domain"],
                                source="red_scoring",
                            )
                        )
                        seeded += 1
            session.commit()  # db-actor-exempt: startup seed (single-threaded, pre-serving)
        self.logger.info(
            "Seeded red_scoring cred for %d new box(es) (default user=%s)",
            seeded, default_username,
        )

    def _seed_red_ops_creds(self) -> None:
        r"""Seed fleet-wide red_ops OPERATOR credentials from inventory.

        Every enabled ``red_ops_accounts:`` entry is seeded on EVERY box as a
        local (and, for domain-joined boxes, a domain) ``Credentials`` row with
        ``is_infrastructure=False``, ``is_default=False``, ``source="red_ops"``.
        These are operator-visible (``GET /c2/creds``) and selectable by every
        operator + C3rberus red-team path, but the IOC pipeline never resolves
        them (it filters ``is_infrastructure==True``). The per-box baseline
        operator cred (``_seed_default_creds``) keeps ``is_default=True``, so it
        still wins the ``_get_best_cred`` tiebreak — red_ops is a secondary,
        explicitly-selectable identity.

        Idempotent + self-pruning: upserts on
        ``(box_ip, protocol, username, source="red_ops")``, so re-seeds /
        reloads / .yml edits re-sync passwords without duplicating rows;
        ``source="red_ops"`` rows for a box whose username is no longer desired
        (account removed or renamed) are deleted. Excluded from the proactive
        ``_revalidate_all_creds`` loop by design (docs/PLAN_red_ops_accounts.md
        §6 D4) — validity is resolved on demand.
        """
        accounts = [a for a in self.inventory_manager.red_ops_accounts if a.enabled]
        seeded = 0
        with Session(engine) as session:
            for box in self.inventory_manager.all_boxes:
                protocol = "smb" if box.os == "windows" else "ssh"
                desired: dict[str, dict] = {}
                for account in accounts:
                    for d in _red_ops_rows_for_box(box, account):
                        desired[d["username"]] = d  # last-writer-wins on dup user

                for username, d in desired.items():
                    existing = session.execute(
                        select(Credentials).where(
                            Credentials.box_ip == box.ip,
                            Credentials.protocol == protocol,
                            Credentials.username == username,
                            Credentials.source == "red_ops",
                        )
                    ).scalar_one_or_none()
                    if existing:
                        existing.password = d["password"]
                        existing.domain = d["domain"]
                        existing.is_default = False
                        existing.is_infrastructure = False
                    else:
                        session.add(
                            Credentials(
                                username=username,
                                password=d["password"],
                                box_ip=box.ip,
                                protocol=protocol,
                                is_default=False,
                                is_infrastructure=False,
                                valid=None,
                                domain=d["domain"],
                                source="red_ops",
                            )
                        )
                        seeded += 1

                # Prune stale red_ops rows for this box (account removed/renamed).
                stale = session.execute(
                    select(Credentials).where(
                        Credentials.box_ip == box.ip,
                        Credentials.protocol == protocol,
                        Credentials.source == "red_ops",
                        Credentials.username.notin_(list(desired.keys()) or [""]),
                    )
                ).scalars().all()
                for row in stale:
                    session.delete(row)
            session.commit()  # db-actor-exempt: startup/reload seed (single-threaded, pre-serving)
        self.logger.info(
            "Seeded %d new red_ops operator cred row(s) across %d account(s)",
            seeded, len(accounts),
        )

    def derive_scoring_cred(self, box_ip: str) -> tuple[str, str] | None:
        r"""Return ``(username, password)`` for a box's red_scoring identity, or None.

        Computed IN-MEMORY from inventory (no DB read/write) — None if the box isn't in
        the current inventory or scoring is disabled.

        Fallback for the inject-persistence path: the seeded infra cred ROW can be
        transiently absent because the test harness's per-worker inventory rebinding
        prunes every non-slice box + its creds (``reload_inventory`` →
        ``prune_stale_boxes``) then reseeds, and an inject deploy landing in that
        window finds no row (``include_invalid`` can't help — the row is deleted, not
        invalidated). Deriving in-memory avoids a write that would race the single
        writer actor (and the DB-lock flake that can cause). Not a prod path — a
        single full-inventory backend seeds once — but it keeps a scored IOC from
        hard-failing on a harness race. Prefers the ``is_default`` row (a member's
        local ``.\\`` identity), matching ``_get_best_cred``'s ordering.
        """
        if not config.RED_SCORING_ENABLED:
            return None
        sa = self.inventory_manager.scoring_account
        if sa is not None and not sa.enabled:
            return None
        box = next((b for b in self.inventory_manager.all_boxes if b.ip == box_ip), None)
        if box is None:
            return None
        default_username = sa.username if sa and sa.username else config.RED_SCORING_USERNAME
        cfg_domain = sa.domain if sa and sa.domain else config.RED_SCORING_DOMAIN
        env_pw = config.RED_SCORING_PASSWORD
        rows = _scoring_rows_for_box(
            box, default_username=default_username, cfg_domain=cfg_domain, env_pw=env_pw,
        )
        if not rows:
            return None
        row = next((r for r in rows if r["is_default"]), rows[0])
        return row["username"], row["password"]

    def _seed_default_cred_iocs(self) -> None:
        """Auto-register one protected virtual default-cred IOC per box.

        Scores whether the box's OS-default operator account (Administrator/root)
        still authenticates. Modeled as a DeployedIOCs row (source='virtual',
        status='active', difficulty=3) so it reuses every scoring view unchanged.
        Idempotent: one row per (box_ip, ioc_name=default_cred_<os>). Protected
        from operator deregistration (DELETE rejects source='virtual').
        """
        created = 0
        with Session(engine) as session:
            for box in self.inventory_manager.all_boxes:
                ioc_name = f"default_cred_{box.os}"
                existing = session.execute(
                    select(DeployedIOCs).where(
                        DeployedIOCs.box_ip == box.ip,
                        DeployedIOCs.ioc_name == ioc_name,
                    )
                ).scalar_one_or_none()
                if existing:
                    continue
                session.add(
                    DeployedIOCs(
                        box_ip=box.ip,
                        ioc_name=ioc_name,
                        persistence_mechanism=None,
                        blue_team_label=assign_blue_team_label(session, box.os, 3),
                        deployed_at=dt.datetime.now(dt.UTC),
                        deployed_by="system",
                        status="active",
                        source="virtual",
                        difficulty=3,
                    )
                )
                created += 1
            session.commit()  # db-actor-exempt: startup seed (single-threaded, pre-serving)
        self.logger.info("Seeded %d default-cred virtual scoring IOC(s)", created)

    def _seed_pre_deployed_iocs(self) -> None:
        """Auto-register virtual DeployedIOCs rows for pre_deployed IOCs.

        Pre-deployed IOCs (``iocs.base.IOC.pre_deployed``) have no
        ``deploy_script`` — the underlying red-team artefact is planted
        out-of-band by an infra script (e.g.
        ``scripts/infra/rogue_dc_admin_persist.py`` for the rogue-trust IOC
        family). This helper creates the ``DeployedIOCs`` rows so the
        scheduler picks up the check on every cycle, exactly as if the IOC
        had been deployed through the normal pipeline.

        Eligibility per IOC:
          * OS must match ``box.os``
          * ``requires_dc=True`` restricts to ``box.is_domain_controller``
          * ``disabled=True`` skips the IOC entirely
        Row shape mirrors ``_seed_default_cred_iocs``: ``source="virtual"``,
        ``status="active"``, ``deployed_by="system"``. Idempotent — one row
        per ``(box_ip, ioc_name)`` tuple. Rows are protected from operator
        deregistration (``DELETE /c2/iocs/deployed`` rejects source='virtual').
        """
        created = 0
        pre = [
            ioc for ioc in self.ioc_loader.ioc_definitions.values()
            if getattr(ioc, "pre_deployed", False) and not getattr(ioc, "disabled", False)
        ]
        if not pre:
            return
        with Session(engine) as session:
            for ioc in pre:
                for box in self.inventory_manager.all_boxes:
                    if box.os != ioc.os:
                        continue
                    if ioc.requires_dc and not box.is_domain_controller:
                        continue
                    existing = session.execute(
                        select(DeployedIOCs).where(
                            DeployedIOCs.box_ip == box.ip,
                            DeployedIOCs.ioc_name == ioc.name,
                        )
                    ).scalar_one_or_none()
                    if existing:
                        continue
                    session.add(
                        DeployedIOCs(
                            box_ip=box.ip,
                            ioc_name=ioc.name,
                            persistence_mechanism=None,
                            blue_team_label=assign_blue_team_label(
                                session, box.os, ioc.difficulty,
                            ),
                            deployed_at=dt.datetime.now(dt.UTC),
                            deployed_by="system",
                            status="active",
                            source="virtual",
                            difficulty=ioc.difficulty,
                        )
                    )
                    created += 1
            session.commit()  # db-actor-exempt: startup seed (single-threaded, pre-serving)
        self.logger.info(
            "Seeded %d pre-deployed virtual IOC row(s) across %d definition(s)",
            created, len(pre),
        )

    async def _load_iocs(self) -> None:
        """Load IOC definitions from YAML files."""
        try:
            self.ioc_loader.load_ioc_definitions()
        except FileNotFoundError as e:
            self.logger.error(f"No complete IOCs available: {e}")
            self.logger.warning("Continuing with empty IOC set for MVP")
        except Exception as e:
            self.logger.error(f"IOC loading failed: {e}")
            raise

    def _load_playbook(self) -> None:
        """Load playbook YAML from config/playbook.yml into self._playbook_config."""
        from fastapi_backend.core.playbook_loader import (  # cycle-break
            PlaybookConfig,
            PlaybookValidationError,  # cycle-break
        )

        playbook_path = config.PROJECT_ROOT / "config" / "playbook.yml"
        try:
            cfg = load_playbook(playbook_path)
            self._playbook_config = cfg
            self._playbook_entries = cfg.entries
            if cfg.entries:
                self.logger.info(
                    "Playbook loaded: %d entries from %s",
                    len(cfg.entries),
                    playbook_path,
                )
            if cfg.start_time:
                self.logger.info(
                    "Playbook competition schedule: start=%s end_offset=%s pause_offset=%s resume_offset=%s",
                    cfg.start_time.isoformat(),
                    cfg.end_time,
                    cfg.pause_at,
                    cfg.resume_at,
                )
        except PlaybookValidationError as exc:
            self.logger.error("Playbook validation failed — no jobs will run: %s", exc)
            self._playbook_config = PlaybookConfig()
            self._playbook_entries = []
        except Exception as exc:
            self.logger.error("Playbook load error: %s", exc)
            self._playbook_config = PlaybookConfig()
            self._playbook_entries = []

    async def _initialize_executor(self) -> None:
        """Initialize the check executor with worker threads."""
        try:
            from fastapi_backend.netexec.wrapper import NXCWrapper  # lazy-load

            self.executor = NXCCheckExecutor(num_workers=config.NUM_WORKERS)
            self.executor.ioc_definitions = self.ioc_loader.ioc_definitions
            self.executor.os_ioc_mapping = self.ioc_loader.os_ioc_mapping
            self.executor.nxc_wrapper = NXCWrapper(
                nxc_path=config.NXC_PATH,
                default_timeout=config.NXC_TIMEOUT,
            )
            # Start the DB writer subprocess at boot, not at competition start.
            # The SA collection loop and `POST /c2/sa/{ip}/collect` both enqueue
            # rows via `db_writer_manager.put_sa_result(...)`; if it isn't
            # running yet, those writes are silently dropped.
            self.executor.start_db_writer()
        except Exception as e:
            self.logger.error(f"Executor initialization failed: {e}")
            raise

    async def start_competition(self) -> None:
        """Transition to RUNNING (admin bypass — any state with valid transition).

        Tears down preflight check pipeline + clears preflight markers if we
        were in PREFLIGHT_ON/PREFLIGHT_ARMED.
        """
        if not self.state.can_transition_to(CompetitionStatus.RUNNING):
            raise RuntimeError(
                f"Cannot start competition from status: {self.state.status.value}"
            )

        self.logger.info("Starting competition (from %s)...", self.state.status.value)

        # If we were in preflight, stop the preflight scheduler before starting
        # the real one so we don't have two running.
        was_preflight = self.state.preflight
        if was_preflight and self.scheduler and self.scheduler.running:
            await self.scheduler.stop()

        # Discard any stale retry tasks — their check_ids reference check_instances
        # that will be deleted on reset_data, and SQLite reuses IDs without AUTOINCREMENT
        if self.executor:
            self.executor.retry_queue.clear()

        self._set_status(CompetitionStatus.RUNNING)
        self._persist_competition_start_at(self.state.start_time)

        if self.executor:
            self.executor.start_workers()

        await self.scheduler.start()

        # Register playbook jobs relative to competition_start_at.
        if self._playbook_entries and self.state.start_time:
            n = register_playbook_jobs(self._playbook_entries, self.state.start_time)
            self.logger.info("Competition started with %d playbook job(s)", n)
        else:
            self.logger.info("Competition started (no playbook entries)")

        # Cancel any armed-start APScheduler job since manual start happened.
        await self._cancel_schedule_job("armed_start")

        self.logger.info("Competition started successfully")

    # -----------------------------------------------------------------------
    # Preflight + state-machine helpers
    # -----------------------------------------------------------------------

    def _transition_or_raise(self, target: CompetitionStatus) -> None:
        """Guard rail: raise ValueError if transition not allowed."""
        if not self.state.can_transition_to(target):
            raise ValueError(
                f"Invalid state transition {self.state.status.name} → {target.name}"
            )

    async def start_preflight(self) -> None:
        """STOPPED / NOT_STARTED → PREFLIGHT_ON.

        Starts a check scheduler that writes preflight=True results — they
        bypass scoring views.
        """
        self._transition_or_raise(CompetitionStatus.PREFLIGHT_ON)
        self.logger.info("Starting preflight (from %s)...", self.state.status.value)
        self._set_status(CompetitionStatus.PREFLIGHT_ON)
        if self.executor:
            self.executor.start_workers()
        if self.scheduler and not self.scheduler.running:
            await self.scheduler.start()

    async def stop_preflight(self) -> None:
        """PREFLIGHT_ON / PREFLIGHT_ARMED → STOPPED.

        Drains the check pipeline (workers + scheduler) and cancels any
        pending arm-start job.
        """
        if self.state.status not in (
            CompetitionStatus.PREFLIGHT_ON,
            CompetitionStatus.PREFLIGHT_ARMED,
        ):
            raise ValueError(
                f"Cannot stop preflight from state: {self.state.status.value}"
            )
        self.logger.info("Stopping preflight (from %s)...", self.state.status.value)
        await self._cancel_schedule_job("armed_start")
        if self.scheduler and self.scheduler.running:
            await self.scheduler.stop()
        if self.executor:
            self.executor.stop_workers()
        self._clear_armed_start_time()
        self._set_status(CompetitionStatus.STOPPED)

    async def arm_preflight(self, start_time: dt.datetime) -> None:
        """PREFLIGHT_ON → PREFLIGHT_ARMED with explicit start_time.

        Persists `armed_start_time` + schedule offsets, registers APScheduler
        date jobs.
        """
        self._transition_or_raise(CompetitionStatus.PREFLIGHT_ARMED)
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=dt.UTC)

        # Validate resume_at > pause_at if both set on playbook.
        cfg = self._playbook_config
        if cfg.pause_at and cfg.resume_at and cfg.resume_at <= cfg.pause_at:
            raise ValueError(
                f"Playbook resume_at ({cfg.resume_at}) must be greater than pause_at ({cfg.pause_at})"
            )

        await self._persist_arm_schedule(start_time, cfg)
        await self._register_arm_jobs(start_time, cfg)
        self._set_status(CompetitionStatus.PREFLIGHT_ARMED)
        self.logger.info("Preflight armed: start_time=%s", start_time.isoformat())

    async def disarm_preflight(self) -> None:
        """PREFLIGHT_ARMED → PREFLIGHT_ON.

        Cancels pending APScheduler date jobs + clears `armed_start_time`.
        """
        self._transition_or_raise(CompetitionStatus.PREFLIGHT_ON)
        await self._cancel_schedule_job("armed_start")
        await self._cancel_schedule_job("scheduled_end")
        await self._cancel_schedule_job("scheduled_pause")
        await self._cancel_schedule_job("scheduled_resume")
        self._clear_armed_start_time()
        self._set_status(CompetitionStatus.PREFLIGHT_ON)

    async def rollback_to_preflight(self) -> None:
        """RUNNING → PREFLIGHT_ON (emergency rollback).

        Aborts in-flight cycle (drops pending BoxIOCTask futures) and pauses
        the scheduler. Existing scored rows from completed cycles remain.
        """
        self._transition_or_raise(CompetitionStatus.PREFLIGHT_ON)
        self.logger.warning("Rolling back from RUNNING to PREFLIGHT_ON")
        if self.scheduler and self.scheduler.running:
            await self.scheduler.pause()
        if self.executor:
            # Drop everything queued; existing in-flight workers see preflight=False
            # for current cycle but the next cycle re-queues with preflight=True.
            try:
                while not self.executor.task_queue.empty():
                    self.executor.task_queue.get_nowait()
            except Exception:
                pass
        clear_playbook_jobs()
        self._set_status(CompetitionStatus.PREFLIGHT_ON)

    async def purge_preflight_rows(self) -> int:
        """Delete every IOCCheckResult + CheckInstance row where preflight=True."""
        from sqlalchemy import delete  # cycle-break

        from fastapi_backend.database.models import (  # cycle-break
            CheckInstance,
            IOCCheckResult,
        )
        from fastapi_backend.database.write_actor import write_actor  # cycle-break

        def _purge(db: Session) -> tuple[int, int]:
            n_r = db.execute(
                delete(IOCCheckResult).where(IOCCheckResult.preflight == True),  # noqa: E712
            ).rowcount or 0
            n_c = db.execute(
                delete(CheckInstance).where(CheckInstance.preflight == True),  # noqa: E712
            ).rowcount or 0
            return n_r, n_c
        n_results, n_checks = await write_actor.submit(_purge, label="purge_preflight_rows")
        self.logger.info("Preflight purge: deleted %d IOC results + %d check instances", n_results, n_checks)
        return n_results

    def preflight_row_count(self) -> int:
        """Return total preflight=True IOCCheckResult rows."""
        from sqlalchemy import func  # cycle-break

        from fastapi_backend.database.models import (  # cycle-break
            IOCCheckResult,
        )

        with Session(engine) as db:
            cnt = db.execute(
                select(func.count(IOCCheckResult.ioc_id)).where(IOCCheckResult.preflight == True)  # noqa: E712
            ).scalar_one()
        return int(cnt or 0)

    # -----------------------------------------------------------------------
    # APScheduler date-job tracking for schedule columns
    # -----------------------------------------------------------------------

    def _schedule_jobs(self) -> dict:
        """Lazy-init dict of named asyncio.Task entries for schedule jobs."""
        if not hasattr(self, "_active_schedule_jobs"):
            self._active_schedule_jobs = {}
        return self._active_schedule_jobs

    async def _cancel_schedule_job(self, name: str) -> None:
        jobs = self._schedule_jobs()
        task = jobs.pop(name, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _persist_arm_schedule(self, start_time: dt.datetime, cfg: "PlaybookConfig") -> None:
        """Write start_time + offset-derived absolute datetimes to CompetitionConfig."""
        from fastapi_backend.database.write_actor import write_actor  # cycle-break

        def _wr(db: Session) -> None:
            row = db.get(CompetitionConfig, 1)
            if row is None:
                row = CompetitionConfig(id=1)
                db.add(row)
            row.armed_start_time = start_time.isoformat()
            row.scheduled_end_time = (
                (start_time + cfg.end_time).isoformat() if cfg.end_time else None
            )
            row.scheduled_pause_at = (
                (start_time + cfg.pause_at).isoformat() if cfg.pause_at else None
            )
            row.scheduled_resume_at = (
                (start_time + cfg.resume_at).isoformat() if cfg.resume_at else None
            )
        await write_actor.submit(_wr, label="_persist_arm_schedule")

    def _clear_armed_start_time(self) -> None:
        from fastapi_backend.database.write_actor import write_actor  # cycle-break

        def _wr(db: Session) -> None:
            row = db.get(CompetitionConfig, 1)
            if row is None:
                return
            row.armed_start_time = None
            row.scheduled_end_time = None
            row.scheduled_pause_at = None
            row.scheduled_resume_at = None
        write_actor.submit_sync(_wr, label="_clear_armed_start_time")

    async def _register_arm_jobs(self, start_time: dt.datetime, cfg: "PlaybookConfig") -> None:
        """Register asyncio.create_task date jobs for each schedule column."""
        now = dt.datetime.now(dt.UTC)
        await self._cancel_schedule_job("armed_start")
        await self._cancel_schedule_job("scheduled_end")
        await self._cancel_schedule_job("scheduled_pause")
        await self._cancel_schedule_job("scheduled_resume")

        plan: list[tuple[str, dt.datetime | None, str]] = [
            ("armed_start", start_time, "start"),
            ("scheduled_end", (start_time + cfg.end_time) if cfg.end_time else None, "end"),
            ("scheduled_pause", (start_time + cfg.pause_at) if cfg.pause_at else None, "pause"),
            ("scheduled_resume", (start_time + cfg.resume_at) if cfg.resume_at else None, "resume"),
        ]
        for name, at, kind in plan:
            if at is None:
                continue
            delay = (at - now).total_seconds()
            if delay <= 0:
                self.logger.warning(
                    "Schedule %s @ %s is in the past — not registering", name, at.isoformat()
                )
                continue
            task = asyncio.create_task(self._run_schedule_job(name, delay, kind))
            self._schedule_jobs()[name] = task

    async def _run_schedule_job(self, name: str, delay: float, kind: str) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self.logger.info("Schedule job %s firing (kind=%s)", name, kind)
        try:
            if kind == "start":
                await self._on_competition_start()
            elif kind == "end":
                await self._on_competition_end()
            elif kind == "pause":
                await self._on_scheduled_pause()
            elif kind == "resume":
                await self._on_scheduled_resume()
        except Exception as exc:
            self.logger.error("Schedule job %s failed: %s", name, exc)

    async def _on_competition_start(self) -> None:
        if self.state.status in (
            CompetitionStatus.PREFLIGHT_ARMED,
            CompetitionStatus.PREFLIGHT_ON,
        ):
            await self.start_competition()

    async def _on_competition_end(self) -> None:
        # Don't force-stop if paused — operator may be working through an issue.
        if self.state.status == CompetitionStatus.PAUSED:
            self.logger.info("scheduled end fired during PAUSED — skipping")
            return
        if self.state.status == CompetitionStatus.RUNNING:
            await self.stop_competition()

    async def _on_scheduled_pause(self) -> None:
        if self.state.status == CompetitionStatus.RUNNING:
            await self.pause_competition()

    async def _on_scheduled_resume(self) -> None:
        if self.state.status == CompetitionStatus.PAUSED:
            await self.unpause_competition()

    def _restore_schedule_jobs_on_boot(self) -> None:
        """If CompetitionConfig has future-dated schedule entries on boot, re-register."""
        try:
            with Session(engine) as db:
                row = db.get(CompetitionConfig, 1)
        except Exception as exc:
            # Tolerate boots where the column hasn't been migrated yet — log and exit.
            self.logger.debug("Schedule restore skipped: %s", exc)
            return
        if row is None:
            return
        now = dt.datetime.now(dt.UTC)

        def _parse(value: str | None) -> dt.datetime | None:
            if not value:
                return None
            try:
                d = dt.datetime.fromisoformat(value)
            except ValueError:
                return None
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.UTC)
            return d

        armed = _parse(row.armed_start_time)
        if armed is not None and armed > now:
            self._set_status(CompetitionStatus.PREFLIGHT_ARMED)
            delay = (armed - now).total_seconds()
            self._schedule_jobs()["armed_start"] = asyncio.create_task(
                self._run_schedule_job("armed_start", delay, "start")
            )
            for name, kind in (
                ("scheduled_end", "end"),
                ("scheduled_pause", "pause"),
                ("scheduled_resume", "resume"),
            ):
                at = _parse(getattr(row, name))
                if at is None or at <= now:
                    continue
                self._schedule_jobs()[name] = asyncio.create_task(
                    self._run_schedule_job(name, (at - now).total_seconds(), kind)
                )
        elif armed is not None and armed <= now:
            # Stays PREFLIGHT_ARMED — operator must manually start; no job.
            self._set_status(CompetitionStatus.PREFLIGHT_ARMED)

    async def stop_competition(self) -> None:
        """Stop the competition."""
        self.logger.info("Stopping competition...")
        clear_playbook_jobs()
        await self.scheduler.stop()
        if self.executor:
            self.executor.stop_workers()
        self._set_status(CompetitionStatus.STOPPED)
        self.logger.info("Competition stopped")

    async def pause_competition(self) -> None:
        """Pause the competition."""
        if self.state.status != CompetitionStatus.RUNNING:
            raise RuntimeError(
                f"Cannot pause competition from status: {self.state.status}"
            )

        self.logger.info("Pausing competition...")
        clear_playbook_jobs()
        if self.scheduler and self.scheduler.running:
            await self.scheduler.pause()
        self._set_status(CompetitionStatus.PAUSED)
        self.logger.info(
            "Competition paused - running checks will complete but no new checks will start"
        )

    async def unpause_competition(self) -> None:
        """Unpause the competition."""
        if self.state.status != CompetitionStatus.PAUSED:
            raise RuntimeError(
                f"Cannot unpause competition from status: {self.state.status}"
            )

        self.logger.info("Unpausing competition...")
        if self.scheduler:
            await self.scheduler.resume()
        self._set_status(CompetitionStatus.RUNNING)
        self.logger.info("Competition unpaused - check scheduling resumed")

    # --- Delegated to CheckManager ---

    async def run_checks(self) -> None:
        """Run checks for all teams and boxes."""
        await self.check_manager.run_checks()

    # --- Delegated to DeployManager ---

    async def deploy_iocs(self, exclude_pairs: set[tuple[str, str]] | None = None) -> dict[str, Any]:
        """Deploy all IOCs to all target systems via nxc."""
        return await self.deploy_manager.deploy_iocs(exclude_pairs=exclude_pairs)

    async def deploy_iocs_selective(
        self, ioc_names: list[str], target_boxes: list[str] | None = None
    ) -> dict[str, Any]:
        """Selectively deploy specific IOCs to specific boxes via nxc."""
        return await self.deploy_manager.deploy_iocs_selective(ioc_names, target_boxes)

    async def redeploy_iocs_selective(
        self, ioc_names: list[str], target_boxes: list[str] | None = None
    ) -> dict[str, Any]:
        """Re-deploy IOCs during an active competition (no state restriction)."""
        return await self.deploy_manager.deploy_iocs_selective(ioc_names, target_boxes)

    async def remediate_iocs_selective(
        self, ioc_names: list[str], target_boxes: list[str] | None = None,
        *, retire_rows: bool = True,
    ) -> dict[str, Any]:
        """Execute remediate scripts for specified IOCs on specified boxes.

        retire_rows=False = blue-clear: leave the DeployedIOCs row active (the
        check then reports effect_status=0 and keeps scoring it). See
        SelectiveDeployRequest.retire_rows.
        """
        return await self.deploy_manager.remediate_iocs_selective(
            ioc_names, target_boxes, retire_rows=retire_rows)

    # --- Competition config persistence ---

    def _persist_competition_start_at(self, start_time: dt.datetime | None) -> None:
        """Write competition_start_at to DB so it survives backend restarts."""
        if start_time is None:
            return
        from fastapi_backend.database.write_actor import write_actor  # cycle-break

        def _wr(db: Session) -> None:
            row = db.get(CompetitionConfig, 1)
            if row is None:
                row = CompetitionConfig(id=1)
                db.add(row)
            row.competition_start_at = start_time.isoformat()
        write_actor.submit_sync(_wr, label="_persist_competition_start_at")

    def restore_competition_start_at(self) -> None:
        """On startup, read persisted competition_start_at into CompetitionState.

        Called during orchestrator initialization so playbook scheduler can
        compute correct run_dates after a mid-competition backend restart.
        """
        try:
            with Session(engine) as db:
                row = db.get(CompetitionConfig, 1)
                stored = row.competition_start_at if row else None
        except Exception as exc:
            self.logger.debug("competition_start_at restore skipped: %s", exc)
            return
        if stored:
            try:
                self.state.start_time = dt.datetime.fromisoformat(stored)
                self.logger.info(f"Restored competition_start_at from DB: {stored}")
            except ValueError:
                self.logger.warning(
                    f"Invalid competition_start_at in DB: {stored!r} — ignored"
                )

    def _set_status(self, status: CompetitionStatus) -> None:
        """Set in-memory status AND persist it so a restart can restore it."""
        self.state.set_status(status)
        self._persist_competition_status(status)

    def _persist_competition_status(self, status: CompetitionStatus) -> None:
        """Write status.value to CompetitionConfig.

        Never raises — a persistence failure must not block an in-memory state
        transition (mirrors _persist_competition_start_at's tolerance).

        INITIALIZING and ERROR are transient boot states; persisting them
        would overwrite a meaningful 'running'/'paused' value left by the
        previous process and break the restart-resume logic in
        _restore_competition_status_on_boot (which reads the persisted value
        to decide whether to replay start/pause after a crash).
        """
        if status in (CompetitionStatus.INITIALIZING, CompetitionStatus.ERROR):
            return
        from fastapi_backend.database.write_actor import write_actor  # cycle-break

        def _wr(db: Session) -> None:
            row = db.get(CompetitionConfig, 1)
            if row is None:
                row = CompetitionConfig(id=1)
                db.add(row)
            row.status = status.value
        try:
            write_actor.submit_sync(_wr, label="_persist_competition_status")
        except Exception as exc:
            self.logger.debug("competition status persist skipped: %s", exc)

    def _read_persisted_status(self) -> str | None:
        """Read the raw persisted status string, or None if absent/unreadable.

        Must be called before the boot-default NOT_STARTED write overwrites
        the column (see initialize()).
        """
        try:
            with Session(engine) as db:
                row = db.get(CompetitionConfig, 1)
                return row.status if row else None
        except Exception as exc:
            self.logger.debug("competition status read skipped: %s", exc)
            return None

    def _scheduled_end_already_passed(self) -> bool:
        """True if CompetitionConfig.scheduled_end_time is set and <= now."""
        try:
            with Session(engine) as db:
                row = db.get(CompetitionConfig, 1)
                raw = row.scheduled_end_time if row else None
        except Exception:
            return False
        if not raw:
            return False
        try:
            end = dt.datetime.fromisoformat(raw)
        except ValueError:
            return False
        if end.tzinfo is None:
            end = end.replace(tzinfo=dt.UTC)
        return end <= dt.datetime.now(dt.UTC)

    async def _restore_competition_status_on_boot(self, stored_status: str | None) -> None:
        """Resume RUNNING/PAUSED after a mid-competition backend restart.

        Only RUNNING and PAUSED are actively restored here. NOT_STARTED/
        STOPPED need no action; PREFLIGHT_ON/PREFLIGHT_ARMED/ERROR are owned
        by _restore_schedule_jobs_on_boot() / operator action, not this path.
        """
        if stored_status not in ("running", "paused"):
            return

        if self._scheduled_end_already_passed():
            self._set_status(CompetitionStatus.STOPPED)
            self._restart_resume_info = {
                "occurred": True,
                "persisted_status": stored_status,
                "resumed_as": "stopped",
                "reason": "scheduled_end_passed",
                "restored_at": dt.datetime.now(dt.UTC).isoformat(),
            }
            self.logger.warning(
                "Backend restarted with persisted status=%s but scheduled_end_time "
                "already passed while down — resuming as STOPPED",
                stored_status,
            )
            fire_and_forget(emit_alert(
                kind="competition_resumed_after_restart",
                summary=f"Backend restarted; scheduled end already passed — "
                        f"competition stopped (was {stored_status})",
                details=dict(self._restart_resume_info),
            ))
            return

        try:
            await self.start_competition()
        except Exception as exc:
            self.logger.error("Auto-resume to RUNNING failed after restart: %s", exc)
            return

        resumed_as = "running"
        if stored_status == "paused":
            try:
                await self.pause_competition()
                resumed_as = "paused"
            except Exception as exc:
                self.logger.error("Failed to re-pause after restart-resume: %s", exc)

        self._restart_resume_info = {
            "occurred": True,
            "persisted_status": stored_status,
            "resumed_as": resumed_as,
            "reason": None,
            "restored_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        self.logger.warning(
            "Competition auto-resumed to %s after backend restart (was %s)",
            resumed_as.upper(), stored_status,
        )
        fire_and_forget(emit_alert(
            kind="competition_resumed_after_restart",
            summary=f"Backend restarted mid-competition; auto-resumed to {resumed_as.upper()}",
            details=dict(self._restart_resume_info),
        ))

    # --- Background tasks ---

    async def _cred_revalidation_loop(self) -> None:
        """Background loop: re-validate all box credentials every N minutes."""
        while True:
            try:
                await asyncio.sleep(config.CRED_REVALIDATION_INTERVAL_MINUTES * 60)
                await self._revalidate_all_creds()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Credential re-validation loop error: {e}")

    async def _revalidate_all_creds(self) -> None:
        """Test all box-scoped credentials via NXC and update their valid flags."""
        from fastapi_backend.core.metrics import (  # cycle-break
            record_loop_run,
        )
        record_loop_run("cred_revalidation")
        if not self.executor or not self.executor.nxc_wrapper:
            return
        wrapper = self.executor.nxc_wrapper
        # Wrap the full body in try/finally so the duration metric updates
        # whether the cycle completes cleanly or raises mid-gather. Soak
        # Phase 7 reads this through /admin/status as a diagnostic signal.
        _t0 = time.monotonic()
        # Use a dedicated, lower concurrency cap for cred revalidation so this
        # background task doesn't saturate host RAM with 40 concurrent nxc
        # subprocesses (each ~150-200 MB).
        semaphore = asyncio.Semaphore(config.CRED_REVALIDATION_MAX_CONCURRENT)

        try:
            with Session(engine) as session:
                # Join with Boxes to get team_num for alerts
                creds_query = session.execute(
                    select(Credentials, Boxes.team_num)
                    .join(Boxes, Credentials.box_ip == Boxes.box_ip)
                    .where(
                        Credentials.box_ip.isnot(None),
                        # Scoring-infrastructure creds are covered by the
                        # dedicated _scoring_cred_probe_loop, not this loop
                        # (keeps their failures out of operator cred_invalid
                        # alert noise).
                        Credentials.is_infrastructure == False,  # noqa: E712
                        # Only password creds — check_creds auths with `-u -p`, so an
                        # nthash row (empty password → always fails) or a ccache is a
                        # wasted, wrong bare-auth here (was ~63% of this loop's SMB
                        # auth + the win2 NETBIOS-timeout flood). nthash is validated
                        # by HashHarvestLoop's `--shares` probe and ccache by
                        # TicketRefreshLoop — no fidelity lost. See
                        # docs/PLAN_cred_loop_efficiency.md.
                        Credentials.cred_type == "password",
                        # red_ops operator creds are excluded from the proactive
                        # loop — bare-authing ~1.3k rows (18 teams × 85 boxes ×
                        # 12 accounts) would flood SMB/SSH auth + trip the
                        # NETBIOS rate-limiter on any invalid account
                        # (docs/PLAN_red_ops_accounts.md §6 D4). Validity is
                        # resolved on demand. NULL-safe so legacy (source IS
                        # NULL) rows stay covered.
                        or_(
                            Credentials.source.is_(None),
                            Credentials.source != "red_ops",
                        ),
                    )
                ).all()
                creds_snapshot = [
                    (c.cred_id, c.box_ip, c.protocol, c.username, c.password, c.valid, team_num)
                    for c, team_num in creds_query
                ]

            async def check_one(
                cred_id: int,
                box_ip: str,
                protocol: str,
                username: str,
                password: str,
                prev_valid: bool | None,
                team_num: int | None,
            ) -> tuple[int, bool | None]:
                # Defer if the box is in circuit-breaker cooldown. Bare-auth
                # cred-loop probes on a fragile box just re-trip NETBIOS + delay
                # recovery; the next 5-min cycle will retry when the breaker
                # has cleared. See docs/PLAN_cred_loop_cooldown_defer.md.
                # Return valid=None to signal "no verdict this cycle" — the
                # bulk update filters these out, so the row's existing valid
                # is preserved.
                from fastapi_backend.netexec.nxc_semaphore import (
                    box_cooldown_active,  # cycle-break
                )
                if box_cooldown_active(box_ip):
                    self.logger.info(
                        "cred-loop: box %s in cooldown, skipping check_creds probe this cycle",
                        box_ip,
                    )
                    return cred_id, None
                async with semaphore:
                    result = await wrapper.check_creds(box_ip, protocol, username, password)
                    if prev_valid is True and not result.success:
                        self.logger.warning(
                            f"Credential {cred_id} for {box_ip} is no longer valid"
                        )
                        # Emit cred_invalid alert
                        fire_and_forget(emit_alert(
                            "cred_invalid",
                            f"Credentials invalid on {box_ip}",
                            box_ip=box_ip,
                            team_num=team_num,
                            details={"cred_id": cred_id, "username": username, "protocol": protocol}
                        ))
                    return cred_id, result.success

            results = await asyncio.gather(*[check_one(*c) for c in creds_snapshot])

            # Single bulk UPDATE keyed on PK instead of one SELECT+write per cred.
            # Filter deferred (v is None) rows — no verdict this cycle, preserve
            # existing valid. See docs/PLAN_cred_loop_cooldown_defer.md.
            from fastapi_backend.database.write_actor import write_actor  # cycle-break
            rows = [{"cred_id": cid, "valid": v} for cid, v in results if v is not None]
            if rows:
                def _bulk(db: Session, __rows=rows) -> None:
                    db.execute(update(Credentials), __rows)
                await write_actor.submit(_bulk, label="revalidate_credentials_bulk")

            # D.2 — domain-scoped operator creds (box_ip NULL) are not box-scoped
            # and were skipped above; revalidate them against every domain box.
            await self._revalidate_domain_creds(wrapper, semaphore)

            self.logger.info(f"Credential re-validation complete: {len(results)} checked")
        finally:
            self._last_revalidate_duration_ms = (time.monotonic() - _t0) * 1000.0

    async def _revalidate_domain_creds(
        self, wrapper: "NXCWrapper", semaphore: asyncio.Semaphore
    ) -> None:
        """Revalidate domain-scoped operator creds against every domain box (D1).

        A domain cred (box_ip NULL, team_num + domain set, cred_type != ccache)
        is probed against every domain-joined box in its team sharing its domain.
        valid=True iff >=1 box authenticates AND no reachable box returns a
        definitive auth-reject; unreachable boxes are ignored so a transient
        outage can't false-invalidate a working domain cred.
        """
        from fastapi_backend.netexec.check_runner import (  # cycle-break
            classify_cred_probe,
        )
        from fastapi_backend.netexec.wrapper import protocol_for_box  # cycle-break

        with Session(engine) as session:
            domain_rows = session.execute(
                select(Credentials).where(
                    Credentials.box_ip.is_(None),
                    Credentials.domain.isnot(None),
                    Credentials.team_num.isnot(None),
                    Credentials.is_infrastructure == False,  # noqa: E712
                    Credentials.cred_type != "ccache",
                )
            ).scalars().all()
            domain_snapshot = [
                (c.cred_id, c.team_num, c.domain, c.protocol, c.username,
                 c.password, c.cred_type, c.nthash, c.valid)
                for c in domain_rows
            ]
            if not domain_snapshot:
                return
            boxes = session.execute(
                select(Boxes.box_ip, Boxes.team_num, Boxes.domain, Boxes.os).where(
                    Boxes.is_domain_joined == True,  # noqa: E712
                    Boxes.domain.isnot(None),
                )
            ).all()

        # Group domain-joined boxes by (team_num, domain) → [(box_ip, protocol)].
        box_map: dict[tuple[int, str], list[tuple[str, str]]] = {}
        for box_ip, team_num, domain, os_name in boxes:
            proto = protocol_for_box(os_name)[0]
            box_map.setdefault((team_num, domain), []).append((box_ip, proto))

        async def revalidate_one(
            cred_id: int,
            team_num: int,
            domain: str,
            protocol: str,
            username: str,
            password: str | None,
            cred_type: str,
            nthash: str | None,
            prev_valid: bool | None,
        ) -> tuple[int, bool | None, bool | None, int, str]:
            targets = [
                (ip, bproto)
                for ip, bproto in box_map.get((team_num, domain), [])
                if bproto == protocol
            ]
            if not targets:
                return cred_id, prev_valid, prev_valid, team_num, domain
            probe_cred = Credentials(
                username=username, password=password or "", protocol=protocol,
                cred_type=cred_type, nthash=nthash, domain=domain,
            )
            verdicts: list[int] = []
            for ip, bproto in targets:
                cmd = "whoami" if bproto == "smb" else "id"
                async with semaphore:
                    # nxc-wrapper-bypass: cred-validation — domain-cred liveness probe,
                    # not an IOC-script delivery.
                    result = await wrapper.execute_cred(ip, bproto, probe_cred, cmd)  # nxc-wrapper-bypass: cred-validation
                verdicts.append(classify_cred_probe(result))
            reachable = [v for v in verdicts if v != -1]
            if not reachable:
                new_valid = prev_valid          # all offline → leave unchanged
            elif any(v == 0 for v in reachable):
                new_valid = False               # a definitive reject anywhere
            else:
                new_valid = True                # >=1 success, no rejects
            return cred_id, new_valid, prev_valid, team_num, domain

        outcomes = await asyncio.gather(
            *[revalidate_one(*c) for c in domain_snapshot]
        )

        # Single bulk UPDATE keyed on PK instead of one SELECT+write per cred.
        from fastapi_backend.database.write_actor import write_actor  # cycle-break
        rows = [{"cred_id": cid, "valid": nv} for cid, nv, _pv, _tn, _dm in outcomes]
        if rows:
            def _bulk(db: Session, __rows=rows) -> None:
                db.execute(update(Credentials), __rows)
            await write_actor.submit(_bulk, label="revalidate_domain_creds_bulk")

        # Alert emission is independent of the DB write (fires on validity drop).
        for cred_id, new_valid, prev_valid, team_num, domain in outcomes:
            if prev_valid is True and new_valid is False:
                self.logger.warning(
                    f"Domain credential {cred_id} ({domain}) is no longer valid"
                )
                fire_and_forget(emit_alert(
                    "cred_invalid",
                    f"Domain credentials invalid for {domain}",
                    team_num=team_num,
                    details={"cred_id": cred_id, "domain": domain, "scope": "domain"},
                ))

    async def _scoring_cred_probe_loop(self) -> None:
        """Background loop: probe the red_scoring infra creds every N minutes.

        Runs an immediate first probe, then on the configured interval. Cheap
        NXC auth check (no command) per infra cred; maintains its `valid` flag,
        which feeds the /admin/status scoring-account health panel.
        """
        first = True
        while True:
            try:
                if not first:
                    await asyncio.sleep(config.SCORING_CRED_PROBE_INTERVAL_MINUTES * 60)
                first = False
                if config.RED_SCORING_ENABLED:
                    await self._probe_scoring_creds()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Scoring-cred probe loop error: {e}")

    async def _probe_scoring_creds(self) -> None:
        """Auth-probe every is_infrastructure cred and update its `valid` flag.

        Emits a `scoring_cred_down` alert on a previously-valid → invalid
        transition so operators learn that a box's scoring identity broke
        (which silently stops that box's IOCs from being checked).
        """
        if not self.executor or not self.executor.nxc_wrapper:
            return
        wrapper = self.executor.nxc_wrapper
        semaphore = asyncio.Semaphore(config.CRED_REVALIDATION_MAX_CONCURRENT)
        with Session(engine) as session:
            rows = session.execute(
                select(Credentials, Boxes.team_num)
                .join(Boxes, Credentials.box_ip == Boxes.box_ip)
                .where(Credentials.is_infrastructure == True)  # noqa: E712
            ).all()
            snapshot = [
                (c.cred_id, c.box_ip, c.protocol, c.username, c.password, c.valid, team_num)
                for c, team_num in rows
            ]
        if not snapshot:
            return

        async def probe_one(
            cred_id: int, box_ip: str, protocol: str, username: str,
            password: str, prev_valid: bool | None, team_num: int | None,
        ) -> tuple[int, bool]:
            # Retry before concluding invalid: one transient DC round-trip / SMB
            # blip under load must not down-score a box for a whole cycle. Only
            # a run of MAX_ATTEMPTS consecutive failures flips valid -> invalid.
            # (A member's local red_scoring is seeded as `.\red_scoring`, so the
            # plain check_creds call does local-SAM auth via the prefix.)
            attempts = max(1, config.SCORING_CRED_PROBE_MAX_ATTEMPTS)
            async with semaphore:
                success = False
                for attempt in range(attempts):
                    result = await wrapper.check_creds(box_ip, protocol, username, password)
                    if result.success:
                        success = True
                        break
                    if attempt < attempts - 1:
                        await asyncio.sleep(config.SCORING_CRED_PROBE_RETRY_DELAY_SEC)
                if prev_valid is True and not success:
                    self.logger.warning(
                        "red_scoring cred for %s is no longer valid after %d "
                        "attempts — that box's IOC checks will fail until it is "
                        "restored", box_ip, attempts,
                    )
                    fire_and_forget(emit_alert(
                        "scoring_cred_down",
                        f"Scoring account broken on {box_ip}",
                        box_ip=box_ip,
                        team_num=team_num,
                        details={"username": username, "protocol": protocol},
                    ))
                return cred_id, success

        results = await asyncio.gather(*[probe_one(*s) for s in snapshot])
        # Single bulk UPDATE keyed on PK instead of one SELECT+write per cred.
        from fastapi_backend.database.write_actor import write_actor  # cycle-break
        rows = [{"cred_id": cid, "valid": v} for cid, v in results]
        if rows:
            def _bulk(db: Session, __rows=rows) -> None:
                db.execute(update(Credentials), __rows)
            await write_actor.submit(_bulk, label="scoring_cred_probe_bulk")
        ok = sum(1 for _, v in results if v)
        self.logger.info("Scoring-cred probe complete: %d/%d boxes OK", ok, len(results))

    def _scoring_cred_health(self) -> dict[str, Any]:
        """Summarize red_scoring cred liveness for /admin/status (no secrets)."""
        with Session(engine) as session:
            rows = session.execute(
                select(Credentials.box_ip, Credentials.valid).where(
                    Credentials.is_infrastructure == True  # noqa: E712
                )
            ).all()
        total = len(rows)
        ok = sum(1 for _, valid in rows if valid is True)
        failing = sorted({box_ip for box_ip, valid in rows if valid is not True and box_ip})
        return {
            "enabled": config.RED_SCORING_ENABLED,
            "ok": ok,
            "total": total,
            "failing": failing,
        }

    async def shutdown(self) -> None:
        """Shutdown orchestrator and cleanup."""
        self.logger.info("Shutting down orchestrator...")

        try:
            if self.state.is_active():
                await self.stop_competition()
            elif self.executor:
                self.executor.stop_workers()

            if self.cred_revalidation_task:
                self.cred_revalidation_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.cred_revalidation_task

            if self.scoring_cred_probe_task:
                self.scoring_cred_probe_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.scoring_cred_probe_task

            # Stop db_writer subprocess that was started at executor init
            if self.executor:
                self.executor.stop_db_writer()

            self.logger.info("Orchestrator shutdown complete")

        except Exception as e:
            self.logger.error(f"Error during shutdown: {e}")

    def get_status(self) -> dict[str, Any]:
        """Get comprehensive status information."""
        import os as _os
        import time as _t
        _prof_on = _os.environ.get("C3R_STATUS_PROFILE", "0") == "1"
        _prof: dict[str, float] = {}
        _t0 = _t.perf_counter() if _prof_on else 0.0
        status = self.state.get_status_info()
        if _prof_on:
            _prof["state"] = _t.perf_counter() - _t0
            _t0 = _t.perf_counter()
        status["preflight"] = self.state.preflight
        status["preflight_row_count"] = self._preflight_row_count_safe()
        if _prof_on:
            _prof["preflight"] = _t.perf_counter() - _t0
            _t0 = _t.perf_counter()

        playbook_block = self._playbook_status_block()
        if _prof_on:
            _prof["playbook"] = _t.perf_counter() - _t0
            _t0 = _t.perf_counter()
        schedule_block = self._schedule_block()
        if _prof_on:
            _prof["schedule"] = _t.perf_counter() - _t0
            _t0 = _t.perf_counter()
        metrics_block = self._metrics_block()
        if _prof_on:
            _prof["metrics"] = _t.perf_counter() - _t0
            _t0 = _t.perf_counter()

        status["components"] = {
            "database": "ready",
            "inventory": self.inventory_manager.get_inventory_summary(),
            "iocs": {
                "loaded": len(self.ioc_loader.ioc_definitions),
                "by_os": {
                    os: len(iocs) for os, iocs in self.ioc_loader.os_ioc_mapping.items()
                },
            },
            "scheduler": self.scheduler.get_status() if self.scheduler else {},
            "executor": {
                "workers": self.executor.num_workers if self.executor else 0,
                "queue_size": self.executor.task_queue.qsize() if self.executor else 0,
                "stats": self.executor.get_stats() if self.executor else {},
            },
            "check_cycles": self.check_manager.get_cycle_stats(),
            "sa_collection": dict(self.sa_stats),
            "script_encryption": {
                "enabled": config.USE_SCRIPT_ENCRYPTION,
                "active": self.script_manager is not None,
            },
            "script_server": self._script_server_block(),
            "playbook": playbook_block,
            "competition_restart": dict(self._restart_resume_info),
            "scoring_creds": self._scoring_cred_health(),
            "configuration": config.summary(),
        }

        status["playbook"] = {
            "start_time": self._playbook_config.start_time.isoformat()
            if self._playbook_config.start_time else None,
            "end_time": _td_to_hms(self._playbook_config.end_time),
            "pause_at": _td_to_hms(self._playbook_config.pause_at),
            "resume_at": _td_to_hms(self._playbook_config.resume_at),
        }
        status["schedule"] = schedule_block
        status["metrics"] = metrics_block
        if _prof_on:
            _prof["components"] = _t.perf_counter() - _t0
            _tot = sum(_prof.values())
            if _tot > 1.0:
                self.logger.warning(
                    "get_status profile: total=%.2fs %s",
                    _tot, {k: round(v, 3) for k, v in _prof.items()},
                )

        # Soak Phase 0 + Phase 7 fields. Added 2026-06-04 per
        # PLAN_soak_bench_alignment.md §4.2.2 + §4.3.
        # `host` — hostname of the backend process; soak's
        # `_soak_assert_same_host()` gate compares this against the soak
        # process's `socket.gethostname()` to enforce single-host runs.
        # `cred_revalidation_last_cycle_duration_ms` — wall-clock duration of
        # the most recent _revalidate_all_creds() call; null until first
        # cycle completes. Consumed by Phase 7 anomaly detection.
        # `dropped_events_total` — global SSE publish-drop counter from
        # sliver_manager; consumed by Phase 0 backpressure metrics.
        import socket  # local-import: status path only
        status["host"] = socket.gethostname()
        status["cred_revalidation_last_cycle_duration_ms"] = (
            self._last_revalidate_duration_ms
        )
        try:
            from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
            status["dropped_events_total"] = sliver_manager.dropped_events_total
        except Exception:
            status["dropped_events_total"] = 0

        return status

    def _script_server_block(self) -> dict[str, Any]:
        """Shared script-delivery metrics + dedicated-listener health.

        Sourced from core/script_listener (fix #2,
        docs/PLAN_script_fetch_amplification.md).  Consumed by the soak's
        ``max_script_fetch_failure_rate`` threshold.
        """
        try:
            from fastapi_backend.core.script_listener import (  # cycle-break
                get_listener_info,
                get_script_server_metrics,
            )
            block = get_script_server_metrics()
            block["listener"] = get_listener_info()
            return block
        except Exception as e:  # never let status assembly fail on this
            self.logger.debug("script_server status block error: %s", e)
            return {}

    def _preflight_row_count_safe(self) -> int:
        try:
            return self.preflight_row_count()
        except Exception:
            return 0

    def _playbook_status_block(self) -> dict[str, Any]:
        """Per-entry playbook status + counts."""
        now = dt.datetime.now(dt.UTC)
        anchor = self._playbook_config.start_time or self.state.start_time

        entries_out: list[dict[str, Any]] = []
        for entry in self._playbook_entries:
            if anchor is not None:
                fire_at = anchor + dt.timedelta(seconds=entry.at_offset_seconds)
                fire_iso: str | None = fire_at.isoformat()
                in_past = fire_at <= now
            else:
                fire_at = None
                fire_iso = None
                in_past = False
            fired_at = entry.fired_at
            if fired_at is not None:
                status = "FIRED"
            elif in_past:
                status = "SKIPPED"
            else:
                status = "PENDING"
            entries_out.append({
                "at": f"T+{entry.at_offset_seconds // 3600:02d}:{(entry.at_offset_seconds % 3600) // 60:02d}:{entry.at_offset_seconds % 60:02d}",
                "fires_at": fire_iso,
                "action": entry.action,
                "os": entry.os,
                "command": entry.command,
                "ioc_name": entry.ioc_name,
                "fired_at": fired_at.isoformat() if fired_at else None,
                "status": status,
            })
        return {
            "job_count": len(_playbook_tasks),
            "entries_loaded": len(self._playbook_entries),
            "entries": entries_out,
        }

    def _schedule_block(self) -> dict[str, Any]:
        """Return {next_event, next_event_at} — earliest future ARM-derived datetime."""
        now = dt.datetime.now(dt.UTC)
        candidates: list[tuple[str, dt.datetime]] = []
        try:
            with Session(engine) as db:
                row = db.get(CompetitionConfig, 1)
        except Exception:
            return {"next_event": None, "next_event_at": None}
        if row:
            for label, raw in (
                ("start", row.armed_start_time),
                ("end", row.scheduled_end_time),
                ("pause", row.scheduled_pause_at),
                ("resume", row.scheduled_resume_at),
            ):
                if not raw:
                    continue
                try:
                    parsed = dt.datetime.fromisoformat(raw)
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.UTC)
                if parsed > now:
                    candidates.append((label, parsed))
        if not candidates:
            return {"next_event": None, "next_event_at": None}
        candidates.sort(key=lambda x: x[1])
        return {"next_event": candidates[0][0], "next_event_at": candidates[0][1].isoformat()}

    def _metrics_block(self) -> dict[str, Any]:
        """Return Part 2 metrics payload."""
        from fastapi_backend.core.metrics import (  # cycle-break
            collect_metrics,
        )
        try:
            return collect_metrics(self)
        except Exception as exc:
            self.logger.debug("metrics collection failed: %s", exc)
            return {}
