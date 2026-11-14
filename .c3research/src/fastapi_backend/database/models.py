"""SQLAlchemy ORM models + Pydantic schemas for the scoring DB."""

import datetime as dt
import re

from pydantic import BaseModel, ConfigDict, model_validator

# from sqlmodel import mapped_column, Relationship, SQLModel
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)

score_meta = MetaData()


class ScoreBase(DeclarativeBase):
    """Declarative base shared by every scoring-engine ORM model."""

    metadata = score_meta


class Users(ScoreBase):
    """User account row — admins, blue/red/white team operators."""

    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str]
    password: Mapped[str]
    is_blue_team: Mapped[bool] = mapped_column(default=True)
    blue_team_num: Mapped[int] = mapped_column(
        ForeignKey("blue_teams.team_num"), default=0
    )
    is_admin: Mapped[bool] = mapped_column(default=False)


class User(BaseModel):
    """Pydantic mirror of `Users` for response payloads."""

    user_id: int
    username: str
    password: str
    is_blue_team: bool
    blue_team_num: int
    is_admin: bool

    model_config = ConfigDict(from_attributes=True)


class BlueTeams(ScoreBase):
    """Blue-team scoreboard row — running total + last check pointer."""

    __tablename__ = "blue_teams"

    team_num: Mapped[int] = mapped_column(primary_key=True)
    last_check_id: Mapped[int | None] = mapped_column(
        ForeignKey("check_instance.check_id"),
        default=None,
    )
    total_score: Mapped[int] = mapped_column(default=0)


class CheckInstance(ScoreBase):
    """One scoring-engine check cycle for one blue team."""

    __tablename__ = "check_instance"

    # Named explicitly (not index=True) so the migration's CREATE INDEX
    # IF NOT EXISTS uses the SAME name → no duplicate index on fresh DBs.
    __table_args__ = (Index("ix_check_instance_team", "blue_team_num"),)

    check_id: Mapped[int] = mapped_column(primary_key=True)
    blue_team_num: Mapped[int] = mapped_column(ForeignKey("blue_teams.team_num"))
    timestamp: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(dt.UTC)
    )
    score: Mapped[int] = mapped_column(default=0)
    preflight: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    results: Mapped[list["IOCCheckResult"]] = relationship(back_populates="check")


class IOCCheckResult(ScoreBase):
    """One IOC's effect + persistence outcome for a single check cycle."""

    __tablename__ = "ioc_check_result"
    # Composite covers check_instance_id-only filters AND the
    # (check_instance_id, deployed_ioc_id) view joins (leading column rule);
    # the single deployed_ioc_id index serves deployed_ioc_id-only filters.
    # Names match db_init._migrate_score_indexes → no duplicate index on fresh DBs.
    __table_args__ = (
        Index(
            "ix_ioc_check_result_ci_deployed",
            "check_instance_id",
            "deployed_ioc_id",
        ),
        Index("ix_ioc_check_result_deployed_ioc", "deployed_ioc_id"),
    )

    ioc_id: Mapped[int] = mapped_column(primary_key=True)
    check_instance_id: Mapped[int] = mapped_column(
        ForeignKey("check_instance.check_id")
    )
    box_ip: Mapped[str]
    ioc_name: Mapped[str]
    difficulty: Mapped[int]  # 1=Easy, 2=Medium, 3=Hard
    status: Mapped[int | None] = mapped_column(nullable=True)  # legacy; use effect_status
    effect_status: Mapped[int | None] = mapped_column(nullable=True)      # -1=error, 0=remediated, 1=present
    persistence_status: Mapped[int | None] = mapped_column(nullable=True) # -1=error, 0=remediated, 1=present; NULL=no persistence
    deployed_ioc_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployed_iocs.id"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(default=None)
    preflight: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    check: Mapped[CheckInstance] = relationship(back_populates="results")


