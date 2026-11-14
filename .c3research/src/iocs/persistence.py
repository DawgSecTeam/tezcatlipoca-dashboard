"""Shared PersistenceMechanism instances reused across IOC definitions.

Generic instances (service_sd, wmi_root_default) do a pattern scan.
IOC-specific instances encode a known competition name in their check script.

Each instance carries:
  - os: target OS so the Generation page can filter
  - protect_paths: files written by deploy_script that the operator may want
    immutable-protected via the binary+persistence scope
  - tier_group: pivot-mode grouping ("scheduled" | "service" | "event")
"""
from iocs.base import HidingEntry, PersistenceMechanism

# ---------------------------------------------------------------------------
# Windows — generic pattern-based (no specific name required)
# ---------------------------------------------------------------------------

ServiceSD = PersistenceMechanism(
    name="service_sd",
    label="Service SDDL Backdoor",
    tier=2,
    retrigger_interval_seconds=300,
    immutable=False,
    check_script="check_scripts/windows/persistence/service_sd.ps1",
    deploy_script="deploy_scripts/windows/persistence/service_sd_deploy.ps1",
    remediate_script="remediate_scripts/windows/persistence/service_sd_remediate.ps1",
    os="windows",
    tier_group="service",
    # Path template pool-driven via C3R_SVC_NAME (docs/PLAN_build_check_deploy_name_pools.md §5B).
    protect_paths=[r"HKLM:\SYSTEM\CurrentControlSet\Services\${C3R_SVC_NAME:-WinCryptSvc32}"],
    sa_highlight={"services": ["WinCryptSvc32"], "processes": ["svchost.exe"]},
    wants_hiding=[
        HidingEntry(type="service",      value_env_key="SVC_NAME", value_default="WinCryptSvc32"),
        HidingEntry(type="process_name", value_env_key="SVC_NAME", value_default="WinCryptSvc32"),
    ],
)

WmiKillRespawn = PersistenceMechanism(
    name="wmi_kill_respawn",
    label="WMI Kill Watchdog",
    tier=3,
    retrigger_interval_seconds=1,
    immutable=False,
    check_script="check_scripts/windows/persistence/wmi_kill_respawn_persist.ps1",
    deploy_script=None,    # WMI subscription created in IOC deploy_script (not called for ScriptCheck IOCs)
    remediate_script=None, # consumer+binding removed in IOC remediate_script (not called for ScriptCheck IOCs)
    os="windows",
    tier_group="event",
    protect_paths=[],
    sa_highlight={},
)

WMIRootDefault = PersistenceMechanism(
    name="wmi_root_default",
    label="WMI Root\\default Subscription",
    tier=2,
    retrigger_interval_seconds=300,
    immutable=False,
    check_script="check_scripts/windows/persistence/wmi_root_default.ps1",
    deploy_script="deploy_scripts/windows/persistence/wmi_root_default_deploy.ps1",
    remediate_script="remediate_scripts/windows/persistence/wmi_root_default_remediate.ps1",
    os="windows",
    tier_group="event",  # WMI event subscription
    protect_paths=[r"HKLM:\SOFTWARE\Microsoft\Wbem\CIMOM"],  # technique-defining key, not pooled
)

# ---------------------------------------------------------------------------
# Windows — IOC-specific scheduled tasks
# Task names are disguised as system tasks under \Microsoft\Windows\
# ---------------------------------------------------------------------------

ImplantObviousTask = PersistenceMechanism(
    name="scheduled_task",
    label="Scheduled Task (Obvious)",
    tier=1,
    retrigger_interval_seconds=300,
    immutable=False,
    check_script="check_scripts/windows/persistence/schtask_obvious.ps1",
    deploy_script="deploy_scripts/windows/persistence/schtask_obvious_deploy.ps1",
    remediate_script="remediate_scripts/windows/persistence/schtask_obvious_remediate.ps1",
    os="windows",
    tier_group="scheduled",
    protect_paths=[r"C:\Windows\System32\Tasks\${C3R_SCHTASK_NAME:-WinUpdaterCheck}"],
    sa_highlight={"scheduled_tasks": ["WinUpdaterCheck"]},
)

