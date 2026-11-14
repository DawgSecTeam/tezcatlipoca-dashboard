"""Dataclasses for the IOC definition schema (``IOC``, ``PersistenceMechanism``, ``ScriptCheck``, ``SliverCheck``)."""

from dataclasses import dataclass, field

from fastapi_backend.core.config import config


@dataclass
class HidingEntry:
    """Declares what the hiding library should conceal when this IOC is active.

    Resolved at deploy time:
      value_literal  → returned as-is (static value)
      value_env_key  → looks up deploy_params[key]; falls back to value_default
                       when the key is absent (e.g. no pool pick was made)
      value_default  → fallback when env_key not in params (mirrors the
                       ${C3R_KEY:-default} pattern in protect_paths)

    type: "process_name" | "file_path" | "service" | "user" | "cron_file"
    """

    type: str
    value_env_key: str | None = None   # reads C3R_<env_key> from deploy_params
    value_literal: str | None = None   # static hardcoded value
    value_default: str | None = None   # fallback when env_key not in params


@dataclass
class PersistenceMechanism:
    """Describes the persistence half of an IOC (script + retrigger metadata)."""

    name: str
    tier: int
    retrigger_interval_seconds: int
    check_script: str               # path relative to iocs/
    immutable: bool = False         # chattr +i / chflags schg / attrib+icacls at deploy time
    deploy_script: str | None = None
    remediate_script: str | None = None
    os: str = ""                    # "windows" | "linux" | "firewall" — derived from check_script
    # Operator-readable UI label for the Generate page dropdown.  Hand-written
    # per instance (Title Case, ≤3-4 words, describes technique family not the
    # Python attribute name).  Default empty is a transition guard; the
    # tests/test_persistence_labels.py audit forbids shipping an empty label.
    label: str = ""
    # Files written by deploy_script that the operator may want immutable-protected
    # alongside the implant binary.  Used by the Generation page when
    # `immutable_scope=binary_and_persistence`.  Empty list = nothing extra.
    protect_paths: list[str] = field(default_factory=list)
    # Auxiliary payload files (relative to iocs/payloads/) the deploy_script needs
    # ON THE BOX but does not itself deliver — e.g. an LD_PRELOAD shim library.
    # _apply_persistence_post_deploy stages each to /tmp/<name> before running the
    # deploy_script and exposes the path via a C3R_PAYLOAD_<UPPER_SNAKE> env var.
    # Empty = the script is self-contained (only wires up the mechanism).
    payloads: list[str] = field(default_factory=list)
    # Tier group used by pivot mode to map tier-name → per-OS mechanism.
    # "scheduled" (cron / scheduled-task), "service" (systemd / Win service / rc.d),
    # "event" (udev / WMI event sub / devd-kqueue).  Empty = uncategorised.
    tier_group: str = ""
    # Maps SA category name → list of substrings to match against displayed
    # field values for IOC artifact highlighting in the SA panel.  Keys match
    # the SA result category keys (e.g. "services", "processes", "scheduled_tasks").
    sa_highlight: dict[str, list[str]] = field(default_factory=dict)
    # Artifacts the hiding library should conceal when this persistence mechanism
    # is active (e.g. service name, cron file, rc.d script basename).  Processed
    # the same way as IOC.wants_hiding: resolved at deploy time, inserted into
    # HidingLibraryConfig, and pushed as an updated XOR blob when enable_hiding=True.
    wants_hiding: list["HidingEntry"] = field(default_factory=list)


@dataclass
class SliverCheck:
    """Marker check type — effect verified via active Sliver session, not NXC.

    implant_identifier: Sliver session/beacon Name to match for auto-registration.
    When a session/beacon connects, any SliverCheck IOC with a matching
    implant_identifier will be auto-registered in DeployedIOCs with source='auto'.
    None = never auto-register (manual-only IOC).

    preferred_type: hint to the C2 UI about which implant type this IOC
    targets ("beacon" | "session" | None).  Used to gate UI buttons and
    filter session listings; does not affect check scoring.
    """

    implant_identifier: str | None = None
    preferred_type: str = "session"