class CheckFailureDiagnostic(ScoreBase):
    """Diagnostic capture for one status=-1 check section.

    Gated by ``CHECK_DIAG_CAPTURE`` (default on). Kept SEPARATE from
    ``ioc_check_result`` so the scoring table is never touched. Lets the soak
    distinguish empty-body (delivery) vs truncated (late sections cut) vs
    garbled combined-check output. See
    docs/PLAN_soak_failure_instrumentation.md.
    """

    __tablename__ = "check_failure_diagnostics"

    id: Mapped[int] = mapped_column(primary_key=True)
    check_instance_id: Mapped[int | None] = mapped_column(nullable=True)
    box_ip: Mapped[str]
    section_name: Mapped[str]            # ioc_name or "<ioc>__persistence"
    failure_kind: Mapped[str]           # section_not_found | no_valid_json | other
    section_index: Mapped[int | None] = mapped_column(nullable=True)      # IOC ordinal in box check
    total_sections: Mapped[int | None] = mapped_column(nullable=True)     # len(ioc_tasks)
    sections_in_output: Mapped[int | None] = mapped_column(nullable=True) # markers parsed from output
    output_len: Mapped[int | None] = mapped_column(nullable=True)         # len(combined stdout)
    raw_excerpt: Mapped[str | None] = mapped_column(Text, default=None)   # head+tail of combined stdout
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )


class Boxes(ScoreBase):
    """Inventory row for one box — IP, OS, team attribution, live implant ids."""

    __tablename__ = "boxes"

    box_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    box_ip: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    box_name: Mapped[str | None] = mapped_column(default=None)
    os: Mapped[str | None] = mapped_column(default=None)
    team_num: Mapped[int | None] = mapped_column(
        ForeignKey("blue_teams.team_num"), nullable=True, default=None
    )
    session_id: Mapped[str | None] = mapped_column(default=None)       # active Sliver session ID
    beacon_id: Mapped[str | None] = mapped_column(default=None)        # active Sliver beacon ID
    last_seen: Mapped[dt.datetime | None] = mapped_column(default=None)
    last_implant_id: Mapped[str | None] = mapped_column(default=None)
    beacon_interval_ns: Mapped[int | None] = mapped_column(default=None)  # beacon interval in nanoseconds
    # Domain-join state — sourced from inventory.yml (declarative, not detected).
    # cred_harvester uses these to decide eligibility:
    #   is_domain_joined → box participates in golden-ticket auth
    #   is_domain_controller → DCSync krbtgt against this box
    #   domain_sid → captured during DCSync, used to forge tickets for the team
    is_domain_joined: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_domain_controller: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    domain: Mapped[str | None] = mapped_column(default=None)
    domain_sid: Mapped[str | None] = mapped_column(default=None)
    # Outbound/call-home address (docs/PLAN_firewall_wan_lan.md): the IP this box
    # appears as when it connects OUT (Sliver RemoteAddress, script-server fetch
    # source) — the WAN IP for a firewall. box_ip stays the LAN/identity address.
    # NOT unique (may be null on most rows). Used only for outbound correlation +
    # wrapper/result token binding; never for NXC connect or as a DB key.
    wan_ip: Mapped[str | None] = mapped_column(String, index=True, default=None)


class BoxResponse(BaseModel):
    """Pydantic mirror of `Boxes` for response payloads."""

    box_id: int
    box_ip: str
    box_name: str | None
    os: str | None
    team_num: int | None
    session_id: str | None
    beacon_id: str | None
    last_seen: dt.datetime | None
    beacon_interval_ns: int | None = None
    is_domain_joined: bool = False
    is_domain_controller: bool = False
    domain: str | None = None
    domain_sid: str | None = None

    model_config = ConfigDict(from_attributes=True)


