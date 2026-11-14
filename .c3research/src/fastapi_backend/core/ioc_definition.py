"""Loads IOC definitions from ``iocs/definitions/`` and validates referenced scripts."""

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import ClassVar


def _ensure_project_root_on_path() -> Path:
    project_root = Path(__file__).parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    return project_root


_ensure_project_root_on_path()

from iocs.base import (  # noqa: E402  (must come after path setup)
    IOC,
    ScriptCheck,
    SliverCheck,
)

# Re-export IOC as IOCDefinition for backward compatibility
IOCDefinition = IOC


# Payloads whose files aren't required to be on disk at loader-validate time:
# core/payload_resolver._resolve_beacon auto-generates them via Sliver-gRPC on
# first deploy (see lifespan_helpers.purge_stale_startup_beacons — startup even
# deletes stale copies so the auto-regenerate path exercises). Requiring the
# file at loader time was over-strict and silently dropped every IOC that
# listed these as aux payloads (firewall_kill_shim, linux_kill_shim), breaking
# soak coverage.
_RUNTIME_PROVIDED_PAYLOADS: frozenset[str] = frozenset({
    "beacon_linux",
    "beacon_freebsd",
    "beacon_windows",
    "msupdate.exe",
})


class IOCDefinitionLoader:
    """Loads and manages IOC definitions from Python modules in iocs/definitions/."""

    # Class-level mirror of the last load_ioc_definitions() call's dropped-IOC list.
    # Consumers that don't hold the instance (e.g. bench_soak's build-check
    # preflight, docs/PLAN_soak_full_coverage.md §6.2.5) can read this.
    _last_filtered_incomplete: ClassVar[list[str]] = []

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        self.ioc_definitions: dict[str, IOC] = {}
        self.dropped_ioc_names: list[str] = []
        self.os_ioc_mapping: dict[str, list[IOC]] = {
            "windows": [],
            "linux": [],
            "firewall": [],
        }

    def load_ioc_definitions(self) -> None:
        """Load all IOC definitions from Python modules in iocs/definitions/."""
        project_root = Path(__file__).parent.parent.parent
        definitions_path = project_root / "iocs" / "definitions"
        py_files = [
            f for f in definitions_path.glob("*.py")
            if f.name != "__init__.py"
        ]

        self.logger.info(f"Loading IOC definitions from {len(py_files)} modules...")

        loaded: list[IOC] = []
        for py_file in sorted(py_files):
            try:
                spec = importlib.util.spec_from_file_location(
                    f"iocs.definitions.{py_file.stem}", py_file
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                for _attr_name, obj in inspect.getmembers(module):
                    if isinstance(obj, IOC):
                        loaded.append(obj)

            except Exception as e:
                self.logger.error(f"Error loading {py_file}: {e}")

        self.logger.info(f"Discovered {len(loaded)} IOC definitions, validating scripts...")
        self._populate_from_list(loaded)
        self._validate_ioc_scripts()

        self.logger.info(f"Loaded {len(self.ioc_definitions)} complete IOC definitions:")
        for os_name, iocs in self.os_ioc_mapping.items():
            self.logger.info(f"  - {os_name}: {len(iocs)} IOCs")

    def _populate_from_list(self, iocs: list[IOC]) -> None:
        self.ioc_definitions = {}
        self.os_ioc_mapping = {"windows": [], "linux": [], "firewall": []}
        for ioc in iocs:
            self.ioc_definitions[ioc.name] = ioc
            if ioc.os in self.os_ioc_mapping:
                self.os_ioc_mapping[ioc.os].append(ioc)
            else:
                self.logger.warning(f"Unknown OS '{ioc.os}' for IOC '{ioc.name}'")

    def _validate_ioc_scripts(self) -> None:
        project_root = Path(__file__).parent.parent.parent
        complete: dict[str, IOC] = {}
        incomplete: list[str] = []

        for name, ioc in self.ioc_definitions.items():
            missing = []

            # Validate effect check script (ScriptCheck only)
            if isinstance(ioc.check, ScriptCheck):
                path = project_root / "iocs" / ioc.check.script
                if not path.exists():
                    missing.append(f"check script: {path}")

            # Validate effect deploy script (non-fatal warning if missing)
            if ioc.deploy_script:
                path = project_root / "iocs" / ioc.deploy_script
                if not path.exists():
                    self.logger.warning(f"IOC '{name}': deploy_script not found: {path}")

            # Validate effect remediate script (non-fatal)
            if ioc.remediate_script:
                path = project_root / "iocs" / ioc.remediate_script
                if not path.exists():
                    self.logger.warning(f"IOC '{name}': remediate_script not found: {path}")

            # Validate payloads — skipped for SliverCheck IOCs because Sliver
            # payloads (beacon_linux, beacon_win.exe, beacon_freebsd) are generated
            # at runtime and are never committed to the repository.
            # ScriptCheck IOCs may also reference runtime-provided Sliver beacons
            # as aux payloads (kill_shim IOCs → beacon_linux/beacon_freebsd);
            # skip those specific names — they're auto-resolved at deploy time
            # by core/payload_resolver._resolve_beacon.
            if not isinstance(ioc.check, SliverCheck):
                for payload in ioc.payloads:
                    if payload in _RUNTIME_PROVIDED_PAYLOADS:
                        continue
                    path = project_root / "iocs" / "payloads" / payload
                    if not path.exists():
                        missing.append(f"payload: {path}")

            # Every non-inject SliverCheck IOC MUST carry an ImplantSpec so the
            # "Deploy All IOCs" orchestrator knows how to compile its implant.
            # Non-fatal here (the IOC still loads) — the hard gate is the unit
            # test tests/test_ioc_definitions.py; "Deploy All" skips spec-less
            # implant IOCs at runtime. inject IOCs are deployed via session
            # inject and intentionally carry no spec.
            if (
                isinstance(ioc.check, SliverCheck)
                and not getattr(ioc, "sliver_inject", False)
                and ioc.implant_spec is None
            ):
                self.logger.warning(
                    "IOC '%s': SliverCheck without implant_spec — "
                    "'Deploy All IOCs' will skip it",
                    name,
                )

            if missing:
                incomplete.append(name)
                self.logger.warning(f"Skipping incomplete IOC '{name}': {missing}")
            else:
                complete[name] = ioc

        self.ioc_definitions = complete
        for os_name in self.os_ioc_mapping:
            self.os_ioc_mapping[os_name] = [
                ioc for ioc in self.os_ioc_mapping[os_name] if ioc.name in complete
            ]

        self.dropped_ioc_names = list(incomplete)
        # Mirror to class attr so out-of-band consumers can read the last drop set.
        type(self)._last_filtered_incomplete = list(incomplete)
        if incomplete:
            self.logger.warning(f"Filtered out {len(incomplete)} incomplete IOCs: {incomplete}")

        self.logger.info(f"Validated {len(complete)} complete IOCs successfully")
        if not complete:
            self.logger.error("No complete IOCs available — all IOCs are missing required scripts")
            raise FileNotFoundError("No complete IOCs available")

    def get_iocs_for_os(self, os: str) -> list[IOC]:
        """Get all IOCs for a specific OS."""
        return self.os_ioc_mapping.get(os.lower(), [])

