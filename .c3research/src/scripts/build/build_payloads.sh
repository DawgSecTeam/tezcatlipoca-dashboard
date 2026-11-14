#!/bin/bash
# Master build script for all source-derived IOC payloads.
# Sliver beacons (beacon_linux, msupdate.exe, beacon_freebsd) and derived
# shellcode (shellcode_win.bin, shellcode_win_lanman.bin) must be generated
# separately via the C3rberus Generation UI or scripts/build/build_shellcode.sh
# after obtaining the beacon EXE from Sliver.
#
# Usage:
#   scripts/build/build_payloads.sh           # build missing payloads only
#   scripts/build/build_payloads.sh --force   # rebuild everything
set -e
FORCE="${1:-}"
ERRORS=0

run() {
    local script="$1"
    echo ">>> $script"
    bash "$script" $FORCE || { echo "[FAIL] $script"; ERRORS=$((ERRORS+1)); }
}

run scripts/build/build_sshd_shim.sh
run scripts/build/build_stc_inject2.sh
run scripts/build/build_linux_ticker.sh
run scripts/build/build_devd_ticker.sh
run scripts/build/build_nginx_shim.sh
run scripts/build/build_linux_kill_shim.sh
run scripts/build/build_freebsd_kill_shim.sh
run scripts/build/build_hide_library_linux.sh
# Note: build_hide_library_bsd.sh requires a FreeBSD 15 sysroot in
# scripts/build/fbsd15-sysroot/ (or /tmp/fbsd15-sysroot). See that script's
# header for one-time setup commands if the sysroot is missing.
run scripts/build/build_hide_library_bsd.sh
run scripts/build/build_hide_library_windows.sh
run scripts/build/build_linux_fake_ntpd.sh

# ELF section embed carrier binaries (linux_elf_section_embed, firewall_elf_section_embed)
run scripts/build/build_elf_section_embed_linux.sh
run scripts/build/build_elf_section_embed_freebsd.sh

# Self-inject dropper shims (PLAN_resilient_implant_iocs.md)
run scripts/build/build_self_inject_windows.sh
run scripts/build/build_self_inject_linux.sh
run scripts/build/build_self_inject_freebsd.sh

# Shared-library (.so) implant loaders + LD_PRELOAD shims
# (PLAN_freebsd_linux_so_implant.md). The FreeBSD tiers need a *dynamic*
# sysroot (scripts/build/provision_fbsd_dynamic_sysroot.sh) and cleanly
# [skip] without it — see each script's header.
run scripts/build/build_so_loader.sh
run scripts/build/build_so_ld_preload_shim.sh

echo ""
if [[ $ERRORS -eq 0 ]]; then
    echo "[ok] All source-derived payloads built."
    echo ""
    echo "Remaining manual steps (require live Sliver C2):"
    echo "  1. Generate beacons via Generation UI → download to iocs/payloads/"
    echo "     - beacon_linux   (linux/amd64 beacon)"
    echo "     - msupdate.exe   (windows/amd64 beacon)"
    echo "     - beacon_freebsd (freebsd/amd64 beacon)"
    echo "  2. scripts/build/build_shellcode.sh iocs/payloads/msupdate.exe"
else
    echo "[FAIL] $ERRORS build(s) failed."
    exit 1
fi
