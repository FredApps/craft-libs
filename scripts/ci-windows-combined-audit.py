"""Inactive two-producer runtime candidate. Not complete-gate activation permission."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
spec = importlib.util.spec_from_file_location("audit", Path(__file__).with_name("ci-windows-integrated-audit.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
c = audit.contract
FILES = ("configured-metadata.json", "configured-targets.json", "compiler-units.json", "inventory-normalized.json", "executable-digests.json", "parity-manifest.json", "combined-layout.json")
LAYOUT = {"schema": 1, "archive": "combined", "kinds": {k: sorted(v) for k, v in audit.KINDS.items()}, "partitions": 8}


def view(full, kind):
    if kind not in audit.KINDS:
        raise ValueError("unknown logical kind")
    suites = {key: value for key, value in full["rust-suites"].items() if value["kind"] in audit.KINDS[kind]}
    if not suites:
        raise ValueError("missing logical kind")
    return {**full, "rust-suites": suites, "test-count": sum(len(s["testcases"]) for s in suites.values())}


def validate_full(metadata, full, units):
    all_cases = c.cases(full)
    # This uncapped full-runtime candidate has no default-filter exclusions.
    # Do not trust a selected-only census that can silently hide runnable cases.
    for suite in full["rust-suites"].values():
        if suite["status"] != "listed":
            raise ValueError("unlisted full-inventory suite")
        for case in suite["testcases"].values():
            if type(case["ignored"]) is not bool:
                raise ValueError("invalid ignored flag")
            match = case["filter-match"]
            if case["ignored"]:
                if match != {"status": "mismatch", "reason": "ignored"}:
                    raise ValueError("ignored case has unexpected full-list filtering")
            elif match != {"status": "matches"}:
                raise ValueError("nonignored case excluded from full inventory")
    actual = {}
    binary_ids = set()
    for kind in audit.KINDS:
        subset = view(full, kind)
        audit.verify_binaries(metadata, subset, kind, units)
        if binary_ids & subset["rust-suites"].keys():
            raise ValueError("overlapping binary views")
        binary_ids.update(subset["rust-suites"])
        cases = c.cases(subset)
        if actual.keys() & cases.keys():
            raise ValueError("overlapping case views")
        actual.update(cases)
    if binary_ids != full["rust-suites"].keys() or actual != all_cases:
        raise ValueError("logical views are not an exact full inventory union")
    if full["test-count"] != len(all_cases):
        raise ValueError("full case count mismatch")
    return all_cases


def selection(full, listed, kind):
    allowed = c.cases(view(full, kind), selected=True)
    all_cases = c.cases(full)
    selected = {}
    for binary, suite in listed["rust-suites"].items():
        if binary not in full["rust-suites"]:
            raise ValueError("unknown partition binary")
        original = full["rust-suites"][binary]
        if any(suite[key] != original[key] for key in ("package-name", "binary-name", "kind")):
            raise ValueError("partition binary identity differs")
        if suite["status"] == "skipped":
            if suite.get("testcases"):
                raise ValueError("skipped suite has cases")
            continue
        if suite["status"] != "listed":
            raise ValueError("unknown suite status")
        for name, case in suite["testcases"].items():
            key = json.dumps([binary, name])
            if key not in all_cases or case["ignored"] != all_cases[key]:
                raise ValueError("partition case identity differs")
            if case["filter-match"]["status"] == "matches":
                if key not in allowed or case["ignored"]:
                    raise ValueError("wrong-kind or ignored runtime selection")
                selected[key] = False
    return selected


def capture(config):
    if config not in c.CONFIGS:
        raise ValueError("unknown configuration")
    metadata = c.read(c.BUNDLE / "configured-metadata.json")
    full = c.read(c.BUNDLE / "inventory.json")
    units = audit.compiler_units((c.BUNDLE / "compiler-artifacts.jsonl").read_text().splitlines())
    validate_full(metadata, full, units)
    hashes = {}
    for kind in audit.KINDS:
        hashes.update(audit.verify_archive_bytes(metadata, view(full, kind), kind, units, Path(os.environ["RUNNER_TEMP"]) / "pilot-archive-verify"))
    c.write(c.BUNDLE / "compiler-units.json", units)
    c.write(c.BUNDLE / "configured-targets.json", audit.configured_targets(metadata))
    c.write(c.BUNDLE / "executable-digests.json", hashes)
    parity = c.parity.verify_build_snapshot(c.read(c.BUNDLE / "parity-prebuild.json"), c.parity.collect(config))
    c.parity.require_queue_case(view(full, "unit"))
    c.write(c.BUNDLE / "parity-manifest.json", parity)
    c.write(c.BUNDLE / "combined-layout.json", LAYOUT)
    c.prepare(config)  # Common physical archive identity has no logical target kind.


def verify(config):
    c.verify(config)
    if c.read(c.BUNDLE / "combined-layout.json") != LAYOUT:
        raise ValueError("not the expected combined archive layout")


def proof(config, kind, shard):
    if config not in c.CONFIGS or kind not in audit.KINDS or shard not in tuple(map(str, range(1, 9))):
        raise ValueError("invalid logical proof identity")
    full = c.read(c.BUNDLE / "inventory-normalized.json")
    selected = selection(full, c.read("pilot-partition.json"), kind)
    parity = c.parity.validate_manifest(c.read(c.BUNDLE / "parity-manifest.json"))
    root = Path("pilot-proof")
    root.mkdir()
    c.write(root / "proof.json", {"config": config, "target_kind": kind, "shard": shard,
            "sha": os.environ["GITHUB_SHA"], "run": os.environ["GITHUB_RUN_ID"], "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
            "expected": c.cases(view(full, kind), selected=True), "selected": selected,
            "runtime_success": True, "parity_digest": c.parity.manifest_digest(parity)})
    # Each proof retains the common complete inventory, including all ignored cases.
    c.write(root / "audit-digests.json", {name: c.digest(c.BUNDLE / name) for name in FILES})
    for name in FILES:
        shutil.copyfile(c.BUNDLE / name, root / name)


def reconcile():
    needs = json.loads(os.environ["PARITY_NEEDS_JSON"])
    expected_jobs = {phase + "_" + config.replace("-", "_") for config in c.CONFIGS for phase in ("archive", "execute")}
    if set(needs) != expected_jobs or any(needs[name].get("result") != "success" for name in expected_jobs):
        raise ValueError("missing or unsuccessful workflow dependency")
    expected_dirs = {f"proof-{config}-{kind}-{shard}" for config in c.CONFIGS for kind in audit.KINDS for shard in range(1, 9)}
    if {p.name for p in Path("proofs").iterdir()} != expected_dirs:
        raise ValueError("unexpected proof directory inventory")
    provenance = {"sha": os.environ["GITHUB_SHA"], "run": os.environ["GITHUB_RUN_ID"], "attempt": os.environ["GITHUB_RUN_ATTEMPT"]}
    for config in c.CONFIGS:
        baseline = None
        all_proofs, union = [], {}
        full = parity = None
        for kind in audit.KINDS:
            proofs = []
            for shard in range(1, 9):
                root = Path("proofs") / f"proof-{config}-{kind}-{shard}"
                actual = {name: c.digest(root / name) for name in FILES}
                if actual != c.read(root / "audit-digests.json") or baseline is not None and actual != baseline:
                    raise ValueError("logical partitions used different common producer evidence")
                baseline = actual
                if c.read(root / "combined-layout.json") != LAYOUT:
                    raise ValueError("wrong combined layout")
                proofs.append(c.read(root / "proof.json"))
                full = c.read(root / "inventory-normalized.json")
                metadata = c.read(root / "configured-metadata.json")
                validate_full(metadata, full, c.read(root / "compiler-units.json"))
                hashes = c.read(root / "executable-digests.json")
                if set(hashes) != set(full["rust-suites"]) or any(not isinstance(v, str) or len(v) != 64 or any(ch not in "0123456789abcdef" for ch in v) for v in hashes.values()):
                    raise ValueError("incomplete common executable identity map")
                if c.read(root / "configured-targets.json") != audit.configured_targets(metadata):
                    raise ValueError("configured target census differs")
                parity = c.read(root / "parity-manifest.json")
            group = audit.reconcile_group(proofs, config, kind, provenance)
            if group != c.cases(view(full, kind), selected=True) or union.keys() & group.keys():
                raise ValueError("wrong logical runtime union")
            union.update(group)
            all_proofs.extend(proofs)
        if union != c.cases(full, selected=True):
            raise ValueError("combined runtime union lost cases")
        c.parity.reconcile_parity(parity, all_proofs, c.parity.require_queue_case(view(full, "unit")))
        print("EXACT_COMBINED_RUNTIME_UNION", config, len(union), "retained_ignored", sum(c.cases(full).values()))


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "capture": capture(sys.argv[2])
    elif command == "verify": verify(sys.argv[2])
    elif command == "membership":
        chosen = selection(c.read(c.BUNDLE / "inventory-normalized.json"), c.read("pilot-partition.json"), sys.argv[2])
        print("fail" if chosen else "pass")
    elif command == "proof": proof(*sys.argv[2:5])
    elif command == "reconcile": reconcile()
    else: raise SystemExit("unknown command")
