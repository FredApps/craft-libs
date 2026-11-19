"""Compare target-kind archives with the verified whole-workspace baseline."""
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys

spec = importlib.util.spec_from_file_location("contract", Path(__file__).with_name("ci-windows-archive-contract.py"))
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
KINDS = {"unit": {"lib"}, "integration": {"test", "example"}}
BASE_SHA = "f0041dc2db5bc8e4e3148a14d56ef874fc41fc06"


def source_tree():
    lines = subprocess.check_output(["git", "ls-tree", "-r", "--full-tree", "HEAD"], text=True, encoding="utf-8").splitlines()
    result = []
    for line in lines:
        path = line.split("\t", 1)[1]
        if (path.startswith(".github/") or path.startswith("scripts/ci-windows-archive-") or
                path == "scripts/test-ci-windows-archive-contract.py" or
                path.startswith("scripts/ci-windows-target-kind") or path.startswith("scripts/ci-archive-baseline/")):
            continue
        result.append(line)
    return result


def verify_environment(current, expected):
    for key in ("rustc", "nextest", "debug", "profile", "hostile_divisor", "proptest_cases", "workspace", "repository", "config"):
        if current[key] != expected[key]:
            raise ValueError("baseline environment differs: " + key)

def baseline(config):
    if config not in ("default", "all-features"):
        raise ValueError("invalid feature configuration")
    data = contract.read(Path("scripts/ci-archive-baseline") / (config + ".json"))
    if data["provenance"]["sha"] != BASE_SHA or data["provenance"]["run"] != "44435916108":
        raise ValueError("wrong baseline provenance")
    capture = data["artifact_capture"]
    expected_id = 14489606674 if config == "default" else 14490376996
    if (capture["repository"] != "FredApps/craft-libs" or capture["run_id"] != "44435916108" or
            capture["attempt"] != "1" or capture["artifact_id"] != expected_id or
            capture["artifact_name"] != "archive-44435916108-1-" + config + "-windows-latest" or
            capture["metadata_member"] != "target/nextest/cargo-metadata.json"):
        raise ValueError("wrong artifact capture")
    inventory_bytes = (json.dumps(data["inventory"], sort_keys=True) + "\n").encode("utf-8")
    if hashlib.sha256(inventory_bytes).hexdigest() != capture["inventory_sha256"]:
        raise ValueError("baseline inventory differs from captured artifact digest")    return data


def expected_inventory(full, kind):
    if kind not in KINDS:
        raise ValueError("invalid target kind")
    if any(suite["kind"] not in {"lib", "test", "example"} for suite in full["rust-suites"].values()):
        raise ValueError("unaccounted target kind")
    suites = {key: value for key, value in full["rust-suites"].items() if value["kind"] in KINDS[kind]}
    if not suites:
        raise ValueError("empty target group")
    return {"test-count": sum(len(s["testcases"]) for s in suites.values()), "rust-suites": suites}


def graph(resolve):
    # Preserve resolved features and dependency edges, including dependency kind/platform.
    return {
        node["id"]: {
            "features": sorted(node["features"]),
            "dependencies": sorted(node["dependencies"]),
            "deps": sorted(json.dumps(dep, sort_keys=True) for dep in node["deps"]),
        }
        for node in resolve["nodes"]
    }


def validate(config, kind, phase):
    data = baseline(config)
    verify_environment(contract.identity(config), data["provenance"])
    if source_tree() != data["source_tree"]:
        raise ValueError("product source differs from baseline")
    expected = expected_inventory(data["inventory"], kind)
    if phase == "source":
        print("BASELINE_SOURCE_IDENTICAL", config, kind, expected["test-count"])
        return
    if phase != "archive":
        raise ValueError("invalid validation phase")
    actual = contract.inventory("pilot-bundle/inventory.json")
    if actual != expected:
        raise ValueError("target-kind binary/test/ignored/filter inventory differs")
    metadata = contract.read("pilot-bundle/cargo-metadata.json")
    if graph(metadata["resolve"]) != graph(data["resolve"]):
        raise ValueError("resolved dependency/feature graph differs")
    if sorted(metadata["workspace_members"]) != sorted(data["workspace_members"]):
        raise ValueError("workspace membership differs")
    print("EXACT_TARGET_SUBSET", config, kind, actual["test-count"])
    print("RESOLVED_FEATURE_GRAPH_IDENTICAL", len(metadata["resolve"]["nodes"]))
    # This is resolved-graph evidence, not a claim about every Cargo compiler unit or runtime.
    manifest = contract.read("pilot-bundle/manifest.json")
    manifest["identity"]["target_kind"] = kind
    contract.write("pilot-bundle/target-kind-proof.json", {
        "baseline_sha": BASE_SHA, "config": config, "kind": kind,
        "test_count": actual["test-count"], "resolved_graph_nodes": len(metadata["resolve"]["nodes"]),
    })
    manifest["files"] = contract.files(Path("pilot-bundle"))
    contract.write("pilot-bundle/manifest.json", manifest)


if __name__ == "__main__":
    validate(sys.argv[1], sys.argv[2], sys.argv[3])