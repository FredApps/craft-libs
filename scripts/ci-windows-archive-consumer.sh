#!/usr/bin/env bash
# Diagnostic runtime only. Does not replace docs, ordinary example linking, lint or other OSes.
set -euo pipefail
unset CRAFT_HOSTILE_DIVISOR PROPTEST_CASES CRAFT_MEDIA_HOSTILE_MULT
case "${1:-}" in default|all-features) ;; *) exit 2 ;; esac
case "${2:-}" in [1-8]) ;; *) exit 2 ;; esac
printf 'CONSUMER_START %s config=%s shard=%s\n' "$(date -u +%FT%TZ)" "$1" "$2"
case "${3:-}" in unit|integration) ;; *) exit 2 ;; esac
python scripts/ci-windows-archive-contract.py verify "$1" "$3"
powershell.exe -NoProfile -Command '$ErrorActionPreference = "Stop"; Start-Service Spooler; Add-PrinterDriver -Name "Generic / Text Only"'
export CRAFT_PSD_CORPUS="$GITHUB_WORKSPACE/.test-inputs/photocraft/corpus"
export CRAFT_PDF_TEST_FONT_DIR="$RUNNER_TEMP/craft-pdf-test-fonts"
# Extract into the original workspace root: integration tests embed target binary absolute paths.
args=(--archive-file pilot-bundle/tests.tar.zst --extract-to "$GITHUB_WORKSPACE" --extract-overwrite --profile ci)
cargo nextest list "${args[@]}" --message-format json > pilot-current.json
python scripts/ci-windows-archive-contract.py compare
cargo nextest list "${args[@]}" --partition "hash:$2/8" --message-format json > pilot-partition.json
printf 'RUNTIME_START %s\n' "$(date -u +%FT%TZ)"
cargo nextest run "${args[@]}" --partition "hash:$2/8"
git diff --exit-code -- Cargo.lock
python scripts/ci-windows-archive-contract.py proof "$1" "$2" "$3"
printf 'RUNTIME_END %s\n' "$(date -u +%FT%TZ)"