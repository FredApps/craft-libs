"""Hosted-only regression tests for the diagnostic artifact contract."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os
import subprocess

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

    def test_executable_paths_and_bytes_are_bounded(self):
        root = Path("executables").resolve()
        root.mkdir()
        binary = root / "one.exe"
        binary.write_bytes(b"same executable")
        actual = c.executable_hashes({"one": str(binary)}, root)
        self.assertEqual(actual, {"one": c.digest(binary)})
        with self.assertRaisesRegex(ValueError, "absolute"):
            c.executable_hashes({"one": "one.exe"}, root)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            c.executable_hashes({"one": str(binary), "two": str(binary)}, root)
        outside = Path("outside.exe").resolve()
        outside.write_bytes(b"outside")
        with self.assertRaises(ValueError):
            c.executable_hashes({"one": str(outside)}, root)
        with self.assertRaises(FileNotFoundError):
            c.executable_hashes({"one": str(root / "missing.exe")}, root)
        with patch.object(c, "MAX_EXECUTABLE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "byte budget"):
                c.executable_hashes({"one": str(binary)}, root)
        with patch.object(c.time, "monotonic", side_effect=[0, 61]):
            with self.assertRaisesRegex(ValueError, "time budget"):
                c.executable_hashes({"one": str(binary)}, root)
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ValueError, "linked"):
                c.executable_hashes({"one": str(binary)}, root)

    def test_real_linked_executable_directory_rejected(self):
        root = Path("executables").resolve()
        real = root / "real"
        real.mkdir(parents=True)
        (real / "one.exe").write_bytes(b"executable")
        link = root / "linked"
        if os.name == "nt":
            # Junctions need no symlink privilege on hosted Windows.
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True)
            self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        else:
            link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "linked"):
            c.executable_hashes({"one": str(link / "one.exe")}, root)

    def test_real_mutation_during_hash_rejected(self):
        root = Path("executables").resolve()
        root.mkdir()
        binary = root / "one.exe"
        binary.write_bytes(b"original")
        real_sha256 = c.hashlib.sha256
        class MutatingHash:
            def __init__(self):
                self.inner = real_sha256()
            def update(self, block):
                self.inner.update(block)
                with binary.open("ab") as output:
                    output.write(b"changed")
            def hexdigest(self):
                return self.inner.hexdigest()
        with patch.object(c.hashlib, "sha256", MutatingHash):
            with self.assertRaisesRegex(ValueError, "changed while hashing"):
                c.executable_hashes({"one": str(binary)}, root)

    def test_executable_identity_rejects_mismatched_and_unlisted_entries(self):
        data = {"rust-suites": {"a": {"binary-id": "a", "status": "listed", "binary-path": "a.exe"}}}
        self.assertEqual(c.inventory_executable_paths(data), {"a": "a.exe"})
        data["rust-suites"]["a"]["binary-id"] = "b"
        with self.assertRaisesRegex(ValueError, "identity"):
            c.inventory_executable_paths(data)
        data["rust-suites"]["a"]["binary-id"] = "a"
        data["rust-suites"]["a"]["status"] = "skipped"
        with self.assertRaisesRegex(ValueError, "identity"):
            c.inventory_executable_paths(data)

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

    def test_nested_manifest_is_hashed(self):
        root = Path("bundle")
        (root / "nested").mkdir(parents=True)
        (root / "manifest.json").write_text("self")
        (root / "nested/manifest.json").write_text("fixture")
        self.assertEqual(set(c.files(root)), {"nested/manifest.json"})

    def test_zip_hidden_file_and_unsafe_members(self):
        Path("pilot-bundle").mkdir()
        with patch.object(c, "identity", return_value={"sha": "a"}):
            for member in ("../escape", "corpus/../escape", "C:/escape", "corpus/a:b", "corpus/./bad"):
                with c.zipfile.ZipFile("pilot-bundle/runtime.zip", "w") as archive:
                    archive.writestr(member, b"bad")
                c.write("pilot-bundle/manifest.json", {"identity": {"sha": "a"}, "files": c.files(Path("pilot-bundle"))})
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    c.verify("default")
            with c.zipfile.ZipFile("pilot-bundle/runtime.zip", "w") as archive:
                archive.writestr("corpus/.hidden-fixture", b"preserved")
            c.write("pilot-bundle/manifest.json", {"identity": {"sha": "a"}, "files": c.files(Path("pilot-bundle"))})
            c.verify("default")
            self.assertEqual(Path("corpus/.hidden-fixture").read_bytes(), b"preserved")

    def test_linked_source_rejected_before_packing(self):
        Path("corpus").mkdir()
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ValueError, "linked runtime root"):
                c.prepare("default")
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