UserScheduledTask = PersistenceMechanism(
    name="scheduled_task",
    label="Scheduled Task (User)",
    tier=1,
    retrigger_interval_seconds=900,
    immutable=False,
    check_script="check_scripts/windows/persistence/schtask_user.ps1",
    deploy_script="deploy_scripts/windows/persistence/schtask_user_deploy.ps1",
    remediate_script="remediate_scripts/windows/persistence/schtask_user_remediate.ps1",
    os="windows",
    tier_group="scheduled",
    protect_paths=[r"C:\Windows\System32\Tasks\${C3R_SCHTASK_NAME:-WinUserSync}"],
    sa_highlight={"scheduled_tasks": ["WinUserSync"]},
)

LogCleanupTask = PersistenceMechanism(
    name="scheduled_task",
    label="Log-Cleanup Scheduled Task",
    tier=1,
    retrigger_interval_seconds=900,
    immutable=False,
    check_script="check_scripts/windows/persistence/schtask_logclean.ps1",
    deploy_script="deploy_scripts/windows/persistence/schtask_logclean_deploy.ps1",
    remediate_script="remediate_scripts/windows/persistence/schtask_logclean_remediate.ps1",
    os="windows",
    tier_group="scheduled",
    # Path template pool-driven via C3R_SCHTASK_NAME
    protect_paths=[r"C:\Windows\System32\Tasks\${C3R_SCHTASK_NAME:-WinDiagCleanup}"],
    sa_highlight={"scheduled_tasks": ["WinDiagCleanup"]},
)

# Windows hidden service for priv-user-service IOC
WindowsPrivUserService = PersistenceMechanism(
    name="service",
    label="Privileged User Service",
    tier=2,
    retrigger_interval_seconds=300,
    immutable=False,
    check_script="check_scripts/windows/persistence/svc_privuser.ps1",
    deploy_script="deploy_scripts/windows/persistence/svc_privuser_deploy.ps1",
    remediate_script="remediate_scripts/windows/persistence/svc_privuser_remediate.ps1",
    os="windows",
    tier_group="service",
    # SVC_NAME = 'WinUserAccountHelper' in svc_privuser_deploy.ps1.
    protect_paths=[r"HKLM:\SYSTEM\CurrentControlSet\Services\WinUserAccountHelper"],
    sa_highlight={
        "services": ["WinUserAccountHelper"],
        "processes": ["WinUserAccountHelper"],
    },
    wants_hiding=[
        HidingEntry(type="service",      value_literal="WinUserAccountHelper"),
        HidingEntry(type="process_name", value_literal="WinUserAccountHelper"),
    ],
)

# Windows attrib+R / icacls DENY — immutable payload protection
WindowsAttribIcacls = PersistenceMechanism(
    name="attrib_icacls",
    label="Attrib + Icacls Deny",
    tier=3,
    retrigger_interval_seconds=300,
    immutable=True,
    check_script="check_scripts/windows/persistence/attrib_icacls.ps1",
    deploy_script="deploy_scripts/windows/persistence/attrib_icacls_deploy.ps1",
    remediate_script="remediate_scripts/windows/persistence/attrib_icacls_remediate.ps1",
    os="windows",
    tier_group="",  # standalone protection mechanism — not exposed in pivot tier picker
    # Path templates pool-driven via C3R_SCHTASK_NAME (docs/PLAN_build_check_deploy_name_pools.md §5B).
    protect_paths=[r"C:\Windows\System32\${C3R_SCHTASK_NAME:-WinDefSvcHelper}.exe"],
    sa_highlight={"processes": ["WinDefSvcHelper"]},
    # process_name only — hide the executable basename (without .exe); full path
    # can't be expressed as a single env_key because deploy appends ".exe".
    wants_hiding=[
        HidingEntry(type="process_name", value_env_key="SCHTASK_NAME", value_default="WinDefSvcHelper"),
    ],
)

# Windows IFEO service watchdog
WindowsIFEOService = PersistenceMechanism(
    name="service",
    label="IFEO Debugger Service",
    tier=2,
    retrigger_interval_seconds=600,
    immutable=False,
    check_script="check_scripts/windows/persistence/svc_ifeo.ps1",
    deploy_script="deploy_scripts/windows/persistence/svc_ifeo_deploy.ps1",
    remediate_script="remediate_scripts/windows/persistence/svc_ifeo_remediate.ps1",
    os="windows",
    tier_group="service",
    # Path template pool-driven via C3R_SERVICE_NAME
    protect_paths=[r"HKLM:\SYSTEM\CurrentControlSet\Services\${C3R_SERVICE_NAME:-WinDefSvcHelper}"],
    sa_highlight={
        "services": ["WinDefSvcHelper"],
        "processes": ["WinDefSvcHelper"],
    },
    wants_hiding=[
        HidingEntry(type="service",      value_env_key="SERVICE_NAME", value_default="WinDefSvcHelper"),
        HidingEntry(type="process_name", value_env_key="SERVICE_NAME", value_default="WinDefSvcHelper"),
    ],
)

