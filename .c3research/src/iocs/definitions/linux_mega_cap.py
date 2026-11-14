from iocs.base import IOC, Artifact, ScriptCheck

linux_mega_cap = IOC(
    name="linux_mega_cap",
    description="nano binary with cap_dac_override capability set",
    category="impair_defenses",
    difficulty=1,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_mega_cap_check.sh"),
    discovery=(
        "Check for files with capabilities. "
        "Check for files with any capabilities, as few should have any. "
        "The getcap utility can be used to check a given file for any capabilities."
    ),
    deploy_script="deploy_scripts/linux/linux_mega_cap_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_mega_cap_remediate.sh",
    sa_highlight={"config_changes": ["/usr/bin/mega"]},
    detection_steps=[
        "Check the target file for Linux capabilities: "
        "`getcap /usr/bin/mega`. A positive result looks like "
        "`/usr/bin/mega cap_dac_override=eip`.",
        "Confirm the file exists and inspect it: `ls -la /usr/bin/mega` and "
        "`file /usr/bin/mega`. It is a copy of an editor/coreutils binary "
        "(nano when available, otherwise a fallback such as vi/cat).",
        "Run a broader capability scan to catch any other elevated binaries: "
        "`getcap -r /usr/bin/ 2>/dev/null` or `getcap -r / 2>/dev/null`.",
        "Verify the capability is actively set (not just the file present): the check "
        "script uses `getcap /usr/bin/mega | grep cap_dac_override` — if the file "
        "exists but the capability was stripped, the IOC is considered remediated.",
    ],
    remediation_steps=[
        "Strip all capabilities from the file: `sudo setcap -r /usr/bin/mega`. "
        "This alone satisfies the check script (cap_dac_override no longer present).",
        "Delete the planted binary: `sudo rm -f /usr/bin/mega`. "
        "Also check and remove `/bin/mega` if present: `sudo rm -f /bin/mega`.",
        "Confirm the file is gone and no capability remains: "
        "`getcap /usr/bin/mega 2>&1` should return no output or a 'no such file' error, "
        "and `ls /usr/bin/mega /bin/mega 2>&1` should show both absent.",
    ],
    artifacts=[
        Artifact(
            path="/usr/bin/mega",
            description="Copy of an editor/coreutils binary (nano when available) "
            "with cap_dac_override+eip capability set",
        ),
        Artifact(
            path="/bin/mega",
            description="Alternate drop path cleaned by remediate script",
        ),
    ],
)
