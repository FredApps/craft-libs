#!/usr/bin/env bash
set -euo pipefail
# Fork-only exploratory validation; not a workspace or consumer acceptance gate.
probe="$(mktemp -d)"
cp -R crates/plotter/src "$probe/src"
if [ -d crates/plotter/tests ]; then cp -R crates/plotter/tests "$probe/tests"; fi
cp rustfmt.toml "$probe/rustfmt.toml"
# Preserve the repository policy that permits unwrap/panic in tests, not production.
cp clippy.toml "$probe/clippy.toml"
cat > "$probe/Cargo.toml" <<'TOML'
[package]
name = "craft-plotter"
version = "0.0.0"
edition = "2024"
rust-version = "1.95"
[dependencies]
kurbo = "0.13"
thiserror = "2"
serde = { version = "1", features = ["derive"], optional = true }
[features]
serde = ["dep:serde"]
[workspace]
TOML
# Ensure the probe executes the exact PR source, not a modified stand-in.
diff -r crates/plotter/src "$probe/src"
status=0
cargo fmt --manifest-path "$probe/Cargo.toml" -- --check || status=1
cargo clippy --manifest-path "$probe/Cargo.toml" --all-targets --all-features -- -D warnings || status=1
cargo test --manifest-path "$probe/Cargo.toml" || status=1
cargo test --manifest-path "$probe/Cargo.toml" --all-features || status=1
exit "$status"
