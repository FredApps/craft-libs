#!/usr/bin/env bash
# Compile/list diagnostic only; no runtime/docs/ordinary example-link equivalence claim.
set -euo pipefail
unset CRAFT_HOSTILE_DIVISOR PROPTEST_CASES
case "${1:-}" in
  default) features=(); examples=(--example stubicc) ;;
  all-features) features=(--all-features); examples=(--example stubicc --example h264_h264enc_bench) ;;
  *) exit 2 ;;
esac
case "${2:-}" in
  unit) targets=(--lib) ;;
  integration) targets=(--test '*' "${examples[@]}") ;;
  *) exit 2 ;;
esac
python scripts/test-ci-windows-archive-contract.py
python scripts/ci-windows-target-kind-tests.py
python scripts/ci-windows-target-kind.py "$1" "$2" source
powershell.exe -NoProfile -Command '$ErrorActionPreference = "Stop"; Start-Service Spooler; Add-PrinterDriver -Name "Generic / Text Only"'
mkdir pilot-bundle
printf 'TARGET_ARCHIVE_START %s config=%s kind=%s\n' "$(date -u +%FT%TZ)" "$1" "$2"
start=$SECONDS
cargo nextest archive --workspace "${features[@]}" "${targets[@]}" --locked --archive-file pilot-bundle/tests.tar.zst
printf 'TARGET_ARCHIVE_END %s seconds=%s\n' "$(date -u +%FT%TZ)" "$((SECONDS-start))"
cargo nextest list --archive-file pilot-bundle/tests.tar.zst --profile ci --message-format json > pilot-bundle/inventory.json
powershell.exe -NoProfile -Command '$ErrorActionPreference = "Stop"; $json = & "$env:SystemRoot\System32\tar.exe" -xOf pilot-bundle/tests.tar.zst target/nextest/cargo-metadata.json; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; [IO.File]::WriteAllText((Join-Path (Get-Location) "pilot-bundle/cargo-metadata.json"), ($json -join "`n"), (New-Object Text.UTF8Encoding($false)))'
python scripts/ci-windows-archive-contract.py prepare "$1"
python scripts/ci-windows-target-kind.py "$1" "$2" archive
printf 'TARGET_BUNDLE_END %s seconds_since_start=%s\n' "$(date -u +%FT%TZ)" "$((SECONDS-start))"