# ---------------------------------------------------------------------------
# Linux — IOC-specific systemd services
# ---------------------------------------------------------------------------

ImplantObviousSystemd = PersistenceMechanism(
    name="systemd",
    label="Systemd Service (Obvious)",
    tier=1,
    retrigger_interval_seconds=1,
    immutable=False,
    check_script="check_scripts/linux/persistence/systemd_obvious.sh",
    deploy_script="deploy_scripts/linux/persistence/systemd_obvious_deploy.sh",
    remediate_script="remediate_scripts/linux/persistence/systemd_obvious_remediate.sh",
    os="linux",
    tier_group="service",
    protect_paths=["/etc/systemd/system/${C3R_SERVICE_NAME:-systemd-network-proxy}.service"],
    sa_highlight={
        "services": ["systemd-network-proxy"],
        "processes": ["systemd-network-proxy"],
    },
    wants_hiding=[
        HidingEntry(type="service",      value_env_key="SERVICE_NAME", value_default="systemd-network-proxy"),
        HidingEntry(type="process_name", value_env_key="SERVICE_NAME", value_default="systemd-network-proxy"),
    ],
)

ImplantHiddenSystemd = PersistenceMechanism(
    name="systemd",
    label="Systemd Service (Hidden)",
    tier=2,
    retrigger_interval_seconds=1,
    immutable=False,
    check_script="check_scripts/linux/persistence/systemd_hidden.sh",
    deploy_script="deploy_scripts/linux/persistence/systemd_hidden_deploy.sh",
    remediate_script="remediate_scripts/linux/persistence/systemd_hidden_remediate.sh",
    os="linux",
    tier_group="service",
    protect_paths=["/usr/local/bin/.syslogd"],
    sa_highlight={"processes": [".syslogd"]},
    wants_hiding=[
        HidingEntry(type="process_name", value_literal=".syslogd"),
        HidingEntry(type="file_path",    value_literal="/usr/local/bin/.syslogd"),
    ],
)

PrivUserSystemd = PersistenceMechanism(
    name="systemd",
    label="Systemd Priv-User Unit",
    tier=1,
    retrigger_interval_seconds=300,
    immutable=False,
    check_script="check_scripts/linux/persistence/systemd_privuser.sh",
    deploy_script="deploy_scripts/linux/persistence/systemd_privuser_deploy.sh",
    remediate_script="remediate_scripts/linux/persistence/systemd_privuser_remediate.sh",
    os="linux",
    tier_group="service",
    # SERVICE="user-account-helper" in systemd_privuser_deploy.sh.
    sa_highlight={
        "services": ["user-account-helper"],
        "processes": ["user-account-helper"],
    },
    wants_hiding=[
        HidingEntry(type="service",      value_literal="user-account-helper"),
        HidingEntry(type="process_name", value_literal="user-account-helper"),
    ],
)

SshdInjectSystemd = PersistenceMechanism(
    name="systemd",
    label="sshd LD_PRELOAD (systemd)",
    tier=1,
    retrigger_interval_seconds=1,
    immutable=False,
    check_script="check_scripts/linux/persistence/systemd_sshd.sh",
    deploy_script="deploy_scripts/linux/persistence/systemd_sshd_deploy.sh",
    remediate_script="remediate_scripts/linux/persistence/systemd_sshd_remediate.sh",
    os="linux",
    tier_group="service",
    protect_paths=["/usr/local/lib/libpam_recv.so"],
    # The deploy_script wires up the systemd unit + LD_PRELOAD but cannot embed
    # the shim binary; stage it so the script can install it to /usr/local/lib.
    payloads=["libpam_recv.so"],
    sa_highlight={
        "services": ["libpam_recv"],
        "processes": ["libpam_recv"],
    },
    wants_hiding=[
        HidingEntry(type="file_path",    value_literal="/usr/local/lib/libpam_recv.so"),
        HidingEntry(type="process_name", value_literal="libpam_recv"),
    ],
)

