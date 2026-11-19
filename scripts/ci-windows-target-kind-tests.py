"""Hosted-only guards for the target-kind audit."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("audit", Path(__file__).with_name("ci-windows-target-kind.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class Tests(unittest.TestCase):
    def test_target_sets_are_disjoint_and_exhaustive(self):
        suites = {kind: {"kind": kind, "testcases": {"case": {"ignored": kind == "example"}}}
                  for kind in ("lib", "test", "example")}
        data = {"test-count": 3, "rust-suites": suites}
        left = audit.expected_inventory(data, "unit")
        right = audit.expected_inventory(data, "integration")
        self.assertFalse(left["rust-suites"].keys() & right["rust-suites"].keys())
        self.assertEqual(left["rust-suites"] | right["rust-suites"], suites)
        self.assertEqual(left["test-count"] + right["test-count"], 3)
        self.assertTrue(right["rust-suites"]["example"]["testcases"]["case"]["ignored"])

    def test_unknown_target_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unaccounted"):
            audit.expected_inventory({"rust-suites": {"bin": {"kind": "bin"}}}, "unit")

    def test_feature_and_edge_changes_are_detected(self):
        node = {"id": "p", "features": ["a"], "dependencies": ["q"], "deps": [{"name": "q", "pkg": "q", "dep_kinds": []}]}
        old = audit.graph({"nodes": [node]})
        self.assertNotEqual(old, audit.graph({"nodes": [node | {"features": ["a", "b"]}]}))
        self.assertNotEqual(old, audit.graph({"nodes": [node | {"dependencies": []}]}))


if __name__ == "__main__":
    unittest.main()