@dataclass
class ScriptCheck:
    """Check type that runs a shell/PowerShell script over NXC."""

    script: str  # path relative to iocs/


@dataclass
class DefaultCredCheck:
    """Marker check — effect verified by an NXC auth probe, not a box script.

    Authenticates with the box's OS-default operator credential (Administrator/
    root from inventory). No script runs on the box; C3rberus probes auth via
    NXC (`check_creds`).
    Scores whether blue teams have hardened the default account:
      status=1  default cred STILL authenticates (compromised — 0 points);
      status=0  auth definitively REJECTED (blue hardened it — full points);
      status=-1 box unreachable / inconclusive (no award this cycle).

    Used only by the per-box virtual `default_cred_<os>` IOCs, which are
    auto-registered as protected DeployedIOCs(source='virtual') rows on startup.
    """


@dataclass
class CredentialSpec:
    """Credentials an IOC's deploy_script creates on the target box.

    `routes/ioc_deploy.deploy_and_register_ioc` inserts a Credentials row
    per spec on successful deploy; `DELETE /c2/iocs/deployed/{id}` removes
    them.  Lets the C2 see and use accounts the red team adds via IOCs
    without manual `POST /c2/creds`.
    """

    username: str
    password: str
    protocol: str          # "smb" | "ssh"


@dataclass
class ImplantSpec:
    """Sliver build parameters for a SliverCheck (implant) IOC.

    Mirrors ``ImplantGenerateRequest`` minus the server-derived ``c2_url`` and
    the deploy-time ``deploy_targets`` — those are supplied by the caller (the
    "Deploy All IOCs" orchestrator builds one binary per implant IOC and
    deploys it to every matching box).  ``goos`` is NOT stored here: it is
    derived from ``IOC.os`` (windows/linux/firewall→freebsd) to avoid drift.

    ``persistence_name`` is the module-level attribute name of an
    ``iocs.persistence.PersistenceMechanism`` (e.g. ``"UdevUinput"``), the same
    label the Generation page sends — resolved via
    ``iocs.persistence.get_by_name`` (strict attr-name lookup, NOT the dataclass
    ``.name`` family field).  ``None`` for bare implants with no persistence.

    ``timestomp=None`` inherits ``IOC.timestomp``; set ``True``/``False`` to
    override per implant.
    """

    format: str = "exe"                  # exe | shared | shellcode | service
    arch: str = "amd64"
    is_beacon: bool = False
    beacon_interval: int = 60            # seconds (only meaningful when is_beacon)
    jitter: int = 0                      # beacon jitter seconds; 0 = half-interval default
    persistence_name: str | None = None  # iocs.persistence attr name, or None
    apply_immutable: bool = False        # chattr +i / chflags schg / attrib+icacls after deploy
    immutable_scope: str = "binary_only"  # binary_only | binary_and_persistence
    timestomp: bool | None = None        # None → inherit IOC.timestomp
    # pivot_gateway=True → this implant IS the pfSense mTLS anchor.  Triggers
    # _maybe_setup_pivot on connect; persistence applied via gateway_persistence_name
    # param in _generate_single_implant (bypasses the standard pivot_gateway guard).
    pivot_gateway: bool = False
    # Logical group name linking the three pivot IOCs (gateway + linux + windows).
    # deploy_all groups IOCs sharing this name and routes them through
    # _generate_pivot_ioc_set instead of _do_implant.  None = standalone.
    pivot_set_name: str | None = None


