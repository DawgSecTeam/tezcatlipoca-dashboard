from iocs.base import IOC, Artifact, ScriptCheck

linux_vim_suid = IOC(
    name="linux_vim_suid",
    description="vim binary is owned by root and has SUID bit set",
    category="impair_defenses",
    difficulty=1,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_vim_suid_check.sh"),
    discovery="Spot-check /usr/bin with ls -l, or use find",
    deploy_script="deploy_scripts/linux/linux_vim_suid_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_vim_suid_remediate.sh",
    detection_steps=[
        "Check the SUID bit on the vim/vi binaries: "
        "`ls -l /usr/bin/vi /usr/bin/vim /usr/bin/vim.basic /usr/bin/vim.tiny 2>/dev/null`. "
        "A leading `s` in the owner-execute position (e.g. `-rwsr-xr-x`) confirms SUID is set.",
        "Alternatively use stat to see the raw octal mode: "
        "`stat -L -c '%a %n' /usr/bin/vi /usr/bin/vim 2>/dev/null`. "
        "The `-L` flag dereferences symlinks (vi is often a symlink). "
        "Modes starting with `4` (e.g. `4755`) indicate the SUID bit is present.",
        "Use find to catch any vi/vim variant in the PATH: "
        "`find /usr/bin -name 'vi*' -perm /4000 -ls`. "
        "Any result here means SUID is active.",
    ],
    remediation_steps=[
        "Remove the SUID bit from every vim/vi binary that has it set: "
        '`for b in /usr/bin/vi /usr/bin/vim /usr/bin/vim.basic /usr/bin/vim.tiny; do [ -f "$b" ] && sudo chmod u-s "$b"; done`. '
        "The file-existence check prevents chmod errors on systems where not all variants are installed.",
        "Confirm the bit is cleared: "
        "`ls -l /usr/bin/vi /usr/bin/vim /usr/bin/vim.basic /usr/bin/vim.tiny 2>/dev/null`. "
        "Each existing binary should now show `-rwxr-xr-x` (no `s` in owner-execute position).",
        "Verify the scoring check now returns remediated by re-running the detection find command: "
        "`find /usr/bin -name 'vi*' -perm /4000 -ls`. "
        "No output means the IOC is fully remediated.",
    ],
    artifacts=[
        Artifact(path="/usr/bin/vi", description="vi binary with SUID bit set (u+s)"),
        Artifact(path="/usr/bin/vim", description="vim binary with SUID bit set (u+s)"),
        Artifact(
            path="/usr/bin/vim.basic",
            description="vim.basic variant with SUID bit set (u+s, if present)",
        ),
        Artifact(
            path="/usr/bin/vim.tiny",
            description="vim.tiny variant with SUID bit set (u+s, if present)",
        ),
    ],
)
