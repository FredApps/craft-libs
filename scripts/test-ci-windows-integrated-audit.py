"""Hosted-only audit regression tests."""
import importlib.util
import json
from pathlib import Path
import unittest
import tempfile
import os
from unittest.mock import patch
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
    def test_proc_macro_kind_is_not_flattened_to_lib(self):
        data = metadata()
        data["packages"][0]["targets"][0]["kind"] = ["proc-macro"]
        item = artifact()
        item["target"]["kind"] = ["proc-macro"]
        units = audit.compiler_units(stream(item))
        suite = {"package-name": "p", "binary-name": "p", "kind": "proc-macro"}
        inventory = {"rust-suites": {"p": suite}}
        audit.verify_binaries(data, inventory, "unit", units)
        suite["kind"] = "lib"
        with self.assertRaisesRegex(ValueError, "inventory"):
            audit.verify_binaries(data, inventory, "unit", units)

    def test_archive_bytes_are_bound_to_unique_compiler_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            built, extracted = root / "built", root / "extracted"
            built.mkdir()
            extracted.mkdir()
            source, archived = built / "p.exe", extracted / "p.exe"
            source.write_bytes(b"actual compiled executable")
            archived.write_bytes(source.read_bytes())
            data = metadata()
            data["target_directory"] = str(built)
            item = artifact()
            item["executable"] = str(source)
            item["filenames"] = [str(source)]
            units = audit.compiler_units(stream(item))
            inventory = {"rust-suites": {"p": {"package-name": "p", "package-id": "p",
                "binary-id": "p", "binary-name": "p", "kind": "lib", "status": "listed",
                "binary-path": str(archived)}}}
            self.assertEqual(audit.verify_archive_bytes(data, inventory, "unit", units, extracted),
                             {"p": audit.contract.digest(source)})
            inventory["rust-suites"]["p"]["package-id"] = "wrong"
            with self.assertRaisesRegex(ValueError, "package identity"):
                audit.verify_archive_bytes(data, inventory, "unit", units, extracted)
            inventory["rust-suites"]["p"]["package-id"] = "p"
            archived.write_bytes(b"different compiled executable")
            with self.assertRaisesRegex(ValueError, "bytes differ"):
                audit.verify_archive_bytes(data, inventory, "unit", units, extracted)

    def test_integration_builds_all_tests_then_filters_archive(self):
        self.assertEqual(audit.selectors(metadata(), "integration"), ["--tests", "-E", "kind(test) | kind(example)"])
        self.assertEqual(audit.selectors(metadata(), "unit"), ["--lib"])
    def test_disabled_collision(self):
        data = metadata()
        data["packages"][0]["targets"][2]["name"] = "yes"
        with self.assertRaisesRegex(ValueError, "collision"):
            audit.selectors(data, "integration")
    def test_cross_package_disabled_collision(self):
        data = metadata()
        data["workspace_members"].append("q")
        data["resolve"]["nodes"].append({"id": "q", "features": []})
        data["packages"].append({"id": "q", "name": "q", "targets": [
            {"name": "yes", "kind": ["test"], "test": True, "required-features": ["off"]}]})
        self.assertEqual(audit.selectors(data, "integration"),
                         ["--tests", "-E", "kind(test) | kind(example)"])
        self.assertEqual([(t["package"], t["name"]) for t in audit.configured_targets(data)
                          if t["kind"] == "test"], [("p", "yes")])

    def test_disabled_example_collision_preserves_exact_eligible_inventory(self):
        data = metadata()
        data["packages"][0]["targets"].append(
            {"name": "demo", "kind": ["example"], "test": True})
        data["workspace_members"].append("q")
        data["resolve"]["nodes"].append({"id": "q", "features": []})
        data["packages"].append({"id": "q", "name": "q", "targets": [
            {"name": "demo", "kind": ["example"], "test": False}]})
        self.assertEqual(audit.selectors(data, "integration"),
                         ["--tests", "-E", "kind(test) | kind(example)"])
        expected = {(t["package"], t["name"]) for t in audit.configured_targets(data)
                    if t["kind"] == "example"}
        self.assertEqual(expected, {("p", "demo")})

    def test_integration_rejects_extra_unit_or_disabled_binary(self):
        data = metadata()
        unit = artifact()
        enabled = artifact()
        enabled["target"] = {"name": "yes", "kind": ["test"]}
        enabled["executable"] = "target/debug/yes.exe"
        enabled["filenames"] = [enabled["executable"]]
        units = audit.compiler_units(stream(unit, enabled))
        inventory = {"rust-suites": {"p::yes": {
            "package-name": "p", "binary-name": "yes", "kind": "test"}}}
        audit.verify_binaries(data, inventory, "integration", units)
        for binary, kind in [("p", "lib"), ("no", "test")]:
            inventory["rust-suites"]["extra"] = {
                "package-name": "p", "binary-name": binary, "kind": kind}
            with self.assertRaisesRegex(ValueError, "inventory"):
                audit.verify_binaries(data, inventory, "integration", units)

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
        self.assertLess(source.index('archive-contract.py verify-executables'), source.index('cargo nextest run '))
        self.assertLess(source.index('cargo nextest run '), source.rindex('archive-contract.py verify-executables'))
        self.assertLess(source.rindex('archive-contract.py verify-executables'), source.index('archive-contract.py proof'))

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

    def test_producer_parity_brackets_metadata_and_build(self):
        source = Path(__file__).with_name("ci-windows-integrated-producer.sh").read_text()
        self.assertLess(source.index('parity.py prebuild'), source.index('cargo metadata'))
        self.assertLess(source.index('parity.py prebuild'), source.index('cargo nextest archive'))
        self.assertLess(source.index('cargo nextest archive'), source.index('integrated-audit.py capture'))

    def test_full_synthetic_reconcile_tree(self):
        # Synthetic files and mocked job-result boundary only. No application runs.
        helper_spec = importlib.util.spec_from_file_location("parity_test_helpers", Path(__file__).with_name("test-ci-windows-parity.py"))
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        c = audit.contract
        needs = {phase + "_" + config.replace("-", "_") + "_" + kind: {"result": "success"}
                 for phase in ("archive", "execute") for config in c.CONFIGS for kind in audit.KINDS}
        env = {"GITHUB_SHA": "fixture-sha", "GITHUB_RUN_ID": "fixture-run", "GITHUB_RUN_ATTEMPT": "1",
               "PARITY_NEEDS_JSON": json.dumps(needs)}
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                for config in c.CONFIGS:
                    provenance = {"sha": env["GITHUB_SHA"], "run": env["GITHUB_RUN_ID"], "attempt": "1", "config": config}
                    parity = helper.Tests().complete_manifest(provenance)
                    for kind in audit.KINDS:
                        binary = "fixture-" + kind
                        names = ["tests::windows_jobs_reach_the_spooler" if kind == "unit" and i == 0 else kind + "_case_" + str(i) for i in range(8)]
                        inventory = {"test-count": 8, "rust-suites": {binary: {"package-name": "craft-print" if kind == "unit" else "fixture",
                            "status": "listed", "testcases": {name: {"ignored": False, "filter-match": {"status": "matches"}} for name in names}}}}
                        expected = c.cases(inventory, selected=True)
                        roots = [Path("proofs") / f"proof-{config}-{kind}-{i}" for i in range(1, 9)]
                        for root in roots:
                            root.mkdir(parents=True)
                        payloads = {"compiler-units.json": audit.compiler_units(stream(artifact())),
                                    "configured-targets.json": audit.configured_targets(metadata()),
                                    "executable-digests.json": {binary: c.parity.sha(b"fixture exe")},
                                    "parity-manifest.json": parity, "inventory-normalized.json": inventory}
                        for filename, value in payloads.items():
                            c.write(roots[0] / filename, value)
                        digests = {"metadata_digest": c.parity.sha(b"same metadata")}
                        for filename, key in (("compiler-units.json", "units_digest"), ("configured-targets.json", "targets_digest"),
                                              ("executable-digests.json", "executables_digest"), ("parity-manifest.json", "parity_file_digest"),
                                              ("inventory-normalized.json", "inventory_digest")):
                            digests[key] = c.digest(roots[0] / filename)
                        for index, root in enumerate(roots):
                            c.write(root / "audit-digests.json", digests)
                            key = json.dumps([binary, names[index]])
                            c.write(root / "proof.json", {**provenance, "target_kind": kind, "shard": str(index + 1),
                                "expected": expected, "selected": {key: False}, "runtime_success": True,
                                "parity_digest": c.parity.manifest_digest(parity)})
                with patch.dict(os.environ, env):
                    audit.reconcile()
                    proof_path = Path("proofs/proof-default-unit-1/proof.json")
                    original = c.read(proof_path)
                    for key, value in (("runtime_success", False), ("parity_digest", "wrong"), ("selected", {})):
                        changed = dict(original)
                        changed[key] = value
                        c.write(proof_path, changed)
                        with self.assertRaises(ValueError):
                            audit.reconcile()
                        c.write(proof_path, original)
                    proof_path.unlink()
                    with self.assertRaises(FileNotFoundError):
                        audit.reconcile()
                    c.write(proof_path, original)
                    evidence_path = Path("proofs/proof-default-unit-1/parity-manifest.json")
                    saved = evidence_path.read_bytes()
                    evidence_path.write_bytes(b"{}")
                    with self.assertRaisesRegex(ValueError, "digest"):
                        audit.reconcile()
                    evidence_path.write_bytes(saved)
                    needs["execute_default_unit"]["result"] = "cancelled"
                    with patch.dict(os.environ, PARITY_NEEDS_JSON=json.dumps(needs)):
                        with self.assertRaisesRegex(ValueError, "unsuccessful"):
                            audit.reconcile()
            finally:
                os.chdir(original_cwd)
        print("SYNTHETIC_FINAL_RECONCILE_TREE_VERIFIED_NO_APPLICATION_EXECUTION")

    def test_workflow_dependencies_require_all_actual_successes(self):
        needs = {phase + "_" + config.replace("-", "_") + "_" + kind: {"result": "success"}
                 for phase in ("archive", "execute") for config in ("default", "all-features") for kind in ("unit", "integration")}
        audit.require_job_success(needs)
        key = next(iter(needs))
        for result in ("failure", "cancelled", "skipped", ""):
            needs[key]["result"] = result
            with self.assertRaisesRegex(ValueError, "unsuccessful"):
                audit.require_job_success(needs)
        del needs[key]
        with self.assertRaisesRegex(ValueError, "missing"):
            audit.require_job_success(needs)

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
