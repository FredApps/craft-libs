#!/usr/bin/env bash
# Diagnostic only: compile/list the full test archive; do not claim runtime/doc/example validation.
set -euo pipefail
unset CRAFT_HOSTILE_DIVISOR PROPTEST_CASES
case "${1:-}" in
  default) features=() ;;
  all-features) features=(--all-features) ;;
  *) echo 'expected default or all-features' >&2; exit 2 ;;
esac
powershell.exe -NoProfile -Command '$ErrorActionPreference = "Stop"; Start-Service Spooler; Add-PrinterDriver -Name "Generic / Text Only"'
archive="$RUNNER_TEMP/nextest-$1.tar.zst"
inventory="$RUNNER_TEMP/nextest-$1.json"
printf 'ARCHIVE_START %s config=%s sha=%s\n' "$(date -u +%FT%TZ)" "$1" "$GITHUB_SHA"
start=$SECONDS
cargo nextest archive --workspace "${features[@]}" --locked --archive-file "$archive"
printf 'ARCHIVE_END %s seconds=%s bytes=%s\n' "$(date -u +%FT%TZ)" "$((SECONDS-start))" "$(wc -c < "$archive")"
cargo nextest list --archive-file "$archive" --message-format json > "$inventory"
python scripts/ci-windows-archive-inventory.py "$inventory"
printf 'INVENTORY_END %s seconds_since_archive_start=%s\n' "$(date -u +%FT%TZ)" "$((SECONDS-start))"
