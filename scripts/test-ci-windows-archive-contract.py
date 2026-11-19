"""Hosted-only regression tests for the diagnostic artifact contract."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os

spec = importlib.util.spec_from_file_location("contract", Path(__file__).with_name("ci-windows-archive-contract.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, old)

    def test_hash_and_identity_fail_before_copy(self):
        Path("pilot-bundle").mkdir()
        Path("pilot-bundle/tests.tar.zst").write_bytes(b"original")
        c.write("pilot-bundle/manifest.json", {"identity": {"sha": "a"}, "files": c.files(Path("pilot-bundle"))})
        with patch.object(c, "identity", return_value={"sha": "b"}), patch.object(c.shutil, "copytree") as cp:
            with self.assertRaisesRegex(ValueError, "identity"):
                c.verify("default")
            cp.assert_not_called()
        Path("pilot-bundle/tests.tar.zst").write_bytes(b"tampered")
        with patch.object(c, "identity", return_value={"sha": "a"}), patch.object(c.shutil, "copytree") as cp:
            with self.assertRaisesRegex(ValueError, "digest"):
                c.verify("default")
            cp.assert_not_called()

    def fixtures(self):
        expected = {str(i): bool(i % 2) for i in range(1, 9)}
        for config in c.CONFIGS:
            for shard in range(1, 9):
                root = Path("proofs") / f"proof-{config}-{shard}"
                root.mkdir(parents=True)
                c.write(root / "proof.json", {"config": config, "shard": str(shard), "sha": "sha", "run": "run", "attempt": "1",
                    "expected": expected, "selected": {str(shard): expected[str(shard)]}})
        return Path("proofs/proof-default-8/proof.json")

    def test_exact_union_ignored_duplicates_and_missing(self):
        path = self.fixtures()
        with patch.dict(os.environ, GITHUB_SHA="sha", GITHUB_RUN_ID="run", GITHUB_RUN_ATTEMPT="1"):
            c.reconcile()
            item = c.read(path)
            for selected, error in (({"1": True}, "duplicate"), ({}, "missing"), ({"8": True}, "changed")):
                item["selected"] = selected
                c.write(path, item)
                with self.assertRaisesRegex(ValueError, error):
                    c.reconcile()

if __name__ == "__main__":
    unittest.main()