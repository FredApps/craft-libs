"""Verify real Cargo/nextest fixture output; run only in scoped hosted diagnostic."""
import importlib.util
import json
from pathlib import Path
import sys

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
audit.verify_binaries(metadata, inventory, "integration", units)
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