# Linux cron watchdog for http.server process
HttpServerCron = PersistenceMechanism(
    name="cron",
    label="HTTP Server Cron",
    tier=1,
    retrigger_interval_seconds=120,
    immutable=False,
    check_script="check_scripts/linux/persistence/cron_http_server.sh",
    deploy_script="deploy_scripts/linux/persistence/cron_http_server_deploy.sh",
    remediate_script="remediate_scripts/linux/persistence/cron_http_server_remediate.sh",
    os="linux",
    tier_group="scheduled",
    # Path template pool-driven via C3R_CRON_NAME
    protect_paths=["/etc/cron.d/${C3R_CRON_NAME:-system-http-watchdog}"],
    sa_highlight={
        "processes": ["http.server"],
        "scheduled_tasks": ["system-http-watchdog"],
    },
    wants_hiding=[
        HidingEntry(type="cron_file", value_env_key="CRON_NAME", value_default="system-http-watchdog"),
    ],
)

# Linux cron log-cleaner
LinuxLogCleanCron = PersistenceMechanism(
    name="cron",
    label="Log-Cleanup Cron",
    tier=1,
    retrigger_interval_seconds=900,
    immutable=False,
    check_script="check_scripts/linux/persistence/cron_logclean.sh",
    deploy_script="deploy_scripts/linux/persistence/cron_logclean_deploy.sh",
    remediate_script="remediate_scripts/linux/persistence/cron_logclean_remediate.sh",
    os="linux",
    tier_group="scheduled",
    # CRON_LINE in cron_logclean_deploy.sh: journalctl --vacuum-time
    sa_highlight={"scheduled_tasks": ["journalctl"]},
)

# Linux kill-shim — immutable /bin/kill flag (chattr +i)
LinuxKillShimImmutable = PersistenceMechanism(
    name="kill_shim_immutable",
    label="Kill Shim (Immutable)",
    tier=3,
    retrigger_interval_seconds=1,
    immutable=True,
    check_script="check_scripts/linux/persistence/linux_kill_shim_persist.sh",
    deploy_script=None,    # chattr +i in IOC deploy_script (not called for ScriptCheck IOCs)
    remediate_script=None, # chattr -i in IOC remediate_script (not called for ScriptCheck IOCs)
    os="linux",
    tier_group="event",
    protect_paths=["/bin/kill", "/bin/.kill.orig"],
    sa_highlight={},
)

UdevUinput = PersistenceMechanism(
    name="udev_uinput",
    label="Udev uinput Trigger",
    tier=3,
    retrigger_interval_seconds=300,
    immutable=True,
    check_script="check_scripts/linux/persistence/udev_uinput.sh",
    deploy_script="deploy_scripts/linux/persistence/udev_uinput_deploy.sh",
    remediate_script="remediate_scripts/linux/persistence/udev_uinput_remediate.sh",
    os="linux",
    tier_group="event",
    protect_paths=["/etc/udev/rules.d/99-uinput.rules", "/usr/local/bin/${C3R_TICKER_NAME:-acpi_handler}"],
    sa_highlight={"processes": ["acpi_handler"]},
    wants_hiding=[
        HidingEntry(type="file_path",    value_literal="/etc/udev/rules.d/99-uinput.rules"),
        HidingEntry(type="process_name", value_env_key="TICKER_NAME", value_default="acpi_handler"),
    ],
)

# Linux hidden service for priv-user-service IOC
LinuxPrivUserService = PersistenceMechanism(
    name="systemd",
    label="Priv-User Systemd Unit",
    tier=2,
    retrigger_interval_seconds=300,
    immutable=False,
    check_script="check_scripts/linux/persistence/systemd_privuser.sh",
    deploy_script="deploy_scripts/linux/persistence/systemd_privuser_deploy.sh",
    remediate_script="remediate_scripts/linux/persistence/systemd_privuser_remediate.sh",
    os="linux",
    tier_group="service",
    # SERVICE="user-account-helper" in systemd_privuser_deploy.sh.
    sa_highlight={
        "services": ["user-account-helper"],
        "processes": ["user-account-helper"],
    },
    wants_hiding=[
        HidingEntry(type="service",      value_literal="user-account-helper"),
        HidingEntry(type="process_name", value_literal="user-account-helper"),
    ],
)

