#!/usr/bin/env bash
set -euo pipefail
# Actual locked CMS package validation, not a workspace/platform acceptance gate.
status=0
cargo fmt -p craft-cms -- --check || status=1
cargo clippy -p craft-cms --all-targets --all-features --locked -- -D warnings || status=1
cargo test -p craft-cms --all-features --locked || status=1
exit "$status"
