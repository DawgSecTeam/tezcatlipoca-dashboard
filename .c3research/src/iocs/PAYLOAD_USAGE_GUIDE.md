# IOC Payload Usage Guide

## Overview

IOCs can include payload files that are automatically copied to target systems before running check or deployment scripts. Payloads are defined in the Python IOC definition using the `payloads` field of the `IOC` dataclass.

## Defining Payloads in IOC Definitions

IOCs are Python modules under `iocs/definitions/`. Payloads are listed in the `IOC` dataclass `payloads` field:

### Single Payload
```python
from iocs.base import IOC, ScriptCheck

my_ioc = IOC(
    name="my_ioc",
    description="...",
    category="persistence",
    difficulty=2,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/my_ioc_check.sh"),
    payloads=["malware_sample.bin"],
)
```

### Multiple Payloads
```python
from iocs.base import IOC, ScriptCheck, PersistenceMechanism

my_ioc = IOC(
    name="advanced_backdoor_linux",
    description="Advanced backdoor with multiple components",
    category="persistence",
    difficulty=3,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/advanced_backdoor_check.sh"),
    deploy_script="deploy_scripts/linux/advanced_backdoor_deploy.sh",
    remediate_script="remediate_scripts/linux/advanced_backdoor_remediate.sh",
    payloads=["backdoor_binary", "config.json", "persistence.sh"],
)
```

### No Payloads (default)
```python
my_ioc = IOC(
    name="my_ioc",
    # ...
    # payloads omitted — defaults to []
)
```

## Payload Storage

All payload files must be placed in the flat directory:
```
iocs/payloads/
```

## Payload Deployment Locations

### Linux/Unix Systems
- Payloads are copied to: `/tmp/`
- Files are made executable (mode 0755)

### Windows Systems
- Payloads are copied to: `C:\Windows\Temp\`

## Using Payloads in Scripts

Check and deploy scripts can access the copied payloads at their deployed locations:

### Linux Script Example
```bash
#!/bin/bash
# Payloads are available in /tmp/
if [ -f "/tmp/config.json" ]; then
    cat /tmp/config.json
fi

if [ -f "/tmp/backdoor.sh" ]; then
    chmod +x /tmp/backdoor.sh
    /tmp/backdoor.sh &
fi
```

### Windows Script Example
```powershell
# Payloads are available in C:\Windows\Temp\
$config = Get-Content "C:\Windows\Temp\config.xml"
Start-Process "C:\Windows\Temp\malware.exe" -WindowStyle Hidden
```

## Complete Example with Persistence

```python
from iocs.base import IOC, ScriptCheck, PersistenceMechanism

windows_implant = IOC(
    name="windows_implant_wmi",
    description="Sliver beacon persisted via WMI event subscription",
    category="persistence",
    difficulty=3,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_implant_wmi_check.ps1"),
    payloads=["beacon_windows.exe"],
    persistence=PersistenceMechanism(
        name="WmiEventSubscription",
        tier=3,
        retrigger_interval_seconds=120,
        check_script="check_scripts/windows/wmi_persist_check.ps1",
        deploy_script="deploy_scripts/windows/wmi_persist_deploy.ps1",
        remediate_script="remediate_scripts/windows/wmi_persist_remediate.ps1",
    ),
)
```

## Validation

- The system validates that all specified payload files exist in `iocs/payloads/`
- IOCs with missing payloads are automatically filtered out during startup
- Validation errors are logged with details about which files are missing

## Sliver Inject IOCs

For Sliver shellcode injection IOCs, set `sliver_inject=True` instead of using `deploy_script`. These IOCs use the `POST /c2/sessions/{session_id}/inject-shellcode` endpoint, which resolves payloads via `_resolve_ioc_payload()` — checking on-disk files first, then querying the `ImplantBuilds` DB table for a matching built binary.

```python
from iocs.base import IOC, SliverCheck
from iocs.persistence import ServiceSD

windows_inject_ioc = IOC(
    name="windows_implant_inject",
    description="Sliver shellcode injected into spoolsv or LanmanServer svchost",
    category="persistence",
    difficulty=3,
    os="windows",
    check=SliverCheck(preferred_type="beacon"),
    sliver_inject=True,
    payloads=["shellcode_win.bin", "injector.exe"],
    persistence=ServiceSD,
)
```

## Best Practices

1. **Use descriptive payload names** that indicate their purpose
2. **Validate payload files exist** in `iocs/payloads/` before referencing the IOC
3. **Keep payload files small** to minimize network transfer time
4. **Use multiple payloads** to simulate realistic attack scenarios with staged components
5. **Pre-competition build check**: run `scripts/testing/preflight_check_payloads.sh` before competition to verify all referenced payload binaries are present and built