class Credentials(ScoreBase):
    """Stored credential row — password, NT hash, or Kerberos ccache.

    cred_type discriminator:
      - 'password': legacy row; `password` holds plaintext
      - 'nthash':   `nthash` holds 32-hex NTLM hash; `password` empty string
      - 'ccache':   `ccache_blob` holds base64 Kerberos ccache; team-scoped
                    (box_ip NULL, team_num set); refreshed before
                    `ticket_expires_at` by cred_harvester.TicketRefreshLoop
    """

    __tablename__ = "credentials"
    # Name matches db_init._migrate_score_indexes → no duplicate on fresh DBs.
    __table_args__ = (Index("ix_credentials_box_ip", "box_ip"),)

    cred_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    password: Mapped[str] = mapped_column(String, nullable=False, default="")
    box_ip: Mapped[str | None] = mapped_column(
        ForeignKey("boxes.box_ip"), nullable=True, default=None
    )
    protocol: Mapped[str] = mapped_column(String, nullable=False)  # smb, ssh, winrm
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # Infrastructure (red_scoring) cred: the ONLY cred the IOC pipeline auths with.
    # Hidden from /c2/creds and excluded from every operator cred-resolution path;
    # excluded from _revalidate_all_creds (a dedicated liveness probe covers them).
    is_infrastructure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    source_deployed_ioc_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployed_iocs.id"), nullable=True, default=None
    )

    # Credential harvesting (Phase A — cred_harvester loops populate these).
    cred_type: Mapped[str] = mapped_column(String, nullable=False, default="password")
    nthash: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # AES256 Kerberos key (hex string) — populated alongside nthash for
    # krbtgt rows when secretsdump returns it. Modern DCs (Server 2025+)
    # reject RC4 (`KDC_ERR_ETYPE_NOSUPP`); forge falls back to NT hash
    # only when this column is empty.
    aes256_key: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    ccache_blob: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    domain: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    ticket_expires_at: Mapped[dt.datetime | None] = mapped_column(
        nullable=True, default=None
    )
    harvested_at: Mapped[dt.datetime | None] = mapped_column(nullable=True, default=None)
    # Source provenance: 'inventory' | 'operator' | 'red_ops' | 'sam_dump'
    # | 'lsa_dump' | 'dcsync' | 'forged' | 'deployed_ioc' (None = legacy row).
    # 'red_ops' = fleet-wide operator cred from inventory's `red_ops_accounts:`
    # (is_infrastructure=False; docs/PLAN_red_ops_accounts.md).
    source: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Team scope for ccache rows (box_ip IS NULL when team_num IS NOT NULL).
    # FK omitted to keep migration simple; cred_harvester maintains integrity.
    team_num: Mapped[int | None] = mapped_column(nullable=True, default=None)


_NTHASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class CredentialCreate(BaseModel):
    """POST /c2/creds request body."""

    username: str
    password: str = ""
    box_ip: str | None = None
    protocol: str  # "smb" | "ssh" | "winrm"
    is_default: bool = False
    cred_type: str = "password"
    nthash: str | None = None
    domain: str | None = None
    team_num: int | None = None

    @model_validator(mode="after")
    def _validate_nthash(self) -> "CredentialCreate":
        """Enforce 32-hex on `nthash` when `cred_type=='nthash'`.

        Defense in depth: client-side regex blocks UI submission, but a
        curl-bypassing-UI caller can still POST garbage. Reject + normalise
        to lowercase here so downstream callers see canonical form.
        """
        if self.cred_type == "nthash":
            if self.nthash is None or not _NTHASH_RE.match(self.nthash):
                raise ValueError("nthash must be 32 hex characters")
            object.__setattr__(self, "nthash", self.nthash.lower())
        return self


class CredentialResponse(BaseModel):
    """Pydantic mirror of `Credentials` for response payloads (excludes secrets)."""

    cred_id: int
    username: str
    box_ip: str | None
    protocol: str
    is_default: bool
    valid: bool | None
    source_deployed_ioc_id: int | None = None
    cred_type: str = "password"
    domain: str | None = None
    team_num: int | None = None
    ticket_expires_at: dt.datetime | None = None
    harvested_at: dt.datetime | None = None
    source: str | None = None
    # password / nthash / ccache_blob intentionally excluded.

    model_config = ConfigDict(from_attributes=True)


class SAResult(ScoreBase):
    """One SA-collection sample for a (box_ip, category) pair."""

    __tablename__ = "sa_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    box_ip: Mapped[str] = mapped_column(ForeignKey("boxes.box_ip"), index=True)
    category: Mapped[str]  # "users" | "processes" | "ports" | "history" | "auth_keys"
    collected_at: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(dt.UTC)
    )
    data: Mapped[str] = mapped_column(Text)  # JSON-encoded structured result


