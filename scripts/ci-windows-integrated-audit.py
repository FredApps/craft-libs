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
    seen = set()
    for package in metadata["packages"]:
        if package["id"] not in members:
            continue
        for target in package["targets"]:
            if not target["test"] or target["kind"] == ["custom-build"]:
                continue
            if len(target["kind"]) != 1 or target["kind"][0] not in set.union(*KINDS.values()):
                raise ValueError("unsupported test target kind")
            key = (package["id"], target["name"], tuple(target["kind"]))
            if key in seen:
                raise ValueError("duplicate target identity collision")
            seen.add(key)
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
    # Cargo's --tests respects test=true and required-features without naming
    # same-name targets across packages. Keep the complete workspace selected:
    # per-package builds could change feature unification. nextest 0.9.150 applies
    # this archive filter AFTER building, so unit binaries may be compiled again
    # but only integration/test-enabled example binaries enter this archive.
    # verify_binaries still requires the exact configured target inventory.
    return ["--tests", "-E", "kind(test) | kind(example)"]


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
        # Retain full Cargo identity, including source, output paths and profile.
        # Cargo does not expose a complete compiler-unit dependency graph here.
        value = {name: record[name] for name in (
            "package_id", "target", "profile", "features", "fresh",
            "executable", "filenames")}
        value["features"] = sorted(value["features"])
        value["filenames"] = sorted(value["filenames"])
        key = json.dumps([value["package_id"], target, value["profile"],
                          value["executable"], value["filenames"]], sort_keys=True)
        if key in units:
            raise ValueError("duplicate or ambiguous compiler artifact identity")
        units[key] = value
    if not finished or not units:
        raise ValueError("missing successful Cargo artifact stream")
    return units


def verify_binaries(metadata, inventory, kind, units):
    """Return unique Cargo artifact matches; byte equality is checked separately."""
    expected = {(t["package"], t["name"], "lib" if t["kind"] == "proc-macro" else t["kind"])
                for t in configured_targets(metadata) if t["kind"] in KINDS[kind]}
    actual = {(s["package-name"], s["binary-name"], s["kind"])
              for s in inventory["rust-suites"].values()}
    if actual != expected or len(actual) != len(inventory["rust-suites"]):
        raise ValueError("configured target inventory differs from archive binaries")
    packages = {p["name"]: p["id"] for p in metadata["packages"] if p["id"] in metadata["workspace_members"]}
    if len(packages) != len(metadata["workspace_members"]):
        raise ValueError("ambiguous workspace package names")
    matched = {}
    for binary_id, suite in inventory["rust-suites"].items():
        package, name, target_kind = suite["package-name"], suite["binary-name"], suite["kind"]
        matching = [u for u in units.values() if u["package_id"] == packages[package]
                    and u["target"]["name"] == name and u["profile"]["test"]
                    and (target_kind in u["target"]["kind"] or
                         target_kind == "lib" and "proc-macro" in u["target"]["kind"])]
        if len(matching) != 1:
            raise ValueError("test binary lacks exactly one actual compiler artifact")
        matched[binary_id] = matching[0]
    return matched


def verify_archive_bytes(metadata, inventory, kind, units, extracted_root):
    matched = verify_binaries(metadata, inventory, kind, units)
    for binary_id, unit in matched.items():
        if inventory["rust-suites"][binary_id]["package-id"] != unit["package_id"]:
            raise ValueError("archive package identity differs from Cargo artifact")
    built = contract.executable_hashes(
        {binary_id: unit["executable"] for binary_id, unit in matched.items()},
        metadata["target_directory"])
    archived = contract.executable_hashes(contract.inventory_executable_paths(inventory), extracted_root)
    if built != archived:
        raise ValueError("archive executable bytes differ from Cargo artifacts")
    return built


