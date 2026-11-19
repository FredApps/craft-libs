"""Integrated archive audit: configured targets and actual Cargo artifact features.

Configured metadata selects targets; it is not a compiler-unit graph. Compiler
artifact records (including fresh/cached ones) prove features for emitted units.
Build-script output and dependency-edge equivalence require separate evidence.
"""
import importlib.util
import json
from pathlib import Path
import re
import sys

spec = importlib.util.spec_from_file_location("contract", Path(__file__).with_name("ci-windows-archive-contract.py"))
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
KINDS = {"unit": {"lib", "proc-macro"}, "integration": {"test", "example"}}


def configured_targets(metadata):
    members = set(metadata["workspace_members"])
    features = {node["id"]: set(node["features"]) for node in metadata["resolve"]["nodes"]}
    targets = []
    for package in metadata["packages"]:
        if package["id"] not in members:
            continue
        for target in package["targets"]:
            if not target["test"] or target["kind"] == ["custom-build"]:
                continue
            if len(target["kind"]) != 1 or target["kind"][0] not in set.union(*KINDS.values()):
                raise ValueError("unsupported test target kind")
            required = set(target.get("required-features", []))
            if not required.issubset(features[package["id"]]):
                continue
            targets.append({
                "package": package["name"], "package_id": package["id"],
                "name": target["name"], "kind": target["kind"][0],
                "required_features": sorted(required),
            })
    if not targets:
        raise ValueError("empty configured target inventory")
    return targets


def selectors(metadata, kind):
    if kind not in KINDS:
        raise ValueError("invalid target kind")
    eligible = configured_targets(metadata)
    names = {}
    for target in eligible:
        if target["kind"] not in KINDS[kind]:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]+", target["name"]):
            raise ValueError("unsafe target name")
        names.setdefault(target["kind"], set()).add(target["name"])
    if not names:
        raise ValueError("empty target kind")
    if kind == "unit":
        return ["--lib"]
    # Cargo name selectors apply across all selected workspace packages. A disabled
    # same-name target must fail rather than silently enabling its features.
    members = set(metadata["workspace_members"])
    eligible_keys = {(t["package_id"], t["name"], t["kind"]) for t in eligible}
    for package in metadata["packages"]:
        if package["id"] not in members:
            continue
        for target in package["targets"]:
            for target_kind in KINDS[kind]:
                if target_kind in target["kind"] and target["name"] in names.get(target_kind, set()):
                    if (package["id"], target["name"], target_kind) not in eligible_keys:
                        raise ValueError("disabled target name collision")
    return [arg for target_kind in ("test", "example")
            for name in sorted(names.get(target_kind, set()))
            for arg in ("--" + target_kind, name)]


def compiler_units(lines):
    units = {}
    finished = False
    for line in lines:
        if not line.startswith("{"):
            continue
        record = json.loads(line)
        if record.get("reason") == "build-finished":
            if not record["success"]:
                raise ValueError("Cargo build failed")
            finished = True
        if record.get("reason") != "compiler-artifact":
            continue
        target = record["target"]
        # Keep profile, host/custom-build kind and test mode distinct. Do not drop
        # fresh=True records: cached compilation units must remain in the audit.
        key = json.dumps([record["package_id"], target["name"], target["kind"],
                          record["profile"]], sort_keys=True)
        value = {"features": sorted(record["features"]), "target": target,
                 "package_id": record["package_id"], "profile": record["profile"],
                 "fresh": record["fresh"]}
        if key in units and units[key]["features"] != value["features"]:
            raise ValueError("conflicting artifact features for one unit")
        units[key] = value
    if not finished or not units:
        raise ValueError("missing successful Cargo artifact stream")
    return units


def verify_binaries(metadata, inventory, kind, units):
    expected = {(t["package"], t["name"], "lib" if t["kind"] == "proc-macro" else t["kind"])
                for t in configured_targets(metadata) if t["kind"] in KINDS[kind]}
    actual = {(s["package-name"], s["binary-name"], s["kind"])
              for s in inventory["rust-suites"].values()}
    if actual != expected:
        raise ValueError("configured target inventory differs from archive binaries")
    packages = {p["name"]: p["id"] for p in metadata["packages"] if p["id"] in metadata["workspace_members"]}
    for package, name, target_kind in actual:
        matching = [u for u in units.values() if u["package_id"] == packages[package]
                    and u["target"]["name"] == name and u["profile"]["test"]
                    and (target_kind in u["target"]["kind"] or
                         target_kind == "lib" and "proc-macro" in u["target"]["kind"])]
        if len(matching) != 1:
            raise ValueError("test binary lacks exactly one actual compiler artifact")


