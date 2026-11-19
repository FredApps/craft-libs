"""Real nextest semantics on dependency-free fixture, never application proof."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

scripts = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("combined_audit", scripts / "ci-windows-integrated-audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
c = audit.contract
config = sys.argv[1]
if config not in c.CONFIGS:
    raise ValueError("unknown fixture configuration")
metadata = c.read("configured-metadata.json")
full = c.read("inventory.json")
units = audit.compiler_units(Path("compiler-artifacts.jsonl").read_text().splitlines())
expected = {("selector-a", "selector_a", "lib"), ("selector-b", "selector_b", "lib"),
            ("selector-macro", "selector_macro", "lib"), ("selector-a", "same", "test"),
            ("selector-a", "demo", "example"), ("selector-b", "always", "test")}
if config == "all-features":
    expected |= {("selector-b", "same", "test"), ("selector-b", "gated", "example")}
actual = {(s["package-name"], s["binary-name"], s["kind"]) for s in full["rust-suites"].values()}
if actual != expected or len(actual) != len(full["rust-suites"]):
    raise ValueError("combined fixture binary inventory mismatch")
full_cases = c.cases(full)
if len(full_cases) != len(expected) + 1 or sum(full_cases.values()) != 1:
    raise ValueError("combined fixture case/ignored inventory mismatch")
views = {}
digests = {}
for kind, kinds in audit.KINDS.items():
    normalized = {"lib" if k == "proc-macro" else k for k in kinds}
    suites = {key: value for key, value in full["rust-suites"].items() if value["kind"] in normalized}
    view = {**full, "rust-suites": suites, "test-count": sum(len(s["testcases"]) for s in suites.values())}
    part = audit.verify_archive_bytes(metadata, view, kind, units, Path.cwd() / "extracted")
    if digests.keys() & part.keys():
        raise ValueError("overlapping executable view")
    digests.update(part)
    views[kind] = c.cases(view)
if views["unit"].keys() & views["integration"].keys() or {**views["unit"], **views["integration"]} != full_cases:
    raise ValueError("combined logical views are not an exact disjoint union")
paths = c.inventory_executable_paths(full)
archive_digest = c.digest(Path("tests.tar.zst"))

def verify_bytes():
    if c.digest(Path("tests.tar.zst")) != archive_digest:
        raise ValueError("immutable combined archive changed")
    if c.executable_hashes(paths, Path.cwd() / "extracted") != digests:
        raise ValueError("combined executable bytes changed")

base = ["--archive-file", "tests.tar.zst", "--extract-to", str(Path.cwd() / "extracted"), "--extract-overwrite"]
proofs = []
# One process per original partition, preserving eight logical identities.
# This is not a proposed application worker count or timing benchmark.
for kind, expression in (("unit", "kind(lib)"), ("integration", "kind(test) | kind(example)")):
    union = {}
    for shard in range(1, 9):
        args = [*base, "-E", expression, "--partition", f"hash:{shard}/8"]
        result = subprocess.run(["cargo", "nextest", "list", *args, "--message-format", "json"], check=True, capture_output=True, text=True, timeout=30)
        listed = json.loads(result.stdout)
        selected = {}
        for binary, suite in listed["rust-suites"].items():
            if binary not in full["rust-suites"]:
                raise ValueError("unknown partition binary")
            original = full["rust-suites"][binary]
            if any(suite[key] != original[key] for key in ("package-name", "binary-name", "kind")):
                raise ValueError("partition binary identity changed")
            if suite["status"] == "skipped":
                if suite.get("testcases"):
                    raise ValueError("skipped binary unexpectedly has cases")
                continue
            if suite["status"] != "listed":
                raise ValueError("unknown suite status")
            for name, case in suite["testcases"].items():
                key = json.dumps([binary, name])
                if key not in full_cases or case["ignored"] != full_cases[key]:
                    raise ValueError("partition case identity changed")
                if case["filter-match"]["status"] == "matches":
                    selected[key] = case["ignored"]
        if not selected.keys() <= views[kind].keys() or union.keys() & selected.keys():
            raise ValueError("partition overlap or wrong logical kind")
        union.update(selected)
        verify_bytes()
        runnable = sum(not ignored for ignored in selected.values())
        # Empty original partitions are expected on a tiny case population.
        env = dict(os.environ, CARGO_TERM_COLOR="never", NO_COLOR="1")
        subprocess.run(["cargo", "nextest", "run", *args, "--no-tests", "fail" if runnable else "pass"], check=True, env=env, timeout=30)
        verify_bytes()
        proofs.append({"kind": kind, "shard": shard, "selected": selected, "runtime_success": True, "archive_digest": archive_digest})
    if union != views[kind]:
        raise ValueError("logical partition union lost cases or ignored semantics")
if len(proofs) != 16 or len({(p["kind"], p["shard"]) for p in proofs}) != 16:
    raise ValueError("missing original partition proofs")
c.write("combined-fixture-proofs.json", {"config": config, "full_cases": full_cases, "proofs": proofs})
print("COMBINED_TINY_EXACT_UNION", config, len(actual), len(full_cases), "ignored", sum(full_cases.values()), "partitions", len(proofs))
print("NO_APPLICATION_OR_PRINTER_EXECUTION_NO_COMPLETE_GATE_OR_TIMING_PROOF")