# ---------------------------------------------------------------------------
# pfSense / Firewall
# ---------------------------------------------------------------------------

ImplantDaemonRC = PersistenceMechanism(
    name="daemon_rc",
    label="rc.conf.local Daemon",
    tier=1,
    retrigger_interval_seconds=5,
    immutable=False,
    check_script="check_scripts/firewall/persistence/daemon_rc_implant.sh",
    deploy_script="deploy_scripts/firewall/persistence/daemon_rc_implant_deploy.sh",
    remediate_script="remediate_scripts/firewall/persistence/daemon_rc_implant_remediate.sh",
    os="firewall",
    tier_group="service",
    # Path templates pool-driven via C3R_SERVICE_NAME (docs/PLAN_build_check_deploy_name_pools.md §5B).
    protect_paths=[
        "/usr/local/etc/rc.d/${C3R_SERVICE_NAME:-syslog_relay}",
        "/usr/local/bin/${C3R_SERVICE_NAME:-syslog_relay}",
    ],
    sa_highlight={
        "services": ["syslog_relay"],
        "processes": ["syslog_relay"],
    },
    wants_hiding=[
        HidingEntry(type="service",      value_env_key="SERVICE_NAME", value_default="syslog_relay"),
        HidingEntry(type="process_name", value_env_key="SERVICE_NAME", value_default="syslog_relay"),
    ],
)

FakeShellDaemonRC = PersistenceMechanism(
    name="daemon_rc",
    label="Fake-Shell Daemon (rc)",
    tier=1,
    retrigger_interval_seconds=900,
    immutable=True,
    check_script="check_scripts/firewall/persistence/daemon_rc_fakeshell.sh",
    deploy_script="deploy_scripts/firewall/persistence/daemon_rc_fakeshell_deploy.sh",
    remediate_script="remediate_scripts/firewall/persistence/daemon_rc_fakeshell_remediate.sh",
    os="firewall",
    tier_group="service",
    # Path templates pool-driven via C3R_SERVICE_NAME
    protect_paths=[
        "/usr/local/etc/rc.d/${C3R_SERVICE_NAME:-sysstats}_svc",
        "/usr/local/sbin/${C3R_SERVICE_NAME:-sysstats}",
    ],
    sa_highlight={
        "services": ["sysstats_svc"],
        "processes": ["sysstats"],
    },
    # process_name = env_key value (e.g. "sysstats"); rc.d service appends "_svc"
    # suffix which can't be expressed as a single env_key, so service type is skipped.
    wants_hiding=[
        HidingEntry(type="process_name", value_env_key="SERVICE_NAME", value_default="sysstats"),
    ],
)

DevdKqueue = PersistenceMechanism(
    name="devd_kqueue",
    label="devd kqueue Trigger",
    tier=3,
    retrigger_interval_seconds=300,
    immutable=True,
    check_script="check_scripts/firewall/persistence/devd_kqueue.sh",
    deploy_script="deploy_scripts/firewall/persistence/devd_kqueue_deploy.sh",
    remediate_script="remediate_scripts/firewall/persistence/devd_kqueue_remediate.sh",
    os="firewall",
    tier_group="event",
    protect_paths=["/usr/local/etc/devd/ifstatus.conf", "/usr/local/bin/${C3R_TICKER_NAME:-devd_helper}"],
    sa_highlight={"processes": ["devd_helper"]},
    wants_hiding=[
        HidingEntry(type="file_path",    value_literal="/usr/local/etc/devd/ifstatus.conf"),
        HidingEntry(type="process_name", value_env_key="TICKER_NAME", value_default="devd_helper"),
    ],
)

FirewallFlushRulesCron = PersistenceMechanism(
    name="cron",
    label="Allow-All Ruleset Cron",
    tier=1,
    retrigger_interval_seconds=300,
    immutable=False,
    check_script="check_scripts/firewall/persistence/cron_flushrules.sh",
    deploy_script="deploy_scripts/firewall/persistence/cron_flushrules_deploy.sh",
    remediate_script="remediate_scripts/firewall/persistence/cron_flushrules_remediate.sh",
    os="firewall",
    tier_group="scheduled",
    # CRON_MARKER="pfctl_allow_all_ioc" in cron_flushrules_deploy.sh.
    sa_highlight={"scheduled_tasks": ["pfctl_allow_all_ioc"]},
)

