"""Source-only parity evidence candidate. Not wired into an active workflow.

Digests, not arbitrary environment/config content, leave the runner. This binds
inputs; it does not prove Cargo unit graphs or dynamic-library closure.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import subprocess
import time

FLAGS = ("CARGO_INCREMENTAL", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTDOCFLAGS", "CARGO_BUILD_TARGET",
         "CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_ENCODED_RUSTDOCFLAGS", "CARGO_BUILD_RUSTFLAGS",
         "CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS")
PROFILES = tuple("CARGO_PROFILE_" + profile + "_" + setting
                 for profile in ("DEV", "TEST", "RELEASE", "DEV_BUILD_OVERRIDE", "TEST_BUILD_OVERRIDE", "RELEASE_BUILD_OVERRIDE")
                 for setting in ("DEBUG", "STRIP", "OPT_LEVEL", "OVERFLOW_CHECKS", "LTO", "CODEGEN_UNITS", "PANIC", "INCREMENTAL", "DEBUG_ASSERTIONS", "RPATH", "SPLIT_DEBUGINFO"))
SAMPLES = ("CRAFT_HOSTILE_DIVISOR", "PROPTEST_CASES", "CRAFT_MEDIA_HOSTILE_MULT")
RUNTIME = ("CRAFT_PRINT_REQUIRE_QUEUE", "CRAFT_FONTS_REQUIRED", "CRAFT_I18N_REQUIRED", "WGPU_BACKEND")
MAX_FILES = 100000
MAX_BYTES = 16 * 1024 ** 3
MAX_SECONDS = 60


class Budget:
    """One shared budget for an entire manifest scan, not one per resource."""
    def __init__(self):
        self.started = time.monotonic()
        self.files = 0
        self.bytes = 0
    def charge(self, files=0, bytes=0):
        self.files += files
        self.bytes += bytes
        if self.files > MAX_FILES or self.bytes > MAX_BYTES or time.monotonic() - self.started > MAX_SECONDS:
            raise ValueError("whole parity scan budget exceeded")
    def remaining(self):
        self.charge()
        return max(0.001, MAX_SECONDS - (time.monotonic() - self.started))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def env_identity(names, env):
    # Hash even fixed-allowlist values: flags/wrappers can contain private paths.
    return {name: None if name not in env else sha(env[name].encode("utf-8")) for name in names}


def no_links(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked parity input")


def resource_identity(root, budget=None, checkout_content=False):
    budget = budget or Budget()
    root = Path(root).absolute()
    no_links(root)
    if not root.is_dir():
        raise ValueError("missing resource root")
    started = time.monotonic()
    entries, total, visited = {}, 0, 0
    # os.walk avoids materializing an unbounded rglob list before enforcing limits.
    def walk_error(error):
        raise error
    for directory, directories, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        budget.charge(files=1 + len(directories) + len(filenames))
        visited += 1 + len(directories) + len(filenames)
        if visited > MAX_FILES or time.monotonic() - started > MAX_SECONDS:
            raise ValueError("resource scan budget exceeded")
        # Resource roots must be content-only, not checkout admin directories.
        if ".git" in directories or ".git" in filenames:
            # Only the known checkout root's directory may be excluded. Files,
            # nested repositories, symlinks and junctions fail closed.
            if checkout_content and Path(directory) == root and ".git" in directories:
                no_links(root / ".git")
                directories.remove(".git")
            else:
                raise ValueError("resource root contains checkout administration")
        for name in directories:
            no_links(Path(directory) / name)
        for name in filenames:
            if time.monotonic() - started > MAX_SECONDS or len(entries) >= MAX_FILES:
                raise ValueError("resource scan budget exceeded")
            path = Path(directory) / name
            no_links(path)
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("nonregular resource")
            budget.charge(bytes=before.st_size)
            total += before.st_size
            if total > MAX_BYTES:
                raise ValueError("resource byte budget exceeded")
            digest, size = hashlib.sha256(), 0
            with path.open("rb") as stream:
                while True:
                    if time.monotonic() - started > MAX_SECONDS:
                        raise ValueError("resource hash time budget exceeded")
                    budget.charge()
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > before.st_size:
                        raise ValueError("resource changed during hashing")
                    digest.update(block)
            after = path.stat()
            if size != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError("resource changed during hashing")
            key = path.relative_to(root).as_posix()
            alias = key.casefold()
            if alias in entries:
                raise ValueError("ambiguous resource path")
            entries[alias] = {"path": key, "sha256": digest.hexdigest(), "bytes": size}
    if not entries:
        raise ValueError("empty resource tree")
    return {"files": len(entries), "bytes": total,
            "tree_sha256": sha(json.dumps(entries, sort_keys=True).encode())}


def file_identity(path, required=False, budget=None):
    budget = budget or Budget()
    budget.charge(files=1)
    path = Path(path).absolute()
    # Inspect existing ancestors even when the optional leaf is absent.
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            no_links(item)
    if not path.exists():
        if required:
            raise ValueError("missing required compiler input")
        return None
    no_links(path)
    if not path.is_file() or path.stat().st_size > 16 * 1024 ** 2:
        raise ValueError("invalid compiler input")
    before = path.stat()
    budget.charge(bytes=before.st_size)
    with path.open("rb") as stream:
        data = stream.read(16 * 1024 ** 2 + 1)
    budget.charge()
    after = path.stat()
    if len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("compiler input changed while hashing")
    return sha(data)


def compiler_inputs(workspace, cargo_home, env, budget=None):
    budget = budget or Budget()
    known = set(FLAGS + PROFILES)
    # Unrecognized profile/target overrides are rejected, not silently omitted.
    if any(name.startswith(("CARGO_PROFILE_", "CARGO_TARGET_", "CARGO_BUILD_")) and
           name not in known and name not in ("CARGO_TARGET_DIR", "CARGO_BUILD_JOBS")
           for name in env):
        raise ValueError("unrecognized compiler environment override")
    workspace = Path(workspace).absolute()
    configs = {"workspace-lock": file_identity(workspace / "Cargo.lock", True, budget),
               "workspace-manifest": file_identity(workspace / "Cargo.toml", True, budget)}
    # Cargo searches ancestors as well as CARGO_HOME. Keep labels path-free.
    for index, root in enumerate((workspace, *workspace.parents)):
        for name in ("config", "config.toml"):
            configs[f"ancestor-{index}-{name}"] = file_identity(root / ".cargo" / name, budget=budget)
    for name in ("config", "config.toml"):
        configs["cargo-home-" + name] = file_identity(Path(cargo_home) / name, budget=budget)
    return {"inputs": configs, "environment": env_identity(FLAGS + PROFILES, env)}


def runtime_inputs(resources, env, budget=None):
    budget = budget or Budget()
    if any(name in env for name in SAMPLES):
        raise ValueError("Full-validation sample override present")
    if env.get("CRAFT_PRINT_REQUIRE_QUEUE") != "1" or env.get("CRAFT_FONTS_REQUIRED") != "1" or env.get("CRAFT_I18N_REQUIRED") != "1":
        raise ValueError("required Windows runtime contract disabled")
    if set(resources) != {"corpus", "source-corpus", "pdf-fonts", "craft-fonts", "craft-i18n"}:
        raise ValueError("missing or unknown runtime resource identity")
    return {"environment": env_identity(SAMPLES + RUNTIME, env),
            "resources": {label: resource_identity(root, budget, checkout_content=label in ("craft-fonts", "craft-i18n")) for label, root in resources.items()}}


def printer_prerequisites(budget=None):
    budget = budget or Budget()
    # Read-only probe. The existing test creates/removes its own paused queue.
    command = "$ErrorActionPreference='Stop'; if ((Get-Service Spooler).Status -ne 'Running') { throw 'Spooler unavailable' }; if (-not (Get-PrinterDriver -Name 'Generic / Text Only')) { throw 'Generic driver unavailable' }; if (-not (Get-PrinterPort -Name 'LPT1:')) { throw 'LPT1 unavailable' }; Write-Output 'PRINT_PREREQUISITES_OK'"
    value = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command", command], text=True, timeout=min(30, budget.remaining()))
    budget.charge()
    if value.strip() != "PRINT_PREREQUISITES_OK":
        raise ValueError("unexpected printer prerequisite evidence")
    return {"spooler": True, "generic_driver": True, "lpt1": True}


def require_queue_case(inventory):
    cases = [(binary, suite, name, case) for binary, suite in inventory["rust-suites"].items()
             if suite["package-name"] == "craft-print"
             for name, case in suite["testcases"].items()
             if name.endswith("::windows_jobs_reach_the_spooler") or name == "windows_jobs_reach_the_spooler"]
    if len(cases) != 1:
        raise ValueError("missing or ambiguous required queue test")
    binary, suite, name, case = cases[0]
    if suite["status"] != "listed" or case["ignored"] or case["filter-match"]["status"] != "matches":
        raise ValueError("required queue test would not execute")
    # Membership is not success: final runtime proof must separately include it.
    return json.dumps([binary, name])


def manifest(workspace, cargo_home, resources, env, provenance):
    budget = Budget()
    if set(provenance) != {"sha", "run", "attempt", "config"} or not all(isinstance(v, str) and v for v in provenance.values()):
        raise ValueError("invalid parity provenance")
    result = {"schema": 1, "provenance": provenance,
              "compiler": compiler_inputs(workspace, cargo_home, env, budget),
              "runtime": runtime_inputs(resources, env, budget),
              "printer_prerequisites": printer_prerequisites(budget)}
    budget.charge()
    return validate_manifest(result)



def validate_manifest(value):
    def exact(mapping, keys, label):
        if not isinstance(mapping, dict) or set(mapping) != set(keys):
            raise ValueError("invalid parity " + label)
    def digest(value, nullable=False):
        if nullable and value is None:
            return
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("invalid parity evidence digest")
    exact(value, ("schema", "provenance", "compiler", "runtime", "printer_prerequisites"), "manifest fields")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("unknown parity schema")
    exact(value["provenance"], ("sha", "run", "attempt", "config"), "provenance")
    provenance = value["provenance"]
    if not all(isinstance(v, str) and v for v in provenance.values()) or provenance["config"] not in ("default", "all-features"):
        raise ValueError("invalid parity provenance")
    exact(value["compiler"], ("inputs", "environment"), "compiler fields")
    exact(value["compiler"]["environment"], FLAGS + PROFILES, "compiler environment")
    for item in value["compiler"]["environment"].values():
        digest(item, nullable=True)
    inputs = value["compiler"]["inputs"]
    required = {"workspace-lock", "workspace-manifest", "cargo-home-config", "cargo-home-config.toml"}
    if not isinstance(inputs, dict) or not required.issubset(inputs):
        raise ValueError("missing parity compiler inputs")
    ancestors = set()
    for name, item in inputs.items():
        if name not in required:
            if not name.startswith("ancestor-"):
                raise ValueError("unknown parity compiler input")
            parts = name.split("-", 2)
            if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("config", "config.toml"):
                raise ValueError("invalid parity ancestor input")
            ancestors.add(int(parts[1]))
        digest(item, nullable=name not in ("workspace-lock", "workspace-manifest"))
    if not ancestors or max(ancestors) > 256 or ancestors != set(range(max(ancestors) + 1)):
        raise ValueError("missing parity ancestor inputs")
    expected_inputs = required | {f"ancestor-{index}-{name}" for index in ancestors for name in ("config", "config.toml")}
    exact(inputs, expected_inputs, "compiler input set")
    exact(value["runtime"], ("environment", "resources"), "runtime fields")
    exact(value["runtime"]["environment"], SAMPLES + RUNTIME, "runtime environment")
    runtime = value["runtime"]["environment"]
    if any(runtime[name] is not None for name in SAMPLES) or any(runtime[name] != sha(b"1") for name in ("CRAFT_PRINT_REQUIRE_QUEUE", "CRAFT_FONTS_REQUIRED", "CRAFT_I18N_REQUIRED")):
        raise ValueError("disabled parity runtime requirements")
    for item in runtime.values():
        digest(item, nullable=True)
    exact(value["runtime"]["resources"], ("corpus", "source-corpus", "pdf-fonts", "craft-fonts", "craft-i18n"), "resource set")
    for item in value["runtime"]["resources"].values():
        exact(item, ("files", "bytes", "tree_sha256"), "resource evidence")
        if type(item["files"]) is not int or not 0 < item["files"] <= MAX_FILES or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= MAX_BYTES:
            raise ValueError("invalid parity resource size")
        digest(item["tree_sha256"])
    exact(value["printer_prerequisites"], ("spooler", "generic_driver", "lpt1"), "printer evidence")
    if any(item is not True for item in value["printer_prerequisites"].values()):
        raise ValueError("missing parity printer prerequisite")
    return value

def manifest_digest(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def reconcile_parity(producer_manifest, proofs, queue_case):
    """Call after exact partition-union/job-status reconciliation, not instead."""
    validate_manifest(producer_manifest)
    expected = manifest_digest(producer_manifest)
    count = 0
    if not proofs or not queue_case:
        raise ValueError("missing parity execution evidence")
    for proof in proofs:
        if proof.get("parity_digest") != expected or proof.get("runtime_success") is not True:
            raise ValueError("runtime parity manifest or success differs")
        if any(proof.get(k) != v for k, v in producer_manifest["provenance"].items()):
            raise ValueError("parity proof provenance differs")
        if queue_case in proof["selected"]:
            if proof["selected"][queue_case] is not False:
                raise ValueError("required queue execution ignored")
            count += 1
    if count != 1:
        raise ValueError("missing or repeated queue execution proof")
    return expected


def collect(config):
    if os.environ.get("RUNNER_OS") != "Windows" or config not in ("default", "all-features"):
        raise ValueError("unexpected parity platform/config")
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve(strict=True)
    if Path.cwd().resolve() != workspace:
        raise ValueError("parity capture outside workspace")
    resources = {"corpus": workspace / "corpus",
                 "source-corpus": workspace / ".test-inputs/photocraft/corpus",
                 "pdf-fonts": Path(os.environ["RUNNER_TEMP"]) / "craft-pdf-test-fonts",
                 "craft-fonts": workspace / "craft-fonts", "craft-i18n": workspace / "craft-i18n"}
    for variable, label in (("CRAFT_PSD_CORPUS", "source-corpus"), ("CRAFT_PDF_TEST_FONT_DIR", "pdf-fonts"),
                            ("CRAFT_FONTS_DIR", "craft-fonts"), ("CRAFT_I18N_DIR", "craft-i18n")):
        if Path(os.environ[variable]).resolve(strict=True) != resources[label].resolve(strict=True):
            raise ValueError("runtime resource path differs from contract")
    provenance = {"sha": os.environ["GITHUB_SHA"], "run": os.environ["GITHUB_RUN_ID"],
                  "attempt": os.environ["GITHUB_RUN_ATTEMPT"], "config": config}
    cargo_home = Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo")))
    return manifest(workspace, cargo_home, resources, os.environ, provenance)



def verify_build_snapshot(before, after):
    validate_manifest(before)
    validate_manifest(after)
    if before != after:
        raise ValueError("producer parity inputs changed across build")
    return after


def verify_current(config):
    expected = json.loads(Path("pilot-bundle/parity-manifest.json").read_text(encoding="utf-8"))
    validate_manifest(expected)
    if collect(config) != expected:
        raise ValueError("consumer compiler/resource/runtime parity differs")
    print("PARITY_INPUTS_IDENTICAL", config)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "prebuild":
        value = collect(sys.argv[2])
        # Exclusive create prevents silently replacing the pre-build snapshot.
        with Path("pilot-bundle/parity-prebuild.json").open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
    elif len(sys.argv) == 3 and sys.argv[1] == "verify":
        verify_current(sys.argv[2])
    else:
        raise SystemExit("unknown fixed parity command")
