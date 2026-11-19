#!/usr/bin/env bash
# Diagnostic: full test archive and runtime inputs; no doctest/ordinary example-link claim.
set -euo pipefail
unset CRAFT_HOSTILE_DIVISOR PROPTEST_CASES
python scripts/test-ci-windows-archive-contract.py
case "${1:-}" in
  default) features=() ;;
  all-features) features=(--all-features) ;;
  *) echo 'expected default or all-features' >&2; exit 2 ;;
esac
powershell.exe -NoProfile -Command '$ErrorActionPreference = "Stop"; Start-Service Spooler; Add-PrinterDriver -Name "Generic / Text Only"'
mkdir pilot-bundle
printf 'ARCHIVE_START %s config=%s sha=%s\n' "$(date -u +%FT%TZ)" "$1" "$GITHUB_SHA"
start=$SECONDS
cargo nextest archive --workspace "${features[@]}" --locked --archive-file pilot-bundle/tests.tar.zst
printf 'ARCHIVE_END %s seconds=%s bytes=%s\n' "$(date -u +%FT%TZ)" "$((SECONDS-start))" "$(wc -c < pilot-bundle/tests.tar.zst)"
cargo nextest list --archive-file pilot-bundle/tests.tar.zst --profile ci --message-format json > pilot-bundle/inventory.json
python scripts/ci-windows-archive-inventory.py pilot-bundle/inventory.json
python scripts/ci-windows-archive-contract.py prepare "$1"
printf 'BUNDLE_END %s seconds_since_archive_start=%s\n' "$(date -u +%FT%TZ)" "$((SECONDS-start))"