FirewallClearLogCron = PersistenceMechanism(
    name="cron",
    label="Log-Clear Cron",
    tier=1,
    retrigger_interval_seconds=900,
    immutable=False,
    check_script="check_scripts/firewall/persistence/cron_clearlog.sh",
    deploy_script="deploy_scripts/firewall/persistence/cron_clearlog_deploy.sh",
    remediate_script="remediate_scripts/firewall/persistence/cron_clearlog_remediate.sh",
    os="firewall",
    tier_group="scheduled",
    # CRON_ENTRY marker "syslog_clear_ioc" in cron_clearlog_deploy.sh.
    sa_highlight={"scheduled_tasks": ["syslog_clear_ioc"]},
)

# pfSense kill-shim — immutable /bin/kill flag (chflags schg)
FirewallKillShimImmutable = PersistenceMechanism(
    name="kill_shim_bsd_immutable",
    label="Kill Shim (BSD Immutable)",
    tier=3,
    retrigger_interval_seconds=1,
    immutable=True,
    check_script="check_scripts/firewall/persistence/firewall_kill_shim_persist.sh",
    deploy_script=None,    # chflags schg in IOC deploy_script (not called for ScriptCheck IOCs)
    remediate_script=None, # chflags noschg in IOC remediate_script (not called for ScriptCheck IOCs)
    os="firewall",
    tier_group="event",
    protect_paths=["/bin/kill", "/bin/.kill.orig"],
    sa_highlight={},
)

NginxShimRC = PersistenceMechanism(
    name="nginx_shim_rc",
    label="Nginx Shim (rc.local)",
    tier=2,
    retrigger_interval_seconds=5,
    immutable=False,
    check_script="check_scripts/firewall/nginx_shim_rc.sh",
    deploy_script="deploy_scripts/firewall/nginx_shim_rc_deploy.sh",
    remediate_script="remediate_scripts/firewall/nginx_shim_rc_remediate.sh",
    os="firewall",
    tier_group="service",
    protect_paths=["/usr/local/bin/.sysdb", "/usr/local/lib/libssl_util.so"],
    payloads=["libssl_util.so"],
    sa_highlight={"processes": ["libssl_util"]},
    wants_hiding=[
        HidingEntry(type="process_name", value_literal="libssl_util"),
        HidingEntry(type="file_path",    value_literal="/usr/local/lib/libssl_util.so"),
        HidingEntry(type="file_path",    value_literal="/usr/local/bin/.sysdb"),
    ],
)

WinInjectWMIRespawn = PersistenceMechanism(
    name="win_inject_wmi_respawn",
    label="WMI Self-Inject Respawn",
    tier=3,
    retrigger_interval_seconds=60,
    immutable=False,
    check_script="check_scripts/windows/persistence/win_inject_wmi_check.ps1",
    deploy_script="deploy_scripts/windows/persistence/win_inject_wmi_deploy.ps1",
    remediate_script="remediate_scripts/windows/persistence/win_inject_wmi_remediate.ps1",
    os="windows",
    tier_group="event",
    protect_paths=[],
    sa_highlight={"services": ["WinLanSvc"]},
)

# ---------------------------------------------------------------------------
# Shared-library (.so) implant persistence — re-launch the native so_loader
# (docs/PLAN_freebsd_linux_so_implant.md). The loader + .so live in dynamic
# pool deploy dirs, so only the fixed service-file path is protect-able here;
# C3R_LOADER_PATH / C3R_SO_PATH are injected into the deploy script at deploy
# time by _apply_persistence_post_deploy.
# ---------------------------------------------------------------------------
SoLoaderDaemonRC = PersistenceMechanism(
    name="so_loader_daemon_rc",
    label=".so Loader (rc.conf.local)",
    tier=2,
    retrigger_interval_seconds=30,  # rc.d re-launches the loader after boot/crash
    immutable=False,
    check_script="check_scripts/firewall/persistence/so_loader_daemon_rc.sh",
    deploy_script="deploy_scripts/firewall/persistence/so_loader_daemon_rc_deploy.sh",
    remediate_script="remediate_scripts/firewall/persistence/so_loader_daemon_rc_remediate.sh",
    os="firewall",
    tier_group="service",
    # Path template pool-driven via C3R_SERVICE_NAME.
    protect_paths=[
        "/usr/local/etc/rc.d/${C3R_SERVICE_NAME:-c3r_net}",
    ],
    sa_highlight={
        "services": ["c3r_net"],
        "processes": ["so_loader"],
    },
    wants_hiding=[
        HidingEntry(type="service",      value_env_key="SERVICE_NAME", value_default="c3r_net"),
        HidingEntry(type="process_name", value_literal="so_loader_freebsd"),
    ],
)

