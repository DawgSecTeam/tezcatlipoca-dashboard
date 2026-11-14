"""Loads ``inventory.yml`` into in-memory ``Box`` and ``Team`` objects."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Box:
    """Represents a single box/host in the competition."""

    ip: str
    name: str
    os: str  # 'windows', 'linux', or 'firewall'
    team_num: int
    username: str = "admin"
    password: str = "password"
    port: int | None = None
    connection: str | None = None  # 'winrm', 'psrp', or None for default
    domain_username: str | None = None  # Windows domain credential (TEAMX\Administrator)
    domain_password: str | None = None
    # Domain-join state — declared in inventory.yml (cred_harvester reads from
    # the synced Boxes table). Linux + firewall default to False until the
    # infra-update lands; today only Windows boxes are domain-joined.
    is_domain_joined: bool = False
    is_domain_controller: bool = False
    domain: str | None = None
    # Per-box red_scoring override (docs/PLAN_scoring_creds_deploy_manager.md).
    # Wins over RED_SCORING_USERNAME/PASSWORD in .env; unset -> orchestrator
    # falls back to the .env default, then to this box's own admin password.
    scoring_username: str | None = None
    scoring_password: str | None = None
    # Outbound/call-home address (docs/PLAN_firewall_wan_lan.md). The IP this box
    # appears as when it connects OUT — its Sliver session RemoteAddress and its
    # source IP when fetching from the script server. For a firewall this is the
    # WAN IP (C3rberus reaches it inbound via its LAN `ip`). Unset -> outbound
    # correlation + token binding use `ip`.
    wan_ip: str | None = None
    # Per-box override for the source IP the script server sees on this box's
    # HMAC-token fetches. Set when a member is NAT'd through its team firewall
    # on the way to Kali (so tokens must be signed with the firewall's WAN IP,
    # not the member's LAN box_ip, or every fetch 401's with signature-mismatch
    # — cad659a). LEAVE UNSET when the box appears at Kali as its own LAN IP
    # (dev inventory, and any range whose pfSense NAT excludes traffic to the
    # Kali subnet). Firewalls don't need this — their wan_ip is used for both
    # dial target and fetch source. Members without this field default to
    # signing tokens with their own box_ip.
    fetch_source_ip: str | None = None

    def __post_init__(self) -> None:
        """Apply OS-derived defaults for port and connection if unset."""
        if self.port is None:
            self.port = 5985 if self.os == "windows" else 22
        if self.connection is None and self.os == "windows":
            self.connection = "winrm"  # Default to winrm for Windows


@dataclass
class Team:
    """Represents a blue team."""

    team_num: int
    name: str
    boxes: list[Box] = field(default_factory=list)


@dataclass
class ScoringAccount:
    """The red_scoring infrastructure account declared in inventory.yml.

    Declarative only — the password is ALWAYS read from RED_SCORING_PASSWORD in
    .env, never stored in inventory. ``username``/``domain`` here override the
    config defaults; ``enabled`` gates per-box seeding (in addition to the
    RED_SCORING_ENABLED config kill-switch).
    """

    username: str
    domain: str = ""
    enabled: bool = True


@dataclass
class RedOpsAccount:
    r"""A fleet-wide red-team OPERATOR account declared in inventory.yml.

    Unlike ScoringAccount (the IOC pipeline's private is_infrastructure=True
    identity), red_ops accounts are seeded is_infrastructure=False: visible in
    /c2/creds and selectable by every operator + C3rberus red-team path, but
    never resolved by the IOC check/deploy/remediate pipeline. Each entry is
    seeded on every box as a local user and, when ``domain`` is True, as a
    domain user on domain-joined boxes too. The password is stored in inventory
    (plaintext, same as the per-box blueteam/blackteam creds). See
    docs/PLAN_red_ops_accounts.md.

    ``foreign_domain`` is a FQDN override for cross-forest identities (e.g. a
    rogue-DC user like ``mira.corp.sus\\svc_dfsr`` that authenticates to team
    DCs via an established forest trust). When set:
      * ``domain`` (the local/NETBIOS toggle) is ignored — a foreign principal
        by definition has no local SAM and no team-side NetBIOS name.
      * A single row per applicable Windows DC is seeded:
        ``{username: <bare>, password: <..>, domain: <FQDN>, protocol: smb}``
        so NXC dials ``-u svc_dfsr -d mira.corp.sus`` (matches the cross-realm
        auth path we already verified via nxc smb).
      * Rows are ONLY seeded on domain-joined Windows DCs. Members
        (``is_domain_controller=False``) technically accept cross-realm auth
        via netlogon pass-through but confer no privilege unless we've
        planted the foreign SID in their local Administrators — skip the
        noise. Linux / firewall boxes are outside the trust and are skipped.
    """

    username: str
    password: str
    domain: bool = True  # also seed a domain row on domain-joined boxes
    enabled: bool = True
    foreign_domain: str = ""  # FQDN override; see class docstring for semantics


class InventoryManager:
    """Manages team and box inventory for the competition.
    
    Parses YAML config files and tracks Box/Team objects in memory.
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        self.teams: dict[int, Team] = {}
        self.all_boxes: list[Box] = []
        self.scoring_account: ScoringAccount | None = None
        self.red_ops_accounts: list[RedOpsAccount] = []
        # C2 endpoint settings declared in the inventory's top-level
        # `c2_settings:` block (script_server_url / sliver_host / c2_ip). Lets a
        # single inventory file carry the network identity so switching
        # inventories no longer requires hand-editing those three .env keys.
        # Empty when the block is absent. Applied to `config` via
        # apply_c2_settings() (called by the orchestrator after each load).
        self.c2_settings: dict[str, str] = {}

    @staticmethod
    def _infer_default_domain(
        *, box_data: dict, team_domain: str | None,
        team_num: int, domain_joined: bool,
    ) -> str | None:
        r"""Best-effort realm inference from inventory shape.

        Precedence (highest first, applied when the box does not set `domain:`):
          1. `domain_username` UPN suffix (`user@corp.example` -> `corp.example`).
             NetBIOS form `DOMAIN\user` is skipped — NetBIOS name is not a
             KDC-usable realm and inferring `<netbios>.local` was the source
             of the KDC_ERR_WRONG_REALM cascade this fix chases.
          2. Team-level `domain:` from inventory.yml.
          3. Legacy fallback `team<N>.c3` (matches the 10-team lab CDE).
        Returns None when the box is not domain-joined.
        """
        if not domain_joined:
            return None
        du = (box_data.get("domain_username") or "").strip()
        if "@" in du:
            suffix = du.rsplit("@", 1)[1].strip()
            if suffix:
                return suffix.lower()
        if team_domain:
            return str(team_domain).lower()
        return f"team{team_num}.c3"

    def get_box_by_ip(self, ip: str) -> Box | None:
        """Return the Box with this IP, or None.

        Used by the domain-auth fallback (see PLAN_domain_auth_fallback.md) to
        find a box's local admin credential when the caller only has a box_ip.
        """
        return next((b for b in self.all_boxes if b.ip == ip), None)

    def load_from_config(self, config_path: Path | None = None) -> None:
        """Load inventory from configuration file."""
        import os  # cycle-break

        if config_path is None:
            # Allow override via env var (e.g. for scoped integration tests)
            env_path = os.environ.get("INVENTORY_FILE")
            if env_path:
                config_path = Path(env_path)
                self.logger.info(f"Using inventory from INVENTORY_FILE: {config_path}")
            else:
                config_path = Path("./config/inventory.yml")
                if not config_path.exists():
                    self.logger.warning("No inventory config found, using defaults")
                    self._create_default_inventory()
                    return
                self.logger.info(f"Loading inventory from {config_path}")

        if config_path.exists():
            self.logger.info(f"Loading inventory from {config_path}")
            with config_path.open("r") as f:
                config = yaml.safe_load(f)
                self._parse_config(config)
        else:
            self.logger.warning(
                f"Inventory config not found at {config_path}, using defaults"
            )
            self._create_default_inventory()

    def _create_default_inventory(self) -> None:
        self.teams = {}
        self.all_boxes = []
        self.scoring_account = None
        self.red_ops_accounts = []

    def _parse_config(self, config: dict) -> None:
        """Parse inventory configuration.

        Resets `teams`/`all_boxes`/`scoring_account` first — load_from_config()
        can be called more than once in a process's lifetime (orchestrator's
        reload_inventory(), docs/PLAN_live_worker_team_binding.md §6.1). Without
        this reset, `all_boxes` (a plain list, appended to below) would
        accumulate stale boxes from every prior load forever, and
        prune_stale_boxes() would never see them as gone since they'd still be
        in the "current" list handed to it.
        """
        self.teams = {}
        self.all_boxes = []
        self.scoring_account = None
        self.red_ops_accounts = []
        self.c2_settings = {}

        c2_data = config.get("c2_settings")
        if c2_data:
            # Only carry keys that are actually present + non-empty. A missing
            # key means "leave config/.env as-is" (see apply_c2_settings).
            for env_key, yaml_key in (
                ("SCRIPT_SERVER_URL", "script_server_url"),
                ("SLIVER_HOST", "sliver_host"),
                ("C2_IP", "c2_ip"),
            ):
                val = c2_data.get(yaml_key)
                if val:
                    self.c2_settings[env_key] = str(val)

        sa_data = config.get("scoring_account")
        if sa_data:
            self.scoring_account = ScoringAccount(
                username=sa_data.get("username", "red_scoring"),
                domain=sa_data.get("domain", "") or "",
                enabled=sa_data.get("enabled", True),
            )

        # Fleet-wide red-team OPERATOR accounts (list). Seeded is_infrastructure
        # =False by the orchestrator — distinct from red_scoring above. See
        # docs/PLAN_red_ops_accounts.md.
        for entry in config.get("red_ops_accounts", []) or []:
            username = entry.get("username")
            if not username:
                self.logger.warning(
                    "Skipping red_ops_accounts entry with no username: %r", entry
                )
                continue
            self.red_ops_accounts.append(
                RedOpsAccount(
                    username=str(username),
                    password=str(entry.get("password", "")),
                    domain=bool(entry.get("domain", True)),
                    enabled=bool(entry.get("enabled", True)),
                    foreign_domain=str(entry.get("foreign_domain", "") or ""),
                )
            )

        for team_data in config.get("teams", []):
            team = Team(
                team_num=team_data["team_num"],
                name=team_data.get("name", f"Team {team_data['team_num']}"),
            )
            # Team-level realm (new): inventories can set `domain: <fqdn>` at
            # the team block to declare the AD realm for all its boxes without
            # repeating per-box. Falls through the per-box precedence below.
            team_domain = team_data.get("domain")

            for box_data in team_data.get("boxes", []):
                os_lower = box_data["os"].lower()
                box_name = box_data["name"]
                # Defaults (overridable per-box in inventory.yml):
                #   is_domain_joined: True for Windows. Linux + firewall False
                #     until the infra-update lands (see TODO Infrastructure).
                #   is_domain_controller: True for Windows boxes whose name
                #     ends in '-1' (matches scripts/promote_domain_controllers
                #     which promotes c3-win25-<team>-1 as the DC for each team).
                #   domain: derived per-box in precedence order — per-box
                #     `domain:` > per-box `domain_username` UPN suffix
                #     (user@fqdn) > per-team `domain:` > legacy fallback
                #     `team<N>.c3` (kept so existing 10-team inventory keeps
                #     working). NetBIOS-form `DOMAIN\\user` is not FQDN so
                #     it's ignored for inference — set `domain:` explicitly
                #     or add a UPN-form sibling.
                default_domain_joined = os_lower == "windows"
                default_dc = (
                    default_domain_joined and box_name.endswith("-1")
                )
                # Resolve effective is_domain_joined FIRST so Linux boxes that
                # explicitly set `is_domain_joined: true` (SSSD-joined range)
                # get realm inference too — not just Windows.
                effective_domain_joined = box_data.get(
                    "is_domain_joined", default_domain_joined
                )
                default_domain = self._infer_default_domain(
                    box_data=box_data,
                    team_domain=team_domain,
                    team_num=team.team_num,
                    domain_joined=effective_domain_joined,
                )
                box = Box(
                    ip=box_data["ip"],
                    name=box_name,
                    os=os_lower,
                    team_num=team.team_num,
                    username=box_data.get("username", "admin"),
                    password=box_data.get("password", "password"),
                    port=box_data.get("port"),
                    connection=box_data.get("connection"),
                    domain_username=box_data.get("domain_username"),
                    domain_password=box_data.get("domain_password"),
                    is_domain_joined=box_data.get(
                        "is_domain_joined", default_domain_joined
                    ),
                    is_domain_controller=box_data.get(
                        "is_domain_controller", default_dc
                    ),
                    domain=box_data.get("domain", default_domain),
                    scoring_username=box_data.get("scoring_username"),
                    scoring_password=box_data.get("scoring_password"),
                    wan_ip=box_data.get("wan_ip"),
                    fetch_source_ip=box_data.get("fetch_source_ip"),
                )
                team.boxes.append(box)
                self.all_boxes.append(box)

            self.teams[team.team_num] = team

        self.logger.info(
            f"Loaded {len(self.teams)} teams with {len(self.all_boxes)} total boxes"
        )

    def apply_c2_settings(self) -> dict[str, str]:
        """Push the inventory's `c2_settings:` onto the live `config` singleton.

        Precedence: an explicit process-environment value ALWAYS wins over the
        inventory. This keeps two things intact:
          * the test harness (run_tests.sh) exports its own C2_IP for the
            dev-VM-reachable address — inventory must not clobber it;
          * a `.env` that still pins one of the three keys behaves exactly as
            before (no silent change for existing setups).
        Where the env var is unset/empty, the inventory value is applied so a
        single inventory file drives SCRIPT_SERVER_URL / SLIVER_HOST / C2_IP.
        Runs before lifespan's resolve_c2_ip()/refresh_script_server_url(),
        whose "only fill when unset" guards then see the inventory value and
        skip auto-derivation. Returns the keys actually applied (for logging).

        The goal: switching inventories (dev ↔ live_fire ↔ testing) no longer
        requires hand-editing those three .env keys — the inventory carries the
        network identity. Drop the three keys from `.env` to let inventory
        drive them; keep one pinned to force it regardless of inventory.
        """
        import os  # cycle-break

        from fastapi_backend.core.config import config  # cycle-break

        applied: dict[str, str] = {}
        for env_key, value in self.c2_settings.items():
            if os.environ.get(env_key):
                # Explicit env wins; leave config as read from that env value.
                continue
            setattr(config, env_key, value)
            applied[env_key] = value
        if applied:
            self.logger.info(
                "Applied c2_settings from inventory to config: %s",
                ", ".join(f"{k}={v}" for k, v in applied.items()),
            )
        return applied

    def get_inventory_summary(self) -> dict:
        """Return a summary of the current inventory."""
        return {
            "teams": len(self.teams),
            "boxes": len(self.all_boxes),
            "boxes_by_os": {
                os: len([b for b in self.all_boxes if b.os == os])
                for os in ("windows", "linux", "firewall")
            },
        }

