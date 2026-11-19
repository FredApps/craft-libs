#!/usr/bin/env bash
# Independent, dependency-free selector contract. No application build or production gate.
set -euo pipefail
case "${1:-}" in default) features=() ;; all-features) features=(--all-features) ;; *) exit 2 ;; esac
scripts=$(cd "$(dirname "$0")" && pwd)
python "$scripts/test-ci-windows-integrated-audit.py"
python "$scripts/test-ci-windows-archive-contract.py"
# Parity probe/queue boundaries are mocked; no application or printer execution.
python "$scripts/test-ci-windows-parity.py"
cd "$scripts/fixtures/archive-selector"
cargo generate-lockfile
cargo metadata --locked --format-version 1 "${features[@]}" > configured-metadata.json
python "$scripts/ci-windows-integrated-audit.py" selectors configured-metadata.json integration > selectors.txt
mapfile -t selection < selectors.txt
cargo nextest archive --workspace "${features[@]}" "${selection[@]}" --locked --cargo-message-format json --archive-file tests.tar.zst > compiler-artifacts.jsonl
mkdir "$PWD/extracted"
cargo nextest list --archive-file tests.tar.zst --extract-to "$PWD/extracted" --message-format json > inventory.json
python "$scripts/test-ci-windows-selector-fixture.py" "$1"
# Mirror consumer extraction semantics, then verify the executed bytes again.
cargo nextest run --archive-file tests.tar.zst --extract-to "$PWD/extracted" --extract-overwrite
python "$scripts/ci-windows-archive-contract.py" verify-executables
python "$scripts/ci-windows-archive-contract.py" proof "$1" 1 integration
python "$scripts/test-ci-windows-selector-proof.py" "$1"
