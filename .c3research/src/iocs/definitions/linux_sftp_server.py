from iocs.base import IOC, Artifact, ScriptCheck

linux_sftp_server = IOC(
    name="linux_sftp_server",
    description="SFTP server enabled and running on Linux",
    category="exfiltration",
    difficulty=1,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_sftp_server_check.sh"),
    discovery="Check sshd_config for Subsystem sftp; check port 22",
    deploy_script="deploy_scripts/linux/linux_sftp_server_deploy.sh",
    remediate_script="remediate_scripts/linux/linux_sftp_server_remediate.sh",
    detection_steps=[
        "Check for the SFTP drop-in config file: "
        "`ls -la /etc/ssh/sshd_config.d/99-sftp.conf`.",
        "Confirm the file contains a Subsystem directive: "
        "`grep -E '^Subsystem.*sftp' /etc/ssh/sshd_config.d/99-sftp.conf`.",
        "Verify sshd is active (Debian/Ubuntu use the 'ssh' unit; RHEL/CentOS use 'sshd'): "
        "`systemctl is-active ssh 2>/dev/null || systemctl is-active sshd`.",
        "Confirm the effective sshd config includes SFTP (picks up all drop-ins): "
        "`sudo sshd -T | grep -i subsystem`.",
    ],
    remediation_steps=[
        "Remove the malicious drop-in file: `sudo rm -f /etc/ssh/sshd_config.d/99-sftp.conf`.",
        "Reload sshd to apply the removal (Debian uses 'ssh', RHEL uses 'sshd'): "
        "`sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd`.",
        "If reload fails, restart instead: "
        "`sudo systemctl restart ssh 2>/dev/null || sudo systemctl restart sshd`.",
        "Verify the Subsystem sftp line is gone from the running config: "
        "`sudo sshd -T | grep -i subsystem` — output should be empty or absent.",
    ],
    artifacts=[
        Artifact(
            path="/etc/ssh/sshd_config.d/99-sftp.conf",
            description="Drop-in config enabling the SFTP subsystem; contains 'Subsystem sftp <binary>'",
        ),
        Artifact(
            path="/etc/ssh/sshd_config.d/",
            description="Drop-in directory created by deploy if it did not already exist",
        ),
    ],
)
