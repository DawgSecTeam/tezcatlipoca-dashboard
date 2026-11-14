from iocs.base import IOC, Artifact, ScriptCheck

linux_fake_false = IOC(
    name="linux_fake_false",
    description="Fake /bin/false and /usr/sbin/login redirect to /bin/bash; Coptic unicode disguise",
    category="persistence",
    difficulty=2,
    os="linux",
    check=ScriptCheck(script="check_scripts/linux/linux_fake_false_check.sh"),
    discovery="ls /bin and /usr/sbin",
    deploy_script="deploy_scripts/linux/linux_fake_false_deploy.py",
    remediate_script="remediate_scripts/linux/linux_fake_false_remediate.sh",
    detection_steps=[
        "Scan /etc/passwd for Cyrillic homoglyph characters: "
        "`python3 -c \"data=open('/etc/passwd','r',encoding='utf-8').read(); "
        "print('IOC PRESENT' if ('\\u0435' in data or '\\u0456' in data) else 'CLEAN')\"`"
        " — U+0435 (Cyrillic е) replaces ASCII e in 'false'; U+0456 (Cyrillic і) replaces "
        "ASCII i in 'nologin'.",
        "Dump the raw bytes around any 'fals' or 'nolog' sequences to confirm non-ASCII bytes: "
        "`grep -P 'fals[^e]|nolog[^i]n' /etc/passwd` or "
        "`python3 -c \"[print(repr(l)) for l in open('/etc/passwd') if 'fals' in l or 'nolog' in l]\"`.",
        "Check for the fake binary in /bin — it will appear as 'fals' + a visually identical "
        "but non-ASCII character: `ls -la /bin/ | grep fals` and "
        "`file /bin/fals*` to confirm the homoglyph file is a shell script, not a real ELF.",
        "Check for the fake nologin in /usr/sbin: `ls -la /usr/sbin/ | grep nolog` and "
        "`cat /usr/sbin/nolog*` — the malicious version prints '#!/bin/bash' and spawns bash "
        "instead of exiting.",
        "Verify the impact: any /etc/passwd entry referencing the homoglyph path will give an "
        "interactive bash shell to accounts that should be locked out — check with "
        "`grep 'fals\\|nolog' /etc/passwd | cat -v` to reveal the non-printable codepoints.",
    ],
    remediation_steps=[
        "Replace all Cyrillic homoglyphs in /etc/passwd back to their ASCII equivalents: "
        "`python3 -c \"p='/etc/passwd'; open(p,'w').write(open(p,'r',encoding='utf-8')"
        ".read().replace('\\u0435','e').replace('\\u0456','i'))\"`.",
        "Remove the fake /bin/false lookalike binary (name contains Cyrillic U+0435): "
        "`find /bin -maxdepth 1 -name 'fals*' ! -name 'false' -delete`.",
        "Remove the fake /usr/sbin/nologin lookalike binary (name contains Cyrillic U+0456): "
        "`find /usr/sbin -maxdepth 1 -name 'nolog*' ! -name 'nologin' -delete`.",
        "Confirm /etc/passwd is clean: "
        "`python3 -c \"data=open('/etc/passwd','r',encoding='utf-8').read(); "
        "print('CLEAN' if '\\u0435' not in data and '\\u0456' not in data else 'STILL INFECTED')\"`.",
        "Confirm the real binaries are intact and the fakes are gone: "
        "`ls -la /bin/false /usr/sbin/nologin` (both should be ELF or hardlinks, not shell scripts) "
        "and verify `ls /bin/fals* /usr/sbin/nolog*` only lists the legitimate originals.",
    ],
    artifacts=[
        Artifact(
            path="/etc/passwd",
            description="Shell fields for locked accounts rewritten with homoglyph paths "
            "(/bin/fals\\u0435 and /usr/sbin/nolog\\u0456n) that resolve to a bash-launching wrapper",
        ),
        Artifact(
            dynamic=True,
            description="Fake /bin/false lookalike: filename is 'fals' + Cyrillic е (U+0435), "
            "content is '#!/bin/bash\\n/bin/bash\\n', mode 0755",
        ),
        Artifact(
            dynamic=True,
            description="Fake /usr/sbin/nologin lookalike: filename is 'nolog' + Cyrillic і (U+0456) "
            "+ 'n', content is '#!/bin/bash\\n/bin/bash\\n', mode 0755",
        ),
    ],
)
