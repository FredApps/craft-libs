"""Hosted-only audit regression tests."""
import importlib.util
import json
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location("audit", Path(__file__).with_name("ci-windows-integrated-audit.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

def metadata():
    return {"workspace_members": ["p"], "resolve": {"nodes": [{"id": "p", "features": ["on"]}]},
            "packages": [{"id": "p", "name": "p", "targets": [
                {"name": "p", "kind": ["lib"], "test": True},
                {"name": "yes", "kind": ["test"], "test": True, "required-features": ["on"]},
                {"name": "no", "kind": ["test"], "test": True, "required-features": ["off"]}]}]}

def artifact(fresh=False, features=None):
    return {"reason": "compiler-artifact", "package_id": "p", "target": {"name": "p", "kind": ["lib"]},
            "profile": {"test": True}, "features": features or ["on"], "fresh": fresh,
            "executable": "target/debug/p.exe", "filenames": ["target/debug/p.exe"]}

def stream(*records):
    return [json.dumps(r) for r in records] + [json.dumps({"reason": "build-finished", "success": True})]

class Tests(unittest.TestCase):
    def test_enabled_names_only(self):
        self.assertEqual(audit.selectors(metadata(), "integration"), ["--test", "yes"])
        self.assertEqual(audit.selectors(metadata(), "unit"), ["--lib"])
    def test_disabled_collision(self):
        data = metadata()
        data["packages"][0]["targets"][2]["name"] = "yes"
        with self.assertRaisesRegex(ValueError, "collision"):
            audit.selectors(data, "integration")
    def test_bad_name_and_kind(self):
        for name, kind in [("$(bad)", "test"), ("new", "bin")]:
            data = metadata()
            data["packages"][0]["targets"].append({"name": name, "kind": [kind], "test": True})
            with self.assertRaises(ValueError):
                audit.selectors(data, "integration")
    def test_cached_units_preserved(self):
        units = audit.compiler_units(stream(artifact(True)))
        self.assertEqual(len(units), 1)
        self.assertTrue(next(iter(units.values()))["fresh"])
    def test_missing_failed_or_conflicting_units_rejected(self):
        for lines in [[], [json.dumps(artifact())],
                      [json.dumps({"reason": "build-finished", "success": False})],
                      stream(artifact(), artifact(features=["off"]))]:
            with self.assertRaises(ValueError):
                audit.compiler_units(lines)
    def test_distinct_outputs_are_not_collapsed(self):
        first, second = artifact(), artifact()
        second["executable"] = "target/host/p.exe"
        second["filenames"] = ["target/host/p.exe"]
        self.assertEqual(len(audit.compiler_units(stream(first, second))), 2)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            audit.compiler_units(stream(first, first))

    def test_proof_created_only_after_runtime_and_lockfile(self):
        source = Path(__file__).with_name("ci-windows-archive-consumer.sh").read_text()
        self.assertLess(source.index('cargo nextest run '), source.index('git diff --exit-code'))
        self.assertLess(source.index('git diff --exit-code'), source.index('archive-contract.py proof'))

    def test_actual_features_not_metadata_detect_changes(self):
        left = audit.compiler_units(stream(artifact()))
        self.assertEqual(audit.shared_library_features(left, audit.compiler_units(stream(artifact(True)))), 1)
        with self.assertRaisesRegex(ValueError, "features differ"):
            audit.shared_library_features(left, audit.compiler_units(stream(artifact(features=["on", "other"]))))
    def test_binary_inventory_and_compiler_artifacts_both_required(self):
        inventory = {"rust-suites": {"p": {"package-name": "p", "binary-name": "p", "kind": "lib"}}}
        audit.verify_binaries(metadata(), inventory, "unit", audit.compiler_units(stream(artifact())))
        with self.assertRaisesRegex(ValueError, "artifact"):
            audit.verify_binaries(metadata(), inventory, "unit", {})
        inventory["rust-suites"].clear()
        with self.assertRaisesRegex(ValueError, "inventory"):
            audit.verify_binaries(metadata(), inventory, "unit", audit.compiler_units(stream(artifact())))

    def test_inactive_workflow_requires_all_success_and_same_run_artifacts(self):
        text = Path(__file__).with_name("ci-windows-integrated-workflow.yml").read_text()
        self.assertNotIn("always()", text)
        self.assertNotIn("continue-on-error:", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("run-id:", text.replace("# No run-id override:", "# Same-run download:"))
        expected_needs = []
        for config in ("default", "all-features"):
            for kind in ("unit", "integration"):
                suffix = config.replace("-", "_") + "_" + kind
                producer, consumer = "archive_" + suffix, "execute_" + suffix
                expected_needs.extend([producer, consumer])
                self.assertIn("    needs: " + producer + "\n", text)
                self.assertIn("needs." + producer + ".result == 'success'", text)
                self.assertIn("needs." + consumer + ".result == 'success'", text)
                self.assertIn("command: bash scripts/ci-windows-integrated-producer.sh " + config + " " + kind, text)
                archive = "archive-${{ github.run_id }}-${{ github.run_attempt }}-" + config + "-" + kind
                self.assertIn("artifact-name: " + archive + "\n", text)
                self.assertIn("name: " + archive + "-windows-latest\n", text)
                self.assertIn("name: proof-" + config + "-" + kind + "-${{ matrix.shard }}", text)
        self.assertIn("needs: [" + ", ".join(expected_needs) + "]", text)
        self.assertEqual(text.count("shard: [1, 2, 3, 4, 5, 6, 7, 8]"), 4)

    def test_runtime_union_rejects_duplicates_missing_and_wrong_identity(self):
        provenance = {"sha": "abc", "run": "1", "attempt": "1"}
        expected = {str(i): i == 3 for i in range(8)}
        proofs = [{"config": "default", "target_kind": "unit", "shard": str(i + 1),
                   **provenance, "runtime_success": True, "expected": expected, "selected": {str(i): expected[str(i)]}}
                  for i in range(8)]
        self.assertEqual(audit.reconcile_group(proofs, "default", "unit", provenance), expected)
        with self.assertRaisesRegex(ValueError, "missing"):
            audit.reconcile_group(proofs[:-1], "default", "unit", provenance)
        proofs[0]["runtime_success"] = False
        with self.assertRaisesRegex(ValueError, "runtime"):
            audit.reconcile_group(proofs, "default", "unit", provenance)
        proofs[0]["runtime_success"] = True
        proofs[1]["selected"] = proofs[0]["selected"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            audit.reconcile_group(proofs, "default", "unit", provenance)
        proofs[1]["selected"] = {"1": expected["1"]}
        proofs[0]["target_kind"] = "integration"
        with self.assertRaisesRegex(ValueError, "provenance"):
            audit.reconcile_group(proofs, "default", "unit", provenance)

if __name__ == "__main__":
    unittest.main()
