"""Default-cred scoring IOCs (one per OS).

These are NOT deployed onto boxes. They are auto-registered as protected
virtual DeployedIOCs (source='virtual') — one per box — by
``orchestrator._seed_default_cred_iocs`` on startup. Each scores whether the
box's OS-default operator account (Administrator/root/pfSense admin) still
authenticates: blue teams earn the full Hard (20pt) value by hardening it.

The check runs no script on the box — ``DefaultCredCheck`` tells the check
pipeline to probe authentication with the box's operator default credential.
They carry no deploy/remediate script (non-deployable), are excluded from the
preflight grid and from "Deploy All IOCs", and cannot be deregistered by an
operator.
"""

from iocs.base import IOC, Artifact, DefaultCredCheck

default_cred_windows = IOC(
    name="default_cred_windows",
    description="Default Windows Administrator credential still valid",
    category="users",
    difficulty=3,
    os="windows",
    check=DefaultCredCheck(),
    discovery="Default local/domain Administrator password unchanged from baseline.",
    detection_steps=[
        "C3rberus scores this IOC by probing SMB authentication with the competition "
        "baseline Administrator credential. To confirm manually: attempt an SMB "
        "connection from your workstation using the baseline password — "
        "`net use \\\\<box_ip>\\IPC$ /user:Administrator <baseline_password>` "
        "(Windows) or `smbclient -U Administrator%<baseline_password> //<box_ip>/IPC$` "
        "(Linux). A successful connection means the password has NOT been changed.",
        "On the Windows box itself, verify the local Administrator account: "
        "`net user Administrator` — the 'Password last set' field shows when (if ever) "
        "the password was rotated.",
        "If domain-joined, check the domain Administrator from the DC: "
        "`Get-ADUser Administrator -Properties PasswordLastSet | "
        "Select-Object Name,PasswordLastSet` — a 'PasswordLastSet' at or before "
        "competition start means the password has not been rotated.",
    ],
    remediation_steps=[
        "Change the local Administrator password from the competition baseline.",
        "If domain-joined, rotate the domain Administrator password.",
        "Confirm the old credential no longer authenticates (e.g. failed SMB logon).",
    ],
    artifacts=[
        Artifact(
            path="Administrator",
            description="Built-in Windows local Administrator account; scored on whether "
            "its password matches the competition baseline credential",
        ),
    ],
)

default_cred_linux = IOC(
    name="default_cred_linux",
    description="Default Linux root credential still valid",
    category="users",
    difficulty=3,
    os="linux",
    check=DefaultCredCheck(),
    discovery="Default root password unchanged from baseline.",
    detection_steps=[
        "C3rberus scores this IOC by probing SSH authentication with the competition "
        "baseline root credential. To confirm manually, attempt an SSH login from "
        "your workstation: "
        "`ssh root@<box_ip>` and enter the baseline password. "
        "A successful login means the password has NOT been changed.",
        "On the Linux box as root, check when the password was last set: "
        "`passwd -S root` — the date field shows the last password-change date. "
        "Alternatively inspect `/etc/shadow`: "
        "`grep '^root:' /etc/shadow` — the third colon-delimited field is the days "
        "since epoch the password was last changed (0 or a very old date = unchanged).",
    ],
    remediation_steps=[
        "Change the root password from the competition baseline (passwd root).",
        "Consider disabling root SSH password auth (PermitRootLogin) once a sudo "
        "user exists.",
        "Confirm the old credential no longer authenticates over SSH.",
    ],
    artifacts=[
        Artifact(
            path="root",
            description="Linux root account; scored on whether its SSH password "
            "matches the competition baseline credential",
        ),
    ],
)

default_cred_firewall = IOC(
    name="default_cred_firewall",
    description="Default pfSense admin credential still valid",
    category="users",
    difficulty=3,
    os="firewall",
    check=DefaultCredCheck(),
    discovery="Default pfSense admin password unchanged from baseline.",
    detection_steps=[
        "C3rberus scores this IOC by probing SSH authentication with the competition "
        "baseline pfSense admin credential. To confirm manually, attempt an SSH login: "
        "`ssh admin@<pfsense_ip>` and enter the baseline password. "
        "A successful login means the password has NOT been changed.",
        "To check via the pfSense web UI: navigate to System > User Manager > Users, "
        "click on the 'admin' user, and review the account — there is no UI indication "
        "of when the password was last changed, so the SSH probe above is the definitive "
        "test.",
    ],
    remediation_steps=[
        "Change the pfSense admin password from the competition baseline.",
        "Confirm the old credential no longer authenticates over SSH.",
    ],
    artifacts=[
        Artifact(
            path="admin",
            description="pfSense built-in admin account; scored on whether its SSH "
            "password matches the competition baseline credential",
        ),
    ],
)
