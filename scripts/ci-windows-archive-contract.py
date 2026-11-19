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
import time

BUNDLE = Path("pilot-bundle")
CONFIGS = ("default", "all-features")
MAX_EXECUTABLES = 2048
MAX_EXECUTABLE_BYTES = 16 * 1024 ** 3
MAX_HASH_SECONDS = 60


def executable_hashes(paths, root):
    """Hash bounded regular executable files within one explicit native-platform root."""
    if not paths or len(paths) > MAX_EXECUTABLES:
        raise ValueError("empty or excessive executable inventory")
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("executable root is not a directory")
    started = time.monotonic()
    total = 0
    seen = set()
    result = {}
    for binary_id, name in sorted(paths.items()):
        if not isinstance(binary_id, str) or not binary_id or not isinstance(name, str):
            raise ValueError("invalid executable identity/path")
        path = Path(name)
        if not path.is_absolute():
            raise ValueError("executable path is not absolute")
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        key = os.path.normcase(str(resolved))
        if key in seen:
            raise ValueError("ambiguous shared executable path")
        seen.add(key)
        # Reject every Windows reparse point as well as POSIX symlinks. Do not
        # silently permit junctions on Python versions without Path.is_junction.
        if any(p.is_symlink() or
               getattr(p.lstat(), "st_file_attributes", 0) &
               getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
               for p in (path, *path.parents)):
            raise ValueError("linked executable path")
        before = resolved.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise ValueError("missing or nonregular executable")
        total += before.st_size
        if total > MAX_EXECUTABLE_BYTES:
            raise ValueError("executable hash byte budget exceeded")
        h = hashlib.sha256()
        read_bytes = 0
        with resolved.open("rb") as stream:
            while True:
                if time.monotonic() - started > MAX_HASH_SECONDS:
                    raise ValueError("executable hash time budget exceeded")
                block = stream.read(1024 * 1024)
                if not block:
                    break
                read_bytes += len(block)
                if read_bytes > before.st_size:
                    raise ValueError("executable changed while hashing")
                h.update(block)
        after = resolved.stat()
        if (read_bytes != before.st_size or
                (before.st_size, before.st_mtime_ns, before.st_ino) !=
                (after.st_size, after.st_mtime_ns, after.st_ino)):
            raise ValueError("executable changed while hashing")
        result[binary_id] = h.hexdigest()
    return result


def inventory_executable_paths(data):
    paths = {}
    for key, suite in data["rust-suites"].items():
        binary_id = suite["binary-id"]
        if key != binary_id or binary_id in paths or suite["status"] != "listed":
            raise ValueError("ambiguous or unlisted binary identity")
        paths[binary_id] = suite["binary-path"]
    return paths


def verify_executables():
    actual = executable_hashes(inventory_executable_paths(read("pilot-current.json")),
                               os.environ["GITHUB_WORKSPACE"])
    if actual != read(BUNDLE / "executable-digests.json"):
        raise ValueError("extracted executable bytes differ from Cargo artifacts")
    print("EXECUTABLE_BYTES_VERIFIED", len(actual))


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
            if path.is_file() or path.is_dir():
                entries.append((path, label + "/" + path.relative_to(root).as_posix()))
    with zipfile.ZipFile(BUNDLE / "runtime.zip", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for path, name in entries:
            archive.write(path, name)
    write(BUNDLE / "inventory-normalized.json", inventory(BUNDLE / "inventory.json"))
    write(BUNDLE / "manifest.json", {"identity": identity(config), "files": files(BUNDLE)})
    print("BUNDLE_BYTES", sum(p.stat().st_size for p in BUNDLE.rglob("*") if p.is_file()))

def verify(config, target_kind=None):
    manifest = read(BUNDLE / "manifest.json")
    expected_identity = identity(config)
    if target_kind is not None:
        if target_kind not in ("unit", "integration"):
            raise ValueError("invalid target kind")
        expected_identity["target_kind"] = target_kind
    if manifest["identity"] != expected_identity:
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
            member = name[:-1] if info.is_dir() else name
            parts = PurePosixPath(member).parts
            if (name.startswith("/") or "\\" in name or ":" in name or
                    len(parts) < 2 or any(p in (".", "..") for p in member.split("/")) or
                    parts[0] not in roots or stat.S_ISLNK(info.external_attr >> 16)):
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
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
    print("PROVENANCE_VERIFIED", config)

def compare():
    if inventory("pilot-current.json") != read(BUNDLE / "inventory-normalized.json"):
        raise ValueError("consumer binary/test/ignored/filter inventory changed")
    print("INVENTORY_IDENTICAL")


def proof(config, shard, target_kind=None):
    if config not in CONFIGS or shard not in tuple(map(str, range(1, 9))):
        raise ValueError("invalid proof identity")
    full = read(BUNDLE / "inventory-normalized.json")
    partition = inventory("pilot-partition.json")
    selected = cases(partition, selected=True)
    expected = cases(full, selected=True)
    if not selected or any(key not in expected or expected[key] != ignored for key, ignored in selected.items()):
        raise ValueError("invalid shard membership")
    if target_kind is not None and target_kind not in ("unit", "integration"):
        raise ValueError("invalid proof target kind")
    Path("pilot-proof").mkdir()
    write(Path("pilot-proof") / "proof.json", {
        "config": config, "shard": shard, "sha": os.environ["GITHUB_SHA"],
        "run": os.environ["GITHUB_RUN_ID"], "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "expected": expected, "selected": selected,
        "target_kind": target_kind,
        "runtime_success": True,  # Caller must invoke only after successful runtime and lockfile check.
    })


    if target_kind is not None:
        audit = {"metadata_digest": digest(BUNDLE / "configured-metadata.json"),
                 "units_digest": digest(BUNDLE / "compiler-units.json"),
                 "targets_digest": digest(BUNDLE / "configured-targets.json"),
                 "executables_digest": digest(BUNDLE / "executable-digests.json")}
        write(Path("pilot-proof") / "audit-digests.json", audit)
        if shard == "1":
            for name in ("compiler-units.json", "configured-targets.json", "executable-digests.json"):
                shutil.copyfile(BUNDLE / name, Path("pilot-proof") / name)


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
        verify(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif command == "compare":
        compare()
    elif command == "verify-executables":
        verify_executables()
    elif command == "proof":
        proof(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    elif command == "reconcile":
        reconcile()
    else:
        raise SystemExit("unknown fixed command")
