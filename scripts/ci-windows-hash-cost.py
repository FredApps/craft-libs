"""Read-only artifact cost measurement: never executes archived application files.

Existing old-head artifact only; outputs are measurements, not gate/coverage proof.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import stat
import shutil
import subprocess
import tarfile
import time
import zipfile

ROOT = Path(__file__).resolve().parent
SOURCE_SHA = "2f3345d60d0696680a09028805f0a7b2a483e54b"
SOURCE_RUN = "44451212129"
ARTIFACT_ID = 14496542319
ARTIFACT_NAME = "archive-44451212129-1-all-features-integration-windows-latest"
# Cooperative checks cannot interrupt a blocked filesystem/7-Zip pipe read.
# The hosted job's15minute timeout remains the hard end-to-end execution bound.
MAX_SECONDS = 480
MAX_FILES = 100000
MAX_BYTES = 8 * 1024 ** 3
STARTED = time.monotonic()


def check_time():
    if time.monotonic() - STARTED > MAX_SECONDS:
        raise ValueError("hash-cost fixture time budget exceeded")


def load_module(name, filename, expected_sha):
    path = ROOT / filename
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
        raise ValueError("measurement implementation differs from reviewed source")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def relative_name(name):
    if not isinstance(name, str) or not name:
        raise ValueError("unsafe archived path")
    parts = name.removesuffix("/").split("/")
    devices = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    devices.update(prefix + number for prefix in ("COM", "LPT") for number in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³"))
    for part in parts:
        if (not part or part in (".", "..") or part.endswith((".", " ")) or
                any(ord(char) < 32 or char in '<>:"\\|?*' for char in part) or
                part.split(".", 1)[0].rstrip(" .").upper() in devices):
            raise ValueError("Windows-ambiguous archived path")
    return PurePosixPath(*parts)


def no_reparse_ancestors(path):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked extraction path")


def create_root(destination):
    destination = Path(destination).absolute()
    no_reparse_ancestors(destination)
    destination.mkdir()  # Exclusive fresh root; never reuse a previous extraction.
    no_reparse_ancestors(destination)
    return destination.resolve(strict=True)


def check_destination(root, target):
    root = Path(root).absolute()
    target = Path(target).absolute()
    no_reparse_ancestors(root)
    no_reparse_ancestors(target)
    target.resolve(strict=False).relative_to(root.resolve(strict=True))
    if target == root:
        raise ValueError("archive member aliases extraction root")


def extraction_target(root, relative, directory=False):
    # Revalidate raw components for callers other than the archive loop.
    relative = relative_name(relative.as_posix())
    target = root.joinpath(*relative.parts)
    check_destination(root, target)
    parent = root
    for component in relative.parts if directory else relative.parts[:-1]:
        parent = parent / component
        check_destination(root, parent)
        parent.mkdir(exist_ok=True)
        check_destination(root, parent)
        if not parent.is_dir():
            raise ValueError("non-directory extraction parent")
    return target


def nonnegative_size(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid negative archive size")
    return value


def copy_bounded(source, root, target, expected_size):
    nonnegative_size(expected_size)
    check_destination(root, target)
    size = 0
    with target.open("xb") as destination:
        while True:
            check_time()
            block = source.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > expected_size:
                raise ValueError("archive member exceeded declared size")
            destination.write(block)
    if size != expected_size:
        raise ValueError("truncated archive member")


def extract_runtime(archive_path, destination):
    destination = create_root(destination)
    seen, total = set(), 0
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_FILES:
            raise ValueError("too many runtime members")
        for member in members:
            check_time()
            relative = relative_name(member.filename)
            if relative.parts[0] not in ("corpus", "source-corpus", "pdf-fonts"):
                raise ValueError("unknown runtime resource root")
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("linked runtime archive member")
            alias = relative.as_posix().casefold()
            if alias in seen:
                raise ValueError("duplicate runtime archive path")
            seen.add(alias)
            total += nonnegative_size(member.file_size)
            if total > MAX_BYTES:
                raise ValueError("runtime extraction byte budget exceeded")
            target = extraction_target(destination, relative, directory=member.is_dir())
            if not member.is_dir():
                with archive.open(member) as source:
                    copy_bounded(source, destination, target, member.file_size)
    return total


def extract_executables(archive_path, destination, seven_zip, remaining_bytes):
    """7-Zip only decompresses zstd to stdout; Python validates all tar paths."""
    destination = create_root(destination)
    nonnegative_size(remaining_bytes)
    paths, seen, total, count = {}, set(), 0, 0
    process = subprocess.Popen([str(seven_zip), "x", str(archive_path), "-so", "-bd"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                check_time()
                count += 1
                relative = relative_name(member.name)
                alias = relative.as_posix().casefold()
                if count > MAX_FILES or alias in seen:
                    raise ValueError("ambiguous or excessive executable archive inventory")
                seen.add(alias)
                if not (member.isdir() or member.isfile()):
                    raise ValueError("linked or special executable archive member")
                total += nonnegative_size(member.size)
                if total > remaining_bytes:
                    raise ValueError("combined extraction byte budget exceeded")
                if member.isfile() and relative.suffix.lower() == ".exe":
                    target = extraction_target(destination, relative)
                    with archive.extractfile(member) as source:
                        copy_bounded(source, destination, target, member.size)
                    paths[relative.as_posix()] = str(target.resolve())
        process.stdout.close()
        code = process.wait(timeout=max(0.1, MAX_SECONDS - (time.monotonic() - STARTED)))
        if code != 0 or not paths:
            raise ValueError("decompression failed or no executable inventory")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    return paths


def main():
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve(strict=True)
    bundle = workspace / "existing-artifact"
    if shutil.disk_usage(workspace).free < MAX_BYTES + 2 * 1024 ** 3:
        raise ValueError("insufficient disk for bounded extraction plus reserve")
    seven_zip = Path(os.environ["ProgramFiles"]) / "7-Zip/7z.exe"
    if not seven_zip.is_file():
        raise ValueError("hosted official 7-Zip unavailable; no fallback installer")
    parity = load_module("parity_cost", "ci-windows-parity.py", "db4aaf0268069fa774ad8b32e8792b557d2350a8ce7b8d99876e4a6c5fd1b348")
    contract = load_module("contract_cost", "ci-windows-archive-contract.py", "5d8bc9bc75c25d1b4e424d39cdf553ad53c314f3d4d27eb3c9bf2e56374987ab")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    identity = manifest["identity"]
    expected = {"sha": SOURCE_SHA, "run": SOURCE_RUN, "attempt": "1", "repository": "FredApps/craft-libs", "config": "all-features", "target_kind": "integration"}
    if any(identity.get(key) != value for key, value in expected.items()):
        raise ValueError("wrong existing-artifact provenance")
    # Bound payload verification before invoking the reviewed file-hash routine.
    payload_files = list(bundle.iterdir())
    if len(payload_files) > 32 or any(not path.is_file() or path.is_symlink() for path in payload_files):
        raise ValueError("unexpected artifact payload layout")
    if sum(path.stat().st_size for path in payload_files) > 2 * 1024 ** 3:
        raise ValueError("artifact payload byte budget exceeded")
    verify_started = time.monotonic()
    if contract.files(bundle) != manifest["files"]:
        raise ValueError("existing artifact payload digest mismatch")
    payload_seconds = time.monotonic() - verify_started
    check_time()
    extraction_started = time.monotonic()
    runtime_root = workspace / "cost-runtime"
    runtime_bytes = extract_runtime(bundle / "runtime.zip", runtime_root)
    executable_paths = extract_executables(bundle / "tests.tar.zst", workspace / "cost-executables", seven_zip, MAX_BYTES - runtime_bytes)
    extraction_seconds = time.monotonic() - extraction_started
    resources = {name: runtime_root / name for name in ("corpus", "source-corpus", "pdf-fonts")}
    resources["craft-fonts"] = workspace / "craft-fonts"
    try:
        (workspace / "craft-i18n").lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("unexpected i18n resource for source without i18n pin")
    scans = []
    previous = None
    for index in range(2):
        check_time()
        began = time.monotonic()
        budget = parity.Budget()
        identities = {label: parity.resource_identity(root, budget, checkout_content=label in ("craft-fonts", "craft-i18n")) for label, root in resources.items()}
        resource_seconds = time.monotonic() - began
        began = time.monotonic()
        exe_hashes = contract.executable_hashes(executable_paths, workspace / "cost-executables")
        exe_seconds = time.monotonic() - began
        current = {"resources": identities, "executables": exe_hashes}
        if previous is not None and previous != current:
            raise ValueError("benchmark inputs changed between measured passes")
        previous = current
        scans.append({"pass": index + 1, "resource_seconds": resource_seconds, "executable_seconds": exe_seconds,
                      "resource_files": sum(x["files"] for x in identities.values()), "resource_bytes": sum(x["bytes"] for x in identities.values()),
                      "exe_files": len(exe_hashes), "exe_bytes": sum(Path(path).stat().st_size for path in executable_paths.values())})
    report = {"purpose": "read-only older-artifact hash cost; NOT application gate or cold-cache proof",
              "artifact_id": ARTIFACT_ID, "artifact_name": ARTIFACT_NAME, "source_sha": SOURCE_SHA, "source_run": SOURCE_RUN,
              "fixture_sha": os.environ["GITHUB_SHA"], "payload_verify_seconds": payload_seconds,
              "extraction_seconds": extraction_seconds, "runtime_extracted_bytes": runtime_bytes, "scans": scans,
              "script_seconds": time.monotonic() - STARTED, "application_executed": False,
              "i18n": "not-configured: verified absent craft-i18n.rev in exact source tree",
              "cache_note": "both scans follow extraction; neither is cold-filesystem evidence"}
    print(json.dumps(report, sort_keys=True))
    Path("hash-cost-report.json").write_text(json.dumps(report, sort_keys=True, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