class ImplantBuilds(ScoreBase):
    """Persistent record of every generated implant binary.

    Written by POST /c2/implants/generate on success.
    Updated by POST /c2/implants/{id}/deploy when a build is redeployed.
    Used by GET /c2/implants/builds to power the sortable library table.
    """

    __tablename__ = "implant_builds"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    os: Mapped[str] = mapped_column(String, nullable=False)
    implant_type: Mapped[str] = mapped_column(String, nullable=False)   # "beacon" | "session"
    arch: Mapped[str] = mapped_column(String, nullable=False)
    format: Mapped[str] = mapped_column(String, nullable=False)
    c2_url: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    # The bare Sliver build codename (e.g. "CORRESPONDING_BASEBALL") this build was
    # compiled under — the ONLY identifier that matches Sliver's own
    # ``implant_builds()`` list. ``filename`` is a composite/masquerade name
    # (``CODENAME_session_linux_amd64.elf`` or ``sshd-keygen.elf``) whose stem never
    # equals the codename, so staleness checks must key on this, not the filename.
    # NULL for disk-synced rows whose codename is unrecoverable → callers fail-open.
    sliver_name: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
    )
    last_deployed_to: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    last_deployed_path: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    pivot_gateway: Mapped[bool] = mapped_column(Boolean, default=False)
    pivot_set_id: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    tcp_pivot_port: Mapped[int | None] = mapped_column(nullable=True, default=None)
    persistence_mechanism: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # "none" | "binary_only" | "binary_and_persistence" — collapses the
    # ImplantGenerateRequest.apply_immutable + immutable_scope fields into a
    # single value.  False ⇒ "none"; True ⇒ the chosen scope.  Disk-synced
    # rows (registered post-hoc by implant_migration) default to "none" since
    # the original generate-request context is gone by then.
    immutable_scope: Mapped[str] = mapped_column(String, nullable=False, default="none")


class PivotSessions(ScoreBase):
    """Tracks active pivot sessions: pfSense mTLS session + its portfwd state."""

    __tablename__ = "pivot_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    team_num: Mapped[int] = mapped_column(ForeignKey("blue_teams.team_num"))
    pfsense_session_id: Mapped[str] = mapped_column(String, unique=True)
    pfsense_box_ip: Mapped[str] = mapped_column(String)
    pfsense_pivot_ip: Mapped[str] = mapped_column(String)   # host NXC connects to (127.0.0.1 via portfwd)
    tcp_pivot_port: Mapped[int] = mapped_column(default=9898)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PortfwdMappings(ScoreBase):
    """Per-box portfwd allocations for an active pivot session."""

    __tablename__ = "portfwd_mappings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pivot_session_id: Mapped[int] = mapped_column(ForeignKey("pivot_sessions.id"))
    box_ip: Mapped[str] = mapped_column(String)
    smb_local_port: Mapped[int] = mapped_column()
    ssh_local_port: Mapped[int] = mapped_column()
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )


class DevSession(ScoreBase):
    """Mock Sliver session for DEV_MODE — populated by seed_dummy_data.py."""

    __tablename__ = "dev_sessions"

    id:             Mapped[str]        = mapped_column(String, primary_key=True)
    name:           Mapped[str]        = mapped_column(String, nullable=False)
    hostname:       Mapped[str]        = mapped_column(String, nullable=False)
    os:             Mapped[str]        = mapped_column(String, nullable=False)
    arch:           Mapped[str]        = mapped_column(String, nullable=False)
    pid:            Mapped[int]
    remote_address: Mapped[str]        = mapped_column(String, nullable=False)
    transport:      Mapped[str]        = mapped_column(String, nullable=False)
    is_dead:        Mapped[bool]       = mapped_column(Boolean, default=False)
    last_checkin:   Mapped[str]        = mapped_column(String, nullable=False)  # ISO timestamp
    box_name:       Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    team_num:       Mapped[int | None] = mapped_column(nullable=True, default=None)
    box_ip:         Mapped[str | None] = mapped_column(String, nullable=True, default=None)


class DevBeacon(ScoreBase):
    """Mock Sliver beacon for DEV_MODE — populated by seed_dummy_data.py."""

    __tablename__ = "dev_beacons"

    id:             Mapped[str]        = mapped_column(String, primary_key=True)
    name:           Mapped[str]        = mapped_column(String, nullable=False)
    hostname:       Mapped[str]        = mapped_column(String, nullable=False)
    os:             Mapped[str]        = mapped_column(String, nullable=False)
    arch:           Mapped[str]        = mapped_column(String, nullable=False)
    pid:            Mapped[int]
    remote_address: Mapped[str]        = mapped_column(String, nullable=False)
    transport:      Mapped[str]        = mapped_column(String, nullable=False)
    interval:       Mapped[int]        # nanoseconds
    jitter:         Mapped[float]
    next_checkin:   Mapped[int]        # unix seconds
    last_checkin:   Mapped[int]        # unix seconds
    is_dead:        Mapped[bool]       = mapped_column(Boolean, default=False)
    box_name:       Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    team_num:       Mapped[int | None] = mapped_column(nullable=True, default=None)
    box_ip:         Mapped[str | None] = mapped_column(String, nullable=True, default=None)


