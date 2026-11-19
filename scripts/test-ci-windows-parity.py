"""Unexecuted source regressions for parity candidate; hosted validation required."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import os
import copy
import subprocess
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("parity", Path(__file__).with_name("ci-windows-parity.py"))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class Tests(unittest.TestCase):
    def test_fixed_env_preserves_absence_without_disclosing_value(self):
        absent = p.env_identity(("RUSTFLAGS",), {})
        empty = p.env_identity(("RUSTFLAGS",), {"RUSTFLAGS": "", "SECRET": "never"})
        private = p.env_identity(("RUSTFLAGS",), {"RUSTFLAGS": "private path"})
        self.assertNotEqual(absent, empty)
        self.assertNotIn("private path", str(private))
        self.assertNotIn("SECRET", empty)

    def test_tree_changes_and_resource_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one").write_bytes(b"first")
            original = p.resource_identity(root)
            (root / "one").write_bytes(b"second")
            self.assertNotEqual(original, p.resource_identity(root))
            with patch.object(p, "MAX_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "budget"):
                    p.resource_identity(root)
            (root / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "administration"):
                p.resource_identity(root)

    def test_required_configuration_and_full_sample_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "sample"):
            p.runtime_inputs({}, {"PROPTEST_CASES": ""})
        with self.assertRaisesRegex(ValueError, "disabled"):
            p.runtime_inputs({}, {})
        with self.assertRaisesRegex(ValueError, "resource"):
            p.runtime_inputs({}, {"CRAFT_PRINT_REQUIRE_QUEUE": "1", "CRAFT_FONTS_REQUIRED": "1"})

    def test_compiler_flags_and_config_are_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Cargo.lock").write_text("lock")
            (root / "Cargo.toml").write_text("manifest")
            before = p.compiler_inputs(root, root / "cargo-home", {})
            after = p.compiler_inputs(root, root / "cargo-home", {"RUSTFLAGS": "--cfg changed"})
            self.assertNotEqual(before, after)
            (root / ".cargo").mkdir()
            (root / ".cargo/config.toml").write_text("config")
            self.assertNotEqual(before, p.compiler_inputs(root, root / "cargo-home", {}))

    def test_queue_case_mandatory_and_not_success_claim(self):
        case = {"ignored": False, "filter-match": {"status": "matches"}}
        suite = {"package-name": "craft-print", "status": "listed", "testcases": {"tests::windows_jobs_reach_the_spooler": case}}
        self.assertTrue(p.require_queue_case({"rust-suites": {"print": suite}}))
        case["ignored"] = True
        with self.assertRaisesRegex(ValueError, "not execute"):
            p.require_queue_case({"rust-suites": {"print": suite}})
        with self.assertRaisesRegex(ValueError, "missing"):
            p.require_queue_case({"rust-suites": {}})

    def test_added_flags_and_unknown_profiles(self):
        self.assertIn("CARGO_ENCODED_RUSTDOCFLAGS", p.FLAGS)
        self.assertIn("CARGO_PROFILE_DEV_BUILD_OVERRIDE_DEBUG_ASSERTIONS", p.PROFILES)
        with self.assertRaisesRegex(ValueError, "unrecognized"):
            p.compiler_inputs(".", ".", {"CARGO_PROFILE_CUSTOM_OPT_LEVEL": "3"})

    def test_printer_failures_timeout_and_wrong_reply(self):
        for error in (subprocess.CalledProcessError(1, "powershell"), subprocess.TimeoutExpired("powershell", 1)):
            with patch.object(p.subprocess, "check_output", side_effect=error):
                with self.assertRaises(type(error)):
                    p.printer_prerequisites()
        with patch.object(p.subprocess, "check_output", return_value="unexpected"):
            with self.assertRaisesRegex(ValueError, "unexpected"):
                p.printer_prerequisites()

    def test_shared_budget_does_not_reset_between_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one").write_bytes(b"abc")
            budget = p.Budget()
            with patch.object(p, "MAX_BYTES", 5):
                p.resource_identity(root, budget)
                with self.assertRaisesRegex(ValueError, "whole parity"):
                    p.resource_identity(root, budget)

    def test_real_link_and_mutation_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            binary = real / "one"
            binary.write_bytes(b"abc")
            link = root / "linked"
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True)
            else:
                link.symlink_to(real, target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError, "linked"):
                    p.resource_identity(link)
            finally:
                os.rmdir(link) if os.name == "nt" else link.unlink()
            original = p.hashlib.sha256
            class Mutating:
                def __init__(self):
                    self.inner = original()
                def update(self, block):
                    self.inner.update(block)
                    with binary.open("ab") as stream:
                        stream.write(b"more")
                def hexdigest(self):
                    return self.inner.hexdigest()
            with patch.object(p.hashlib, "sha256", Mutating):
                with self.assertRaisesRegex(ValueError, "changed"):
                    p.resource_identity(real)

    def test_case_alias_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one").write_bytes(b"abc")
            # Simulate a directory enumeration collision on case-insensitive Windows.
            with patch.object(p.os, "walk", return_value=[(str(root), [], ["one", "one"])]):
                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    p.resource_identity(root)

    def test_real_manifest_pipeline_with_mocked_printer_and_queue_evidence(self):
        # Explicit boundary: no real printer probe, queue or application test runs.
        # Files, resource hashes, schema validator and reconciliation are real.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Cargo.lock").write_text("fixture lock")
            (root / "Cargo.toml").write_text("fixture manifest")
            resources = {}
            for label in ("corpus", "source-corpus", "pdf-fonts", "craft-fonts", "craft-i18n"):
                directory = root / label
                directory.mkdir()
                (directory / "fixture").write_bytes(label.encode())
                resources[label] = directory
            env = {"CRAFT_PRINT_REQUIRE_QUEUE": "1", "CRAFT_FONTS_REQUIRED": "1"}
            provenance = {"sha": "fixture-sha", "run": "fixture-run", "attempt": "1", "config": "default"}
            inventory = {"rust-suites": {"fixture-print": {"package-name": "craft-print", "status": "listed",
                "testcases": {"tests::windows_jobs_reach_the_spooler": {"ignored": False, "filter-match": {"status": "matches"}}}}}}
            queue = p.require_queue_case(inventory)
            with patch.object(p.subprocess, "check_output", return_value="PRINT_PREREQUISITES_OK\n") as probe:
                first = p.manifest(root, root / "isolated-cargo-home", resources, env, provenance)
                second = p.manifest(root, root / "isolated-cargo-home", resources, env, provenance)
                self.assertEqual(first, second)
                self.assertEqual(probe.call_count, 2)
                self.assertEqual(probe.call_args.args[0][0], "powershell.exe")
                self.assertLessEqual(probe.call_args.kwargs["timeout"], 30)
                proof = {**provenance, "runtime_success": True, "selected": {queue: False},
                         "parity_digest": p.manifest_digest(first)}
                p.reconcile_parity(second, [proof], queue)
                (resources["corpus"] / "fixture").write_bytes(b"changed resource")
                changed = p.manifest(root, root / "isolated-cargo-home", resources, env, provenance)
                with self.assertRaisesRegex(ValueError, "manifest"):
                    p.reconcile_parity(changed, [proof], queue)
            print("MOCK_BOUNDARY_PARITY_PIPELINE_VERIFIED_NO_APPLICATION_EXECUTION")

    def complete_manifest(self, provenance):
        h = p.sha(b"fixture")
        return {"schema": 1, "provenance": provenance,
                "compiler": {"inputs": {"workspace-lock": h, "workspace-manifest": h,
                    "cargo-home-config": None, "cargo-home-config.toml": None,
                    "ancestor-0-config": None, "ancestor-0-config.toml": None},
                    "environment": p.env_identity(p.FLAGS + p.PROFILES, {})},
                "runtime": {"environment": p.env_identity(p.SAMPLES + p.RUNTIME,
                    {"CRAFT_PRINT_REQUIRE_QUEUE": "1", "CRAFT_FONTS_REQUIRED": "1"}),
                    "resources": {name: {"files": 1, "bytes": 1, "tree_sha256": h}
                        for name in ("corpus", "source-corpus", "pdf-fonts", "craft-fonts", "craft-i18n")}},
                "printer_prerequisites": {"spooler": True, "generic_driver": True, "lpt1": True}}

    def test_incomplete_and_unknown_schema_evidence_rejected(self):
        provenance = {"sha": "sha", "run": "run", "attempt": "1", "config": "default"}
        good = self.complete_manifest(provenance)
        p.validate_manifest(good)
        bad = [{"schema": 1, "provenance": provenance}]
        for key in ("compiler", "runtime", "printer_prerequisites"):
            item = copy.deepcopy(good)
            del item[key]
            bad.append(item)
        for version in (2, True, "1", None):
            item = copy.deepcopy(good)
            item["schema"] = version
            bad.append(item)
        for section, key in (("compiler", "inputs"), ("compiler", "environment"), ("runtime", "resources"), ("runtime", "environment")):
            item = copy.deepcopy(good)
            item[section][key] = {}
            bad.append(item)
        item = copy.deepcopy(good)
        item["printer_prerequisites"]["spooler"] = False
        bad.append(item)
        for item in bad:
            proof = {**provenance, "runtime_success": True, "parity_digest": p.manifest_digest(item), "selected": {"queue": False}}
            with self.assertRaises(ValueError):
                p.reconcile_parity(item, [proof], "queue")

    def test_manifest_and_successful_queue_bound_to_reconciliation(self):
        provenance = {"sha": "sha", "run": "run", "attempt": "1", "config": "default"}
        manifest = self.complete_manifest(provenance)
        proof = {**provenance, "runtime_success": True, "parity_digest": p.manifest_digest(manifest), "selected": {"queue": False}}
        p.reconcile_parity(manifest, [proof], "queue")
        for field, value in (("parity_digest", "changed"), ("runtime_success", False), ("sha", "different"), ("selected", {})):
            wrong = dict(proof)
            wrong[field] = value
            with self.assertRaises(ValueError):
                p.reconcile_parity(manifest, [wrong], "queue")
        with self.assertRaisesRegex(ValueError, "repeated"):
            p.reconcile_parity(manifest, [proof, proof], "queue")

if __name__ == "__main__":
    unittest.main()