SoLoaderSystemd = PersistenceMechanism(
    name="so_loader_systemd",
    label=".so Loader (systemd)",
    tier=2,
    retrigger_interval_seconds=30,  # systemd Restart= re-launches the loader
    immutable=False,
    check_script="check_scripts/linux/persistence/so_loader_systemd.sh",
    deploy_script="deploy_scripts/linux/persistence/so_loader_systemd_deploy.sh",
    remediate_script="remediate_scripts/linux/persistence/so_loader_systemd_remediate.sh",
    os="linux",
    tier_group="service",
    protect_paths=[
        "/etc/systemd/system/${C3R_SERVICE_NAME:-c3r-net}.service",
    ],
    sa_highlight={
        "services": ["c3r-net"],
        "processes": ["so_loader"],
    },
    wants_hiding=[
        HidingEntry(type="service",      value_env_key="SERVICE_NAME", value_default="c3r-net"),
        HidingEntry(type="process_name", value_literal="so_loader_linux"),
    ],
)

# Legacy aliases kept for backwards compat with existing IOC definitions
# (linux_adduser_crontab uses CronWatchdog via ioc deploy_script directly,
#  not as a PersistenceMechanism, but some tests may reference these names)
SystemdRestart = ImplantObviousSystemd
CronWatchdog = LinuxLogCleanCron
DaemonRC = ImplantDaemonRC
FirewallCron = FirewallFlushRulesCron

# Windows legacy alias
WindowsScheduledTask = UserScheduledTask
WindowsHiddenService = WindowsPrivUserService
LinuxHiddenService = LinuxPrivUserService


# ---------------------------------------------------------------------------
# Lookup helpers — used by the Generation page + check pipeline
# ---------------------------------------------------------------------------

def _all_mechanisms() -> list[PersistenceMechanism]:
    """Return every PersistenceMechanism defined at module scope."""
    return [
        v for v in globals().values()
        if isinstance(v, PersistenceMechanism)
    ]


def get_by_name(name: str) -> PersistenceMechanism | None:
    """Look up a mechanism by its module-level Python attribute name.

    The label the Generation page sends back, e.g. ``"WindowsAttribIcacls"``.

    Strict attr-name lookup only — does **not** match on the dataclass
    `name` field, since multiple mechanisms share the same family name
    (e.g. "systemd" maps to 5 different Linux mechanisms).  Legacy
    DeployedIOCs.persistence_mechanism rows that store the family name
    therefore return None here; callers should fall back to the IOC's
    bundled `ioc.persistence` in that case.
    """
    if not name:
        return None
    obj = globals().get(name)
    if isinstance(obj, PersistenceMechanism):
        return obj
    return None


def by_os(os: str) -> list[tuple[str, PersistenceMechanism]]:
    """Return [(attr_name, mechanism), ...] filtered to the given OS.

    Aliases (e.g. ``SystemdRestart = ImplantObviousSystemd``) are deduplicated
    by Python object id — only the first attribute pointing at a given
    mechanism is returned, sorted alphabetically so the canonical
    name typically wins.
    """
    seen_ids: set[int] = set()
    out: list[tuple[str, PersistenceMechanism]] = []
    for attr in sorted(globals().keys()):
        val = globals()[attr]
        if not isinstance(val, PersistenceMechanism):
            continue
        if val.os != os:
            continue
        if id(val) in seen_ids:
            continue
        seen_ids.add(id(val))
        out.append((attr, val))
    out.sort(key=lambda r: (r[1].tier_group or "z", r[1].name, r[0]))
    return out


def by_tier_group(tier_group: str, os: str) -> PersistenceMechanism | None:
    """Pivot-mode helper: find one mechanism in the given tier_group for the given OS.

    Returns the lowest-tier match for stability.
    """
    candidates = [m for _, m in by_os(os) if m.tier_group == tier_group]
    if not candidates:
        return None
    candidates.sort(key=lambda m: m.tier)
    return candidates[0]
