"""Source-only synthetic pipeline regressions; hosted tiny validation required."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

def module(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

x = module("combined", "ci-windows-combined-audit.py")
h = module("parity_tests", "test-ci-windows-parity.py")
c = x.c


def data():
    targets = [{"name": "print", "kind": ["lib"], "test": True}, {"name": "integration", "kind": ["test"], "test": True}]
    metadata = {"workspace_members": ["p"], "resolve": {"nodes": [{"id": "p", "features": []}]},
                "packages": [{"id": "p", "name": "craft-print", "targets": targets}]}
    units, suites = {}, {}
    for target in targets:
        name, kind = target["name"], target["kind"][0]
        units[name] = {"package_id": "p", "target": target, "profile": {"test": True}}
        case = "windows_jobs_reach_the_spooler" if kind == "lib" else "integration_case"
        cases = {case: {"ignored": False, "filter-match": {"status": "matches"}}}
        if kind == "lib":
            cases["ignored_case"] = {"ignored": True, "filter-match": {"status": "mismatch", "reason": "ignored"}}
        suites[name] = {"package-name": "craft-print", "binary-name": name, "kind": kind, "status": "listed", "testcases": cases}
    return metadata, {"test-count": 3, "rust-suites": suites}, units


class Tests(unittest.TestCase):
    def test_full_views_and_wrong_kind_selection(self):
        metadata, full, units = data()
        x.validate_full(metadata, full, units)
        part = x.view(full, "unit")
        self.assertEqual(len(x.selection(full, part, "unit")), 1)
        with self.assertRaises(ValueError): x.selection(full, part, "integration")
        wrong = copy.deepcopy(part)
        wrong["rust-suites"]["print"]["testcases"]["ignored_case"]["filter-match"] = {"status": "matches"}
        with self.assertRaises(ValueError): x.selection(full, wrong, "unit")
        wrong = copy.deepcopy(full)
        wrong["rust-suites"]["integration"]["kind"] = "unknown"
        with self.assertRaises(ValueError): x.validate_full(metadata, wrong, units)

    def test_full_inventory_cannot_hide_nonignored_or_run_ignored(self):
        metadata, full, units = data()
        for name, match in (("windows_jobs_reach_the_spooler", {"status": "mismatch", "reason": "expression"}),
                            ("ignored_case", {"status": "matches"}),
                            ("ignored_case", {"status": "mismatch", "reason": "expression"})):
            wrong = copy.deepcopy(full)
            wrong["rust-suites"]["print"]["testcases"][name]["filter-match"] = match
            with self.assertRaises(ValueError):
                x.validate_full(metadata, wrong, units)

    def test_synthetic_final_tree(self):
        needs = {p + "_" + cfg.replace("-", "_"): {"result": "success"} for p in ("archive", "execute") for cfg in c.CONFIGS}
        env = {"GITHUB_SHA": "fixture-sha", "GITHUB_RUN_ID": "fixture-run", "GITHUB_RUN_ATTEMPT": "1", "PARITY_NEEDS_JSON": json.dumps(needs)}
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                metadata, full, units = data()
                for cfg in c.CONFIGS:
                    provenance = {"sha": env["GITHUB_SHA"], "run": env["GITHUB_RUN_ID"], "attempt": "1", "config": cfg}
                    parity = h.Tests().complete_manifest(provenance)
                    payloads = {"configured-metadata.json": metadata, "configured-targets.json": x.audit.configured_targets(metadata),
                        "compiler-units.json": units, "inventory-normalized.json": full,
                        "executable-digests.json": {key: "a" * 64 for key in full["rust-suites"]},
                        "parity-manifest.json": parity, "combined-layout.json": x.LAYOUT}
                    for kind in x.audit.KINDS:
                        expected = c.cases(x.view(full, kind), selected=True)
                        for shard in range(1, 9):
                            root = Path("proofs") / f"proof-{cfg}-{kind}-{shard}"
                            root.mkdir(parents=True)
                            for name, value in payloads.items(): c.write(root / name, value)
                            c.write(root / "audit-digests.json", {name: c.digest(root / name) for name in x.FILES})
                            c.write(root / "proof.json", {**provenance, "target_kind": kind, "shard": str(shard), "expected": expected,
                                "selected": expected if shard == 1 else {}, "runtime_success": True, "parity_digest": c.parity.manifest_digest(parity)})
                with patch.dict(os.environ, env):
                    x.reconcile()
                    path = Path("proofs/proof-default-unit-1/proof.json")
                    original = c.read(path)
                    for key, value in (("runtime_success", False), ("selected", {}), ("parity_digest", "bad"), ("target_kind", "integration")):
                        altered = dict(original, **{key: value});c.write(path, altered)
                        with self.assertRaises(ValueError): x.reconcile()
                        c.write(path, original)
                    other = Path("proofs/proof-default-unit-2/proof.json")
                    saved = c.read(other);c.write(other, dict(saved, selected=original["selected"]))
                    with self.assertRaises(ValueError): x.reconcile()
                    c.write(other, saved)
                    with patch.dict(os.environ, {"PARITY_NEEDS_JSON": json.dumps({**needs, "archive_default": {"result": "skipped"}})}):
                        with self.assertRaises(ValueError): x.reconcile()
                    payload = Path("proofs/proof-default-unit-2/combined-layout.json")
                    c.write(payload, {"wrong": "layout"})
                    with self.assertRaises(ValueError): x.reconcile()
                print("SYNTHETIC_COMBINED_RECONCILE_NO_APPLICATION_OR_PRINTER_EXECUTION")
            finally:
                os.chdir(original_cwd)

    def test_source_order_and_mapping(self):
        producer = Path(__file__).with_name("ci-windows-combined-producer.sh").read_text()
        self.assertLess(producer.index(' prebuild '), producer.index('cargo metadata'))
        self.assertLess(producer.index('cargo nextest archive'), producer.index('combined-audit.py capture'))
        self.assertIn('--workspace "${features[@]}" --tests', producer)
        consumer = Path(__file__).with_name("ci-windows-combined-consumer.sh").read_text()
        self.assertIn('kind(lib) | kind(proc-macro)', consumer)
        self.assertIn('--partition "hash:$2/8"', consumer)
        self.assertLess(consumer.index('cargo nextest run'), consumer.index('git diff --exit-code'))
        self.assertLess(consumer.index('git diff --exit-code'), consumer.index('combined-audit.py proof'))
        self.assertEqual(consumer.count('verify-executables'), 2)
        self.assertEqual(consumer.count('parity.py verify'), 2)

if __name__ == "__main__": unittest.main()
