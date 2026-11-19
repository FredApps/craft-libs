#!/usr/bin/env bash
# Dependency-free combined archive semantics only; never application validation.
set -euo pipefail
case "${1:-}" in default) features=() ;; all-features) features=(--all-features) ;; *) exit 2 ;; esac
scripts=$(cd "$(dirname "$0")" && pwd)
cd "$scripts/fixtures/combined-selector"
cargo generate-lockfile
cargo metadata --locked --format-version 1 "${features[@]}" > configured-metadata.json
cargo nextest archive --workspace "${features[@]}" --tests --locked --cargo-message-format json --archive-file tests.tar.zst > compiler-artifacts.jsonl
mkdir "$PWD/extracted"
cargo nextest list --archive-file tests.tar.zst --extract-to "$PWD/extracted" --message-format json > inventory.json
python "$scripts/test-ci-windows-combined-fixture.py" "$1"