@dataclass
class Artifact:
    """A concrete on-target trace an IOC leaves behind.

    A path, registry key, service name, or account name a blue team can hunt
    for.  For names generated at deploy time from a pool (random filenames,
    UUID directories, randomized service names) set ``dynamic=True`` and put the
    pattern + pool in ``description`` — ``path`` stays empty and the UI renders
    the description instead of a literal path.
    """

    path: str = ""           # concrete path / key / service / account; "" when dynamic
    dynamic: bool = False     # name generated at deploy time from a pool
    description: str = ""      # required when dynamic; optional annotation otherwise


@dataclass
class IOC:
    """Full IOC definition: identity, OS, check method, optional persistence."""

    name: str
    description: str
    category: str    # remote_access | persistence | users | exfiltration | impair_defenses
    difficulty: int  # 1=Easy 2=Medium 3=Hard 4=Ultra
    os: str          # windows | linux | firewall
    check: SliverCheck | ScriptCheck | DefaultCredCheck
    persistence: PersistenceMechanism | None = None
    payloads: list[str] = field(default_factory=list)
    discovery: str = "Not specified"
    deploy_script: str | None = None      # effect deploy script (relative to iocs/)
    remediate_script: str | None = None   # effect remediate script (relative to iocs/)
    # Credentials the deploy_script creates on the target box.  Auto-
    # registered on deploy, auto-removed on deregister.  See CredentialSpec.
    added_credentials: list[CredentialSpec] = field(default_factory=list)
    # True for IOCs delivered via Sliver session inject (not NXC deploy script).
    # deploy-and-register rejects these; use POST /c2/sessions/{id}/inject-shellcode.
    sliver_inject: bool = False
    # When True, deploy pipelines call timestomp.stomp_files() after the file
    # lands on the target.  ``timestomp_paths`` lists artefact paths the deploy
    # writes (implant binaries are stomped automatically without listing).
    # Must run BEFORE PersistenceMechanism.immutable application: chattr +i /
    # chflags schg block subsequent timestamp writes.
    timestomp: bool = False
    timestomp_paths: list[str] = field(default_factory=list)
    # When True, deploy pipeline stages a second immutable copy of the implant
    # binary to a distinct BACKUP_DIR (higher difficulty tier than DROP_DIR).
    # A watchdog persistence re-trigger re-stages primary from BACKUP_DIR when
    # primary is missing.  Generate page: locked ON for IOCs with backup_copy=True;
    # opt-in toggle (default OFF) for others.  Disabled when storage_mode is
    # self_inject* (no persistent binary on disk).
    backup_copy: bool = False
    # Pooled artifact NAME keys this IOC's scripts consume (docs/PLAN_build_check_
    # deploy_name_pools.md §5B.3). Each entry is (env_key, pool_kind), e.g.
    # ("SERVICE_NAME", "service") — resolve_deploy_params picks a tiered name from
    # naming_pools.pick_unused_name(kind, os, difficulty) and injects it as
    # C3R_<env_key>; the converted deploy/check/remediate read ${C3R_<env_key>:-<old>}.
    # Empty → no name pooling (default behaviour). DROP_DIR is handled separately.
    pooled_names: list[tuple[str, str]] = field(default_factory=list)
    # Like pooled_names but picks N distinct names, injected space-separated as
    # C3R_<env_key>. Each tuple: (env_key, pool_kind, count). Scripts iterate:
    # for U in ${C3R_USER_NAMES}; do ...; done
    pooled_name_lists: list[tuple[str, str, int]] = field(default_factory=list)
    # True when the SCORED artifact is attacker code rather than a name/path, so
    # real bytes must be generated per deploy and injected as C3R_SHELLCODE_B64.
    # resolve_deploy_params() raises if generation fails — an empty payload would
    # make the IOC score as already-remediated the moment it deploys.
    # See docs/PLAN_shellcode_registry_stager.md.
    needs_stager: bool = False
    # When False, unattended "Deploy All IOCs" skips this IOC unless named
    # explicitly via ioc_names. Use for high-blast-radius techniques.
    deploy_all_default: bool = True
    # When True, the IOC is completely removed from deploy_all's fanout (even
    # if named explicitly via ioc_names) AND hidden from the /iocs listing
    # endpoints. Files stay on disk so the IOC can be re-enabled by clearing
    # the flag when the environment supports it. Set for IOCs whose deploy
    # currently fails on every reachable range (missing prereq like OpenSSH
    # capability with no WSUS pointer, unavailable RSAT module without a DC,
    # etc.) so they don't clutter deploy-all output or the operator IOC
    # browser until the range side is fixed.
    disabled: bool = False
    # When True, deploy_all only fans out to Boxes.is_domain_controller=True
    # targets in scope. Skip (rather than fail) a run whose scope contains no
    # DC. Set for IOCs whose deploy touches AD directly (Get-ADDomain, DSInternals,
    # LDAP ACL edits) and would otherwise fail with "module 'ActiveDirectory'
    # was not loaded" on domain members that don't have RSAT installed.
    requires_dc: bool = False
    # When True, the IOC is scored but never deployed by Cerberus — the
    # underlying red-team artefact is planted by an out-of-band script (e.g.
    # ``scripts/infra/rogue_dc_admin_persist.py`` runs the DACL/FSP/trust
    # plants once, manually). The orchestrator auto-registers ONE virtual
    # ``DeployedIOCs`` row per eligible box at startup so scoring picks the
    # check up like any other IOC; ``deploy_script``/``remediate_script`` are
    # expected to be ``None``. Eligibility mirrors ``requires_dc``: True →
    # domain controllers only; False → every matching-OS box. Rows use
    # ``source="virtual"`` and are protected from operator deregistration
    # (same as ``default_cred_<os>``). Applies to ScriptCheck IOCs only.
    pre_deployed: bool = False
    # Per-IOC SA artifact-highlight map; merged with `persistence.sa_highlight`
    # in `GET /c2/iocs/deployed` to drive the SA panel highlighting.  See
    # `PersistenceMechanism.sa_highlight` for the category→substrings shape.
    sa_highlight: dict[str, list[str]] = field(default_factory=dict)
    # Red/admin-only operator metadata — surfaced on the IOC browser (/iocs),
    # gated from blue in routes/iocs.py via include_sensitive.
    # detection_steps: how a blue team finds this IOC (concrete commands/paths).
    # remediation_steps: how a blue team removes it and restores the box.
    # artifacts: concrete traces it leaves (see Artifact); names generated at
    # deploy time use Artifact(dynamic=True, description=...).
    detection_steps: list[str] = field(default_factory=list)
    remediation_steps: list[str] = field(default_factory=list)
    artifacts: list["Artifact"] = field(default_factory=list)
    # Build parameters for SliverCheck IOCs — required for every non-inject
    # SliverCheck IOC so "Deploy All IOCs" knows how to compile the implant.
    # None for ScriptCheck IOCs and for sliver_inject IOCs (which are deployed
    # via session inject, not a standalone build).  See ImplantSpec.
    implant_spec: "ImplantSpec | None" = None
    # Entries the hiding library should conceal when this IOC is active.
    # Deploy flow: ensures the OS-specific hide_library IOC is deployed to the
    # box (deploys it if absent), then adds these entries to HidingLibraryConfig
    # and pushes the XOR-encrypted config blob to the box.
    # Entries auto-removed from DB when this IOC is remediated.
    wants_hiding: "list[HidingEntry]" = field(default_factory=list)

    @property
    def points(self) -> int:
        """Base point value from difficulty (5 / 10 / 20 / 40 by default)."""
        return {
            1: config.DIFFICULTY_EASY_POINTS,
            2: config.DIFFICULTY_MEDIUM_POINTS,
            3: config.DIFFICULTY_HARD_POINTS,
            4: config.DIFFICULTY_ULTRA_POINTS,
        }.get(self.difficulty, 0)