def shared_library_features(unit_records, integration_records):
    def libraries(records):
        result = {}
        for unit in records.values():
            if unit["target"]["kind"] not in (["lib"], ["proc-macro"]):
                continue
            key = json.dumps([unit["package_id"], unit["target"]["name"], unit["target"]["kind"]])
            result.setdefault(key, set()).add(tuple(unit["features"]))
        return result
    left, right = libraries(unit_records), libraries(integration_records)
    common = left.keys() & right.keys()
    if not common:
        raise ValueError("no shared compiled libraries")
    for key in common:
        if left[key] != right[key]:
            raise ValueError("actual compiled library features differ across target kinds")
    return len(common)


def capture(config, kind):
    if config not in contract.CONFIGS or kind not in KINDS:
        raise ValueError("invalid producer identity")
    metadata = contract.read("pilot-bundle/configured-metadata.json")
    inventory = contract.inventory("pilot-bundle/inventory.json")
    units = compiler_units(Path("pilot-bundle/compiler-artifacts.jsonl").read_text(encoding="utf-8").splitlines())
    verify_binaries(metadata, inventory, kind, units)
    contract.write("pilot-bundle/compiler-units.json", units)
    contract.write("pilot-bundle/configured-targets.json", configured_targets(metadata))
    contract.prepare(config)
    manifest = contract.read("pilot-bundle/manifest.json")
    manifest["identity"]["target_kind"] = kind
    contract.write("pilot-bundle/manifest.json", manifest)
    print("ACTUAL_COMPILER_UNITS", config, kind, len(units),
          "cached", sum(u["fresh"] for u in units.values()))



def reconcile_group(proofs, config, kind, provenance):
    if len(proofs) != 8:
        raise ValueError("missing shard proof")
    expected = proofs[0]["expected"]
    union = {}
    for shard, proof in enumerate(proofs, 1):
        identity = {"config": config, "target_kind": kind, "shard": str(shard), **provenance}
        if any(proof.get(key) != value for key, value in identity.items()):
            raise ValueError("wrong shard provenance")
        if proof["expected"] != expected:
            raise ValueError("mixed expected inventories")
        if union.keys() & proof["selected"].keys():
            raise ValueError("duplicate case in runtime partitions")
        union.update(proof["selected"])
    if not expected or union != expected:
        raise ValueError("runtime partition union differs")
    return union


def reconcile():
    import os
    provenance = {"sha": os.environ["GITHUB_SHA"], "run": os.environ["GITHUB_RUN_ID"],
                  "attempt": os.environ["GITHUB_RUN_ATTEMPT"]}
    for config in contract.CONFIGS:
        all_cases = {}
        group_audits = {}
        for kind in KINDS:
            roots = [Path("proofs") / f"proof-{config}-{kind}-{shard}" for shard in range(1, 9)]
            proofs = [contract.read(root / "proof.json") for root in roots]
            union = reconcile_group(proofs, config, kind, provenance)
            if all_cases.keys() & union.keys():
                raise ValueError("duplicate case across target kinds")
            all_cases.update(union)
            digests = [contract.read(root / "audit-digests.json") for root in roots]
            if any(digest != digests[0] for digest in digests):
                raise ValueError("shards used different producer audits")
            for filename, key in (("compiler-units.json", "units_digest"),
                                  ("configured-targets.json", "targets_digest")):
                if contract.digest(roots[0] / filename) != digests[0][key]:
                    raise ValueError("producer audit digest differs")
            group_audits[kind] = {
                "digests": digests[0], "units": contract.read(roots[0] / "compiler-units.json"),
                "targets": contract.read(roots[0] / "configured-targets.json"),
            }
        unit, integration = group_audits["unit"], group_audits["integration"]
        if (unit["digests"]["metadata_digest"] != integration["digests"]["metadata_digest"] or
                unit["targets"] != integration["targets"]):
            raise ValueError("target kinds used different configured workspace inventories")
        common = shared_library_features(unit["units"], integration["units"])
        print("EXACT_RUNTIME_UNION", config, len(all_cases), "ignored", sum(all_cases.values()))
        print("ACTUAL_SHARED_LIBRARY_FEATURES_IDENTICAL", config, common)

if __name__ == "__main__":
    if sys.argv[1] == "selectors":
        sys.stdout.reconfigure(newline="\n")
        print("\n".join(selectors(contract.read(sys.argv[2]), sys.argv[3])))
    elif sys.argv[1] == "capture":
        capture(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "reconcile":
        reconcile()
    else:
        raise SystemExit("unknown fixed command")