class DeployedIOCs(ScoreBase):
    """Tracks IOCs that have been registered/deployed by the red team on specific boxes."""

    __tablename__ = "deployed_iocs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    box_ip: Mapped[str] = mapped_column(String, nullable=False)
    ioc_name: Mapped[str] = mapped_column(String, nullable=False)
    persistence_mechanism: Mapped[str | None] = mapped_column(String, nullable=True)
    implant_identifier: Mapped[str | None] = mapped_column(String, nullable=True)  # session.Name
    blue_team_label: Mapped[str] = mapped_column(String, nullable=False)  # OS_DIFFICULTY_NUM
    deployed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
    )
    deployed_by: Mapped[str] = mapped_column(String, nullable=False)
    # active | failed | lost | deploy_failed
    #   active        — planted and scoring.
    #   failed        — RED remediated it (see CLAUDE.md); it WAS deployed.
    #   lost          — implant/beacon went away.
    #   deploy_failed — it never fully landed. DISTINCT from `failed` on purpose:
    #                   the operator redeploy UI has to tell "red tore it down"
    #                   apart from "it never deployed", and only the latter is
    #                   redeployable. Like every non-`active` status it leaves the
    #                   scoreboard denominator, so it cannot be scored — which is
    #                   the point: a persistence half that never deployed reads to
    #                   the check pipeline exactly like one blue remediated, and
    #                   would otherwise pay blue partial points (measured: 16 of 40
    #                   on an Ultra IOC) for an artifact red never planted.
    #                   See docs/PLAN_failed_deploy_redeploy.md.
    status: Mapped[str] = mapped_column(String, default="active")
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")  # manual | auto
    difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1=Easy, 2=Medium, 3=Hard
    # JSON dict of pool-chosen artifact attributes recorded at deploy time so the
    # check/remediate pipelines can inject the SAME values back (deploy/check/
    # remediate agree on where the artifact lives). Part 1: {"DROP_DIR": "<dir>"};
    # extensible for Part 2 name pools (SERVICE_NAME/CRON_NAME/BINARY_NAME/...).
    # See docs/PLAN_recorded_deploy_dirs.md. Each distinct DROP_DIR = one row =
    # one scoring unit; a partial unique index enforces distinct dirs per active
    # (box, ioc).
    deploy_params: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Failure bookkeeping (docs/PLAN_failed_deploy_redeploy.md). Operators need to
    # see WHY a deploy failed without leaving the deploy page, and the retry
    # wrapper needs somewhere durable to count attempts.
    deploy_error: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # The OperatorTask that attempted this deploy — deep-links the UI to the
    # existing per-box progress view instead of duplicating it.
    deploy_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    deploy_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @property
    def params_dict(self) -> dict:
        """Parse ``deploy_params`` JSON → dict ({} when NULL/blank/invalid)."""
        import json  # cycle-break
        if not self.deploy_params:
            return {}
        try:
            v = json.loads(self.deploy_params)
            return v if isinstance(v, dict) else {}
        except (ValueError, TypeError):
            return {}

    def set_params(self, params: dict | None) -> None:
        """Serialize a params dict → ``deploy_params`` JSON (None/{} → NULL)."""
        import json  # cycle-break
        self.deploy_params = json.dumps(params) if params else None


class PendingKills(ScoreBase):
    """Tracks intentional kills issued via C3rberus UI before gRPC disconnect fires.

    Rows inserted before kill_session/kill_beacon gRPC call; deleted when the
    corresponding disconnect event is processed in sliver_manager. Allows the
    event handler to distinguish intentional kills (→ DeployedIOCs status='lost')
    from organic disconnects (network blip, blue team action, crash).
    """

    __tablename__ = "pending_kills"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)   # Sliver session or beacon ID
    initiated_by: Mapped[str] = mapped_column(String, nullable=False)   # operator username
    initiated_at: Mapped[str] = mapped_column(String, nullable=False)   # ISO datetime string


