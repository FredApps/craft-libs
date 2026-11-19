"""Verify real Cargo/nextest fixture output; run only in scoped hosted diagnostic."""
import importlib.util
import json
from pathlib import Path
import sys
import shutil

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("audit", ROOT / "ci-windows-integrated-audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
config = sys.argv[1]
if config not in ("default", "all-features"):
    raise ValueError("unknown fixture configuration")
metadata = json.loads(Path("configured-metadata.json").read_text())
inventory = json.loads(Path("inventory.json").read_text())
units = audit.compiler_units(Path("compiler-artifacts.jsonl").read_text().splitlines())
digests = audit.verify_archive_bytes(metadata, inventory, "integration", units, Path.cwd() / "extracted")
expected = {
    ("selector-a", "same", "test"),
    ("selector-a", "demo", "example"),
    ("selector-b", "always", "test"),
}
if config == "all-features":
    expected.update({("selector-b", "same", "test"), ("selector-b", "gated", "example")})
actual = {(s["package-name"], s["binary-name"], s["kind"]) for s in inventory["rust-suites"].values()}
if actual != expected or inventory["test-count"] != len(expected):
    raise ValueError(f"wrong fixture inventory: {actual}")
for suite in inventory["rust-suites"].values():
    if suite["status"] != "listed" or len(suite["testcases"]) != 1:
        raise ValueError("missing fixture test")
    for case in suite["testcases"].values():
        if case["ignored"] or case["filter-match"]["status"] != "matches":
            raise ValueError("fixture test would not execute")
print("EXACT_FIXTURE_INVENTORY", config, len(expected), sorted(actual))
print("EXACT_EXECUTABLE_BYTES", config, len(digests))

# Exercise the real consumer byte verifier against the extracted real executables.
# This fixture is not a substitute for eight application runtime shards.
c = audit.contract
c.BUNDLE.mkdir()
c.write(c.BUNDLE / "executable-digests.json", digests)
c.write(c.BUNDLE / "inventory-normalized.json", c.inventory("inventory.json"))
c.write(c.BUNDLE / "configured-metadata.json", metadata)
c.write(c.BUNDLE / "compiler-units.json", units)
c.write(c.BUNDLE / "configured-targets.json", audit.configured_targets(metadata))
shutil.copyfile("inventory.json", "pilot-current.json")
shutil.copyfile("inventory.json", "pilot-partition.json")
c.compare()
c.verify_executables()
binary = Path(next(iter(c.inventory_executable_paths(inventory).values())))
original = binary.read_bytes()
try:
    binary.write_bytes(original + b"tamper fixture")
    try:
        c.verify_executables()
    except ValueError as error:
        if "bytes differ" not in str(error):
            raise
    else:
        raise AssertionError("consumer accepted tampered executable")
finally:
    binary.write_bytes(original)
c.verify_executables()
print("CONSUMER_TAMPER_REJECTED", config)

# Controlled fixture-only parity adapter. Resource files are tiny synthetic data;
# printer prerequisites are mocked. Never application/real-queue evidence.
import os
from unittest.mock import patch
parity = c.parity
resources = {}
for label in ("corpus", "source-corpus", "pdf-fonts", "craft-fonts", "craft-i18n"):
    root = Path("mock-parity-resources") / label
    root.mkdir(parents=True)
    (root / "fixture").write_bytes(label.encode())
    resources[label] = root.resolve()
provenance = {"sha": os.environ["GITHUB_SHA"], "run": os.environ["GITHUB_RUN_ID"],
              "attempt": os.environ["GITHUB_RUN_ATTEMPT"], "config": config}
with patch.object(parity.subprocess, "check_output", return_value="PRINT_PREREQUISITES_OK\n"):
    parity_manifest = parity.manifest(Path.cwd(), Path.cwd() / "mock-cargo-home", resources,
        {"CRAFT_PRINT_REQUIRE_QUEUE": "1", "CRAFT_FONTS_REQUIRED": "1", "CRAFT_I18N_REQUIRED": "1"}, provenance)
c.write(c.BUNDLE / "parity-manifest.json", parity_manifest)
print("MOCK_PARITY_ATTACHED_TO_TINY_PROOF_NO_APPLICATION_QUEUE_EVIDENCE")
