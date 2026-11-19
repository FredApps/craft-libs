"""Fork-only archive provenance and inventory contract. No workflow text execution."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import zipfile
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
        if path.is_file() and path != root / "manifest.json":
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
    # Pack hidden public fixture files too, without enabling broad hidden-file uploads.
    sources = {
        "corpus": Path("corpus"),
        "source-corpus": Path(".test-inputs/photocraft/corpus"),
        "pdf-fonts": Path(os.environ["RUNNER_TEMP"]) / "craft-pdf-test-fonts",
    }
    entries = []
    for label, root in sources.items():
        if not root.is_dir() or root.is_symlink():
            raise ValueError("missing or linked runtime root")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("linked runtime fixture")
            if path.is_file():
                entries.append((path, label + "/" + path.relative_to(root).as_posix()))
    with zipfile.ZipFile(BUNDLE / "runtime.zip", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for path, name in entries:
            archive.write(path, name)
    write(BUNDLE / "inventory-normalized.json", inventory(BUNDLE / "inventory.json"))
    write(BUNDLE / "manifest.json", {"identity": identity(config), "files": files(BUNDLE)})
    print("BUNDLE_BYTES", sum(p.stat().st_size for p in BUNDLE.rglob("*") if p.is_file()))

def verify(config):
    manifest = read(BUNDLE / "manifest.json")
    if manifest["identity"] != identity(config):
        raise ValueError("producer identity differs from this same-run consumer")
    if manifest["files"] != files(BUNDLE):
        raise ValueError("archive payload digest or inventory differs")
    roots = {
        "corpus": Path("corpus"),
        "source-corpus": Path(".test-inputs/photocraft/corpus"),
        "pdf-fonts": Path(os.environ["RUNNER_TEMP"]) / "craft-pdf-test-fonts",
    }
    # No archive-provided path is passed to extractall; validate all members before writing.
    destinations = []
    seen = set()
    with zipfile.ZipFile(BUNDLE / "runtime.zip") as archive:
        for info in archive.infolist():
            name = info.filename
            parts = PurePosixPath(name).parts
            if (name.startswith("/") or "\\" in name or ":" in name or
                    len(parts) < 2 or any(p in (".", "..") for p in name.split("/")) or
                    parts[0] not in roots or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError("unsafe fixture member")
            target = roots[parts[0]].joinpath(*parts[1:])
            target.resolve().relative_to(roots[parts[0]].resolve())
            if any(p.is_symlink() for p in (target, *target.parents)):
                raise ValueError("linked fixture destination")
            key = str(target.resolve()).casefold()
            if key in seen:
                raise ValueError("duplicate fixture destination")
            seen.add(key)
            destinations.append((info, target))
        for info, target in destinations:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
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