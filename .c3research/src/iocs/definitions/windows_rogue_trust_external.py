from iocs.base import IOC, Artifact, ScriptCheck

windows_rogue_trust_external = IOC(
    name="windows_rogue_trust_external",
    description=(
        "Detects the one-way outbound trust from this team's domain to the "
        "rogue-DC realm mira.corp.sus (Direction=Outbound, TrustType=Uplevel), "
        "hardened to forest-transitive + SID-history-enabled + quarantine-off "
        "by rogue_dc_trust_deploy.py + follow-on netdom flips. This trust is "
        "the identity plumbing that makes mira.corp.sus\\svc_dfsr resolvable "
        "cross-realm. Tearing it down (or setting /quarantine:yes) defangs "
        "every downstream artefact — the FSP, DA-ACE, and BUILTIN\\Administrators "
        "membership stay in AD but no cross-realm auth succeeds so nothing "
        "can be leveraged. Scored as remediated when the trust is missing, "
        "non-forest, or quarantined."
    ),
    category="users",
    difficulty=4,
    os="windows",
    requires_dc=True,
    pre_deployed=True,
    deploy_script=None,
    remediate_script=None,
    check=ScriptCheck(
        script="check_scripts/windows/windows_rogue_trust_external_check.ps1",
    ),
    discovery=(
        "On the DC: `Get-ADTrust -Filter \"Name -eq 'mira.corp.sus'\" "
        "-Properties Direction, ForestTransitive, SIDFilteringQuarantined, TrustAttributes`"
    ),
    detection_steps=[
        (
            "On the DC, dump the trust record: "
            "`Import-Module ActiveDirectory; "
            "Get-ADTrust -Filter \"Name -eq 'mira.corp.sus'\" "
            "-Properties Direction, ForestTransitive, SIDFilteringQuarantined, TrustAttributes "
            "| Format-List Name, Direction, TrustType, ForestTransitive, SIDFilteringQuarantined, TrustAttributes`."
        ),
        (
            "IOC present when the trust exists AND Direction is Outbound (or "
            "BiDirectional) AND ForestTransitive is True AND "
            "SIDFilteringQuarantined is False. Any one of those three flipped "
            "collapses the cross-realm privilege pass-through — the trust "
            "record may still exist but no longer weaponisable."
        ),
        (
            "Cross-check with nltest: `nltest /trusted_domains`. The trust "
            "appears as `mira mira.corp.sus (NT 5) (Direct Outbound)`; the "
            "`Attr:` field reflects the same flags (`forest_transitive`, "
            "`quarantined`)."
        ),
        (
            "TrustAttributes bitmap: 0x00000008 = FOREST_TRANSITIVE, "
            "0x00000040 = TREAT_AS_EXTERNAL, 0x00000004 = QUARANTINED_DOMAIN. "
            "Weaponised state is `72` (0x48 = FOREST_TRANSITIVE | 0x40). "
            "Values that include bit 0x04 are quarantined and score status=0."
        ),
    ],
    remediation_steps=[
        (
            "Full teardown (drops the trust record entirely): "
            "`netdom trust <this-team-fqdn> /Domain:mira.corp.sus /remove /Force`. "
            "Requires Domain Admin. Once removed, no cross-realm auth from "
            "mira.corp.sus succeeds against this domain; svc_dfsr's downstream "
            "FSP / DA-ACE / BUILTIN\\Administrators entries stay in AD but "
            "become unreachable identities."
        ),
        (
            "Softer alternative — flip both hostile flags without dropping the "
            "trust: "
            "`netdom trust <this-team-fqdn> /Domain:mira.corp.sus /Quarantine:yes` then "
            "`netdom trust <this-team-fqdn> /Domain:mira.corp.sus /ForestTRANsitive:no`. "
            "SID history + forest-transitive privilege pass-through both die; "
            "existing service-ticket callers see immediate access-denied on "
            "resources that used them."
        ),
        (
            "Verify: re-run `Get-ADTrust -Filter \"Name -eq 'mira.corp.sus'\" "
            "-Properties ForestTransitive, SIDFilteringQuarantined`. Either the "
            "trust is gone entirely, or ForestTransitive is False and "
            "SIDFilteringQuarantined is True. Test cross-realm auth: "
            "`nxc smb <dc-ip> -u svc_dfsr -p '<pw>' -d mira.corp.sus` should "
            "return STATUS_NO_LOGON_SERVERS or an equivalent auth failure."
        ),
    ],
    artifacts=[
        Artifact(
            path="mira.corp.sus",
            description=(
                "Outbound Uplevel trust from this team's domain to the rogue-DC "
                "realm. Forest-transitive (TrustAttributes bit 0x08) with SID "
                "history enabled and quarantine disabled — the identity bridge "
                "that lets mira.corp.sus\\svc_dfsr authenticate cross-realm to "
                "team boxes."
            ),
        ),
        Artifact(
            path="CN=System,CN=<domainDN>",
            description=(
                "Trust record stored as a `trustedDomain` object under "
                "`CN=System`. Removed by `netdom trust ... /remove`; modified "
                "by `netdom trust ... /Quarantine:yes` and `.../ForestTRANsitive:no`."
            ),
        ),
    ],
)
