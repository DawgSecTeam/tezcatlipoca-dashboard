from iocs.base import Artifact, IOC, ScriptCheck

windows_rid_hijack = IOC(
    name="windows_rid_hijack",
    description=(
        "Enables the built-in Guest account and modifies its RID in the SAM F value "
        "from 501 to 500 (Administrator), giving Guest implicit Administrator privileges "
        "at the SAM level without altering group membership."
    ),
    category="users",
    difficulty=3,
    os="windows",
    check=ScriptCheck(script="check_scripts/windows/windows_rid_hijack_check.ps1"),
    discovery=(
        "Check Guest account state: `net user Guest`. "
        "Inspect Guest SAM F value: read HKLM\\SAM\\SAM\\Domains\\Account\\Users\\000001F5 F "
        "and verify bytes 0x30–0x31 equal 0xF4, 0x01 (RID 500)."
    ),
    deploy_script="deploy_scripts/windows/windows_rid_hijack_deploy.ps1",
    remediate_script="remediate_scripts/windows/windows_rid_hijack_remediate.ps1",
    detection_steps=[
        "Run `net user Guest` — check Account active is Yes.",
        "Read SAM key HKLM\\SAM\\SAM\\Domains\\Account\\Users\\000001F5 F value (requires SYSTEM). "
        "Bytes at offset 0x30–0x31 should be 0xF5 0x01 (RID 501); if 0xF4 0x01, RID has been swapped to 500.",
    ],
    remediation_steps=[
        "As SYSTEM: read F value from HKLM\\SAM\\SAM\\Domains\\Account\\Users\\000001F5, "
        "set bytes [0x30]=0xF5 [0x31]=0x01, write back.",
        "Disable Guest account: `net user Guest /active:no`.",
        "Verify: `net user Guest` shows Account active: No.",
    ],
    artifacts=[
        Artifact(
            path="HKLM\\SAM\\SAM\\Domains\\Account\\Users\\000001F5 (F value)",
            description="Guest SAM F value with RID bytes at 0x30-0x31 changed from 501 to 500",
        ),
    ],
)
