#!/usr/bin/env bash
# Inactive two-producer source: one authoritative archive per feature mode.
set -euo pipefail
unset CRAFT_HOSTILE_DIVISOR PROPTEST_CASES CRAFT_MEDIA_HOSTILE_MULT
case "${1:-}" in default) features=() ;; all-features) features=(--all-features) ;; *) exit 2 ;; esac
mkdir pilot-bundle
python scripts/test-ci-windows-archive-contract.py
python scripts/test-ci-windows-integrated-audit.py
powershell.exe -NoProfile -Command '$ErrorActionPreference = "Stop"; Start-Service Spooler; Add-PrinterDriver -Name "Generic / Text Only"'
python scripts/ci-windows-parity.py prebuild "$1"
# Unlike nextest's unconditional --all-features metadata, this uses the actual configuration.
cargo metadata --locked --format-version 1 --filter-platform x86_64-pc-windows-msvc "${features[@]}" > pilot-bundle/configured-metadata.json
printf 'PRODUCER_BUILD_START %s config=%s layout=combined\n' "$(date -u +%FT%TZ)" "$1"
# The forwarded stream includes cached compiler-artifact records. No second compile is needed.
cargo nextest archive --workspace "${features[@]}" --tests --locked --cargo-message-format json --archive-file pilot-bundle/tests.tar.zst > pilot-bundle/compiler-artifacts.jsonl
# Keep the independent extraction alive for byte comparison with Cargo's output files.
mkdir "$RUNNER_TEMP/pilot-archive-verify"
cargo nextest list --archive-file pilot-bundle/tests.tar.zst --extract-to "$RUNNER_TEMP/pilot-archive-verify" --profile ci --message-format json > pilot-bundle/inventory.json
python scripts/ci-windows-combined-audit.py capture "$1"
printf 'PRODUCER_END %s\n' "$(date -u +%FT%TZ)"
