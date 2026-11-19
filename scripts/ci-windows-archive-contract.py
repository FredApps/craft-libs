"""Fork-only archive provenance and inventory contract. No workflow text execution."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

BUNDLE = Path("pilot-bundle")
CONFIGS = ("default", "all-features")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def identity(config):
    if config not in CONFIGS or os.environ["RUNNER_OS"] != "Windows":
        raise ValueError("unexpected platform/configuration")
    return {
        "sha": os.environ["GITHUB_SHA"],
        "run": os.environ["GITHUB_RUN_ID"],
        "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "repository": os.environ["GITHUB_REPOSITORY"],
        "config": config,
        "workspace": str(Path.cwd().resolve()),
        "rustc": subprocess.check_output(["rustc", "-vV"], text=True, encoding="utf-8"),
        "nextest": subprocess.check_output(["cargo", "nextest", "--version"], text=True, encoding="utf-8"),
        "debug": os.environ["CARGO_PROFILE_DEV_DEBUG"],
        "profile": "ci",
        "hostile_divisor": os.environ.get("CRAFT_HOSTILE_DIVISOR"),
        "proptest_cases": os.environ.get("PROPTEST_CASES"),
    }


def files(root):
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink in archive payload")
        if path.is_file() and path.name != "manifest.json":
            found[path.relative_to(root).as_posix()] = digest(path)
    if not found:
        raise ValueError("empty bundle")
    return found


def inventory(path):
    data = read(path)
    suites = {}
    for key, suite in data["rust-suites"].items():
        suites[key] = {
            name: suite[name]
            for name in ("package-name", "binary-id", "binary-name", "kind", "build-platform", "status", "testcases")
        }
    if not suites or data["test-count"] <= 0:
        raise ValueError("empty inventory")
    return {"test-count": data["test-count"], "rust-suites": suites}


def cases(data, selected=False):
    result = {}
    for binary, suite in data["rust-suites"].items():
        if suite["status"] != "listed":
            raise ValueError("unlisted binary")
        for name, case in suite["testcases"].items():
            if selected and case["filter-match"]["status"] != "matches":
                continue
            result[json.dumps([binary, name])] = case["ignored"]
    return result


def prepare(config):
    # The upload contains only fixed test artifacts and pinned public fixtures, never credentials.
    shutil.copytree(Path("corpus"), BUNDLE / "corpus")
    shutil.copytree(Path(".test-inputs/photocraft/corpus"), BUNDLE / "source-corpus")
    shutil.copytree(Path(os.environ["RUNNER_TEMP"]) / "craft-pdf-test-fonts", BUNDLE / "pdf-fonts")
    write(BUNDLE / "inventory-normalized.json", inventory(BUNDLE / "inventory.json"))
    write(BUNDLE / "manifest.json", {"identity": identity(config), "files": files(BUNDLE)})
    print("BUNDLE_BYTES", sum(p.stat().st_size for p in BUNDLE.rglob("*") if p.is_file()))


def verify(config):
    manifest = read(BUNDLE / "manifest.json")
    if manifest["identity"] != identity(config):
        raise ValueError("producer identity differs from this same-run consumer")
    if manifest["files"] != files(BUNDLE):
        raise ValueError("archive payload digest or inventory differs")
    # Never extract into arbitrary manifest-supplied paths.
    for source, target in (
        ("corpus", Path("corpus")),
        ("source-corpus", Path(".test-inputs/photocraft/corpus")),
        ("pdf-fonts", Path(os.environ["RUNNER_TEMP"]) / "craft-pdf-test-fonts"),
    ):
        shutil.copytree(BUNDLE / source, target, dirs_exist_ok=True)
    print("PROVENANCE_VERIFIED", config)


def compare():
    if inventory("pilot-current.json") != read(BUNDLE / "inventory-normalized.json"):
        raise ValueError("consumer binary/test/ignored/filter inventory changed")
    print("INVENTORY_IDENTICAL")


def proof(config, shard):
    if config not in CONFIGS or shard not in tuple(map(str, range(1, 9))):
        raise ValueError("invalid proof identity")
    full = read(BUNDLE / "inventory-normalized.json")
    partition = inventory("pilot-partition.json")
    selected = cases(partition, selected=True)
    expected = cases(full, selected=True)
    if not selected or any(key not in expected or expected[key] != ignored for key, ignored in selected.items()):
        raise ValueError("invalid shard membership")
    Path("pilot-proof").mkdir()
    write(Path("pilot-proof") / "proof.json", {
        "config": config, "shard": shard, "sha": os.environ["GITHUB_SHA"],
        "run": os.environ["GITHUB_RUN_ID"], "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "expected": expected, "selected": selected,
    })


def reconcile():
    for config in CONFIGS:
        proofs = [read(Path("proofs") / f"proof-{config}-{shard}" / "proof.json") for shard in range(1, 9)]
        union = {}
        expected = proofs[0]["expected"]
        for shard, item in enumerate(proofs, 1):
            for key, value in (("config", config), ("shard", str(shard)), ("sha", os.environ["GITHUB_SHA"]),
                               ("run", os.environ["GITHUB_RUN_ID"]), ("attempt", os.environ["GITHUB_RUN_ATTEMPT"])):
                if item[key] != value:
                    raise ValueError("wrong shard provenance")
            if item["expected"] != expected:
                raise ValueError("shards came from different inventories")
            if union.keys() & item["selected"].keys():
                raise ValueError("duplicate test across shards")
            union.update(item["selected"])
        if union != expected:
            raise ValueError("missing/changed tests in shard union")
        print("EXACT_PARTITION_UNION", config, len(union), "ignored", sum(union.values()))


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "prepare":
        prepare(sys.argv[2])
    elif command == "verify":
        verify(sys.argv[2])
    elif command == "compare":
        compare()
    elif command == "proof":
        proof(sys.argv[2], sys.argv[3])
    elif command == "reconcile":
        reconcile()
    else:
        raise SystemExit("unknown fixed command")