def shared_library_features(unit_records, integration_records):
    """Compare feature-set collections on shared labels only, not unit graphs."""
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
    inventory = contract.read("pilot-bundle/inventory.json")
    units = compiler_units(Path("pilot-bundle/compiler-artifacts.jsonl").read_text(encoding="utf-8").splitlines())
    import os
    executable_digests = verify_archive_bytes(metadata, inventory, kind, units,
                                              Path(os.environ["RUNNER_TEMP"]) / "pilot-archive-verify")
    contract.write("pilot-bundle/executable-digests.json", executable_digests)
    contract.write("pilot-bundle/compiler-units.json", units)
    contract.write("pilot-bundle/configured-targets.json", configured_targets(metadata))
    parity_manifest = contract.parity.verify_build_snapshot(
        contract.read("pilot-bundle/parity-prebuild.json"), contract.parity.collect(config))
    if kind == "unit":
        contract.parity.require_queue_case(inventory)
    contract.write("pilot-bundle/parity-manifest.json", parity_manifest)
    contract.prepare(config)
    manifest = contract.read("pilot-bundle/manifest.json")
    manifest["identity"]["target_kind"] = kind
    contract.write("pilot-bundle/manifest.json", manifest)
    print("CARGO_ARTIFACT_RECORDS", config, kind, len(units),
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
        if proof.get("runtime_success") is not True:
            raise ValueError("missing successful runtime completion")
        if proof["expected"] != expected:
            raise ValueError("mixed expected inventories")
        if union.keys() & proof["selected"].keys():
            raise ValueError("duplicate case in runtime partitions")
        union.update(proof["selected"])
    if not expected or union != expected:
        raise ValueError("runtime partition union differs")
    return union


def require_job_success(needs):
    expected = {phase + "_" + config.replace("-", "_") + "_" + kind
                for phase in ("archive", "execute") for config in contract.CONFIGS for kind in KINDS}
    if set(needs) != expected or any(needs[key].get("result") != "success" for key in expected):
        raise ValueError("missing or unsuccessful workflow dependency")


def reconcile():
    import os
    require_job_success(json.loads(os.environ["PARITY_NEEDS_JSON"]))
    provenance = {"sha": os.environ["GITHUB_SHA"], "run": os.environ["GITHUB_RUN_ID"],
                  "attempt": os.environ["GITHUB_RUN_ATTEMPT"]}
    for config in contract.CONFIGS:
        all_cases = {}
        group_audits = {}
        config_proofs = []
        for kind in KINDS:
            roots = [Path("proofs") / f"proof-{config}-{kind}-{shard}" for shard in range(1, 9)]
            proofs = [contract.read(root / "proof.json") for root in roots]
            union = reconcile_group(proofs, config, kind, provenance)
            config_proofs.extend(proofs)
            if all_cases.keys() & union.keys():
                raise ValueError("duplicate case across target kinds")
            all_cases.update(union)
            digests = [contract.read(root / "audit-digests.json") for root in roots]
            if any(digest != digests[0] for digest in digests):
                raise ValueError("shards used different producer audits")
            for filename, key in (("compiler-units.json", "units_digest"),
                                      ("configured-targets.json", "targets_digest"),
                                      ("executable-digests.json", "executables_digest"),
                                      ("parity-manifest.json", "parity_file_digest"),
                                      ("inventory-normalized.json", "inventory_digest")):
                if contract.digest(roots[0] / filename) != digests[0][key]:
                    raise ValueError("producer audit digest differs")
            group_audits[kind] = {
                "digests": digests[0], "units": contract.read(roots[0] / "compiler-units.json"),
                "targets": contract.read(roots[0] / "configured-targets.json"),
                "parity": contract.read(roots[0] / "parity-manifest.json"),
                "inventory": contract.read(roots[0] / "inventory-normalized.json"),
            }
            if union != contract.cases(group_audits[kind]["inventory"], selected=True):
                raise ValueError("runtime union differs from producer inventory")
        unit, integration = group_audits["unit"], group_audits["integration"]
        if (unit["digests"]["metadata_digest"] != integration["digests"]["metadata_digest"] or
                unit["targets"] != integration["targets"]):
            raise ValueError("target kinds used different configured workspace inventories")
        if unit["parity"] != integration["parity"]:
            raise ValueError("target kinds used different parity manifests")
        queue_case = contract.parity.require_queue_case(unit["inventory"])
        # Successful job conclusions and exact case unions were checked above.
        contract.parity.reconcile_parity(unit["parity"], config_proofs, queue_case)
        common = shared_library_features(unit["units"], integration["units"])
        print("EXACT_RUNTIME_UNION", config, len(all_cases), "ignored", sum(all_cases.values()))
        print("SHARED_LIBRARY_LABEL_FEATURE_SETS_IDENTICAL", config, common)

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