class HidingLibraryConfig(ScoreBase):
    """Per-box entries the hiding library should conceal.

    Populated when an IOC with wants_hiding is deployed; auto-removed when the
    source IOC is remediated.  C3rberus encrypts all rows for a box into one
    XOR-encrypted blob and pushes it to the box (xattr on Linux, extattr on
    FreeBSD, registry value on Windows).  The hiding library reads the blob and
    filters the listed names from process lists, directory listings, etc.
    """

    __tablename__ = "hiding_library_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    box_ip: Mapped[str] = mapped_column(String, nullable=False, index=True)
    os: Mapped[str] = mapped_column(String, nullable=False)           # linux | firewall | windows
    entry_type: Mapped[str] = mapped_column(String, nullable=False)   # process_name | file_path | service | user | cron_file
    entry_value: Mapped[str] = mapped_column(String, nullable=False)
    source_ioc_name: Mapped[str] = mapped_column(String, nullable=False)
    source_deployed_ioc_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("deployed_iocs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )


class Alerts(ScoreBase):
    """Alert events: lost sessions, credential failures, box unreachable, etc."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True
    )
    severity: Mapped[str] = mapped_column(String, nullable=False)  # info | warn | critical
    kind: Mapped[str] = mapped_column(String, nullable=False)     # session_lost | beacon_lost | beacon_stale | cred_invalid | box_unreachable | pivot_lost | redeploy_failed | clock_skew
    box_ip: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    team_num: Mapped[int | None] = mapped_column(nullable=True, index=True)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class AlertReads(ScoreBase):
    """Per-user read state for alerts."""

    __tablename__ = "alert_reads"

    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )


def assign_blue_team_label(db: Session, ioc_os: str, ioc_difficulty: int) -> str:
    """Generate an obscured OS_DIFFICULTY_N label for a newly deployed IOC."""
    os_map = {"windows": "WINDOWS", "linux": "LINUX", "firewall": "FIREWALL"}
    diff_map = {1: "EASY", 2: "MEDIUM", 3: "HARD", 4: "ULTRA"}
    prefix = f"{os_map.get(ioc_os, ioc_os.upper())}_{diff_map.get(ioc_difficulty, str(ioc_difficulty))}"
    count = db.query(func.count(DeployedIOCs.id)).filter(
        DeployedIOCs.blue_team_label.like(f"{prefix}_%")
    ).scalar() + 1
    return f"{prefix}_{count}"


class Modules(ScoreBase):
    """Script modules that can be run against target boxes."""

    __tablename__ = "modules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    target_os: Mapped[str] = mapped_column(String, nullable=False)  # "windows" | "linux" | "any"
    params: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)  # default params string shown to operator
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    file_hash: Mapped[str] = mapped_column(String, nullable=False)   # SHA-256
    last_modified: Mapped[float] = mapped_column(nullable=False)     # file mtime (epoch)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # runs relationship removed — module run history now tracked via OperatorTask


class ScheduledTask(ScoreBase):
    """A command scheduled to run against one or more implants at a future time."""

    __tablename__ = "scheduled_tasks"

    id:          Mapped[int]       = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:        Mapped[str]       = mapped_column(String, nullable=False)
    command:     Mapped[str]       = mapped_column(String, nullable=False)
    args:        Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    target_type: Mapped[str]       = mapped_column(String, nullable=False)  # "box_ip" | "team" | "all"
    target:      Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    run_at:      Mapped[str]       = mapped_column(String, nullable=False)   # ISO timestamp
    repeat_secs: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    status:      Mapped[str]       = mapped_column(String, default="pending")  # pending | running | complete | error | cancelled
    created_by:  Mapped[str]       = mapped_column(String, nullable=False)
    created_at:  Mapped[str]       = mapped_column(String, nullable=False,
                                         default=lambda: dt.datetime.now(dt.UTC).isoformat())
    last_run_at:      Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    next_run_at:      Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    operator_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # FK-free int — points to OperatorTask.id for the current session run.
    # Cleared at startup when OperatorTask table is truncated.


class ScheduledCompetitionEvent(ScoreBase):
    """Admin-scheduled competition lifecycle events (start / pause / end)."""

    __tablename__ = "scheduled_competition_events"

    id:         Mapped[int]  = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str]  = mapped_column(String, nullable=False)  # "start" | "pause" | "end" | "check"
    run_at:     Mapped[str]  = mapped_column(String, nullable=False)   # ISO timestamp
    fired:      Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str]  = mapped_column(String, nullable=False)


# Legacy ``ExecTask`` model removed in exec-merge plan Phase 5.  Single-implant
# exec history now writes ``OperatorTask(task_type='exec_single')`` rows;
# ``_migrate_drop_exec_tasks`` in db_init drops the old table.


class OperatorTask(ScoreBase):
    """Async operator task — deploy / generate / module run with per-box progress tracking."""

    __tablename__ = "operator_tasks"

    id:            Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_type:     Mapped[str]        = mapped_column(String, nullable=False)
    # "generate" | "generate_deploy" | "deploy" | "module"
    status:        Mapped[str]        = mapped_column(String, nullable=False, default="pending")
    # "pending" | "running" | "done" | "failed"
    current_phase: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # "compiling" | "deploying" | None — only set for generate/generate_deploy types
    operator:      Mapped[str]        = mapped_column(String, nullable=False)
    team_num:      Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    target_boxes:  Mapped[str]        = mapped_column(Text, nullable=False, default="[]")
    # JSON array: '["10.0.1.1","10.0.1.2"]'
    progress_json: Mapped[str]        = mapped_column(Text, nullable=False, default="{}")
    # JSON dict keyed by IP → {status, error, details, last_error, timestamp, attempts, exec_path, duration_s}
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # generate/generate_deploy: {"build_id", "implant_name"}
    # deploy: {"ioc_name"}
    # module: {"module_id", "params_used"}
    description:   Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # human-readable summary, computed server-side at create_task() time
    created_at:    Mapped[str]        = mapped_column(String, nullable=False,
                                           default=lambda: dt.datetime.now(dt.UTC).isoformat())
    updated_at:    Mapped[str]        = mapped_column(String, nullable=False,
                                           default=lambda: dt.datetime.now(dt.UTC).isoformat())
    completed_at:  Mapped[str | None] = mapped_column(String, nullable=True, default=None)


class StagedUploads(ScoreBase):
    """Phase 4 (exec-merge plan): files staged via POST /c2/bulk/put-file/stage.

    The async POST /c2/bulk/put-file/async then references rows by upload_id
    instead of carrying file bytes in the JSON body.  Sweep loop purges rows
    + on-disk files older than 24h via
    ``fastapi_backend.c2.bulk_put_file.staged_upload_sweep_loop``.
    """

    __tablename__ = "staged_uploads"

    upload_id:   Mapped[str]       = mapped_column(String, primary_key=True)
    filename:    Mapped[str]       = mapped_column(String, nullable=False)
    size_bytes:  Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    local_path:  Mapped[str]       = mapped_column(String, nullable=False)
    staged_at:   Mapped[str]       = mapped_column(String, nullable=False,
                                           default=lambda: dt.datetime.now(dt.UTC).isoformat())
    operator:    Mapped[str]       = mapped_column(String, nullable=False)


class CompetitionConfig(ScoreBase):
    """Singleton row (id=1) persisting competition-wide config across restarts."""

    __tablename__ = "competition_config"

    id:                    Mapped[int]       = mapped_column(Integer, primary_key=True, default=1)
    status:                Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Persisted on every CompetitionState.set_status() transition (the enum's
    # .value) so a mid-competition backend restart can resume RUNNING/PAUSED
    # instead of reverting to NOT_STARTED. See docs/PLAN_competition_status_restart.md.
    competition_start_at:  Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # ISO datetime string — set when competition transitions to RUNNING.
    # Playbook scheduler uses this to compute job run_dates after a backend restart.
    armed_start_time:      Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    scheduled_end_time:    Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    scheduled_pause_at:    Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    scheduled_resume_at:   Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Absolute ISO datetime strings — populated at arm time as
    # `playbook.start_time + offset`. Cleared on disarm.


# TODO: Either pass engine into models.py somehow or move this to db_init and
#        figure out global/import stuff.
# TeamLatestIocDetails = Table("team_latest_ioc_details", score_meta, autoload_with=engine)
"""
class BlueTeamsScoreboard(SQLModel):
    # This is a database view, not a table

    # Columns exposed by your view
    team_num: int = mapped_column(
        primary_key=True
    )  # pick a PK (team_num works fine here)
    last_check_id: int | None = None
    total_score: int
    last_check_score: int
    last_check_time: datetime | None = None


class TeamLatestIocDetails(SQLModel):
    # This is a database view, not a table

    team_num: int = mapped_column(primary_key=True)
    box_ip: str = mapped_column(primary_key=True)
    ioc_name: str = mapped_column(primary_key=True)

    difficulty: int
    status: int
    error: str | None
    points: int
"""
