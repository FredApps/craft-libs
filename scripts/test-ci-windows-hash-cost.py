"""Pure extractor/path regressions; no application binaries or tests executed."""
import importlib.util
import io
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("hash_cost", Path(__file__).with_name("ci-windows-hash-cost.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class Tests(unittest.TestCase):
    def test_windows_ambiguous_components_rejected(self):
        for name in ("", "/absolute", "../outside", ".. /outside", "nested/../x", "a//b", "a//", "a/./b",
                     "CON", "con.exe", "LPT1.txt", "COM9.exe", "COM¹.exe", "AUX.foo", "NUL", "CONOUT$",
                     "CON .txt", "folder/file.", "folder/file ", "C:/file", "folder\\file", "a:b", "a?b", "a*b",
                     'a"b', "a<b", "a>b", "a|b", "a\x00b", "a\x1fb"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                c.relative_name(name)
        self.assertEqual(c.relative_name("corpus/test.exe"), PurePosixPath("corpus/test.exe"))
        self.assertEqual(c.relative_name("corpus/"), PurePosixPath("corpus"))

    def test_resolved_containment_and_exclusive_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = c.create_root(Path(tmp) / "extract")
            with self.assertRaises(ValueError):
                c.check_destination(root, Path(tmp) / "outside")
            target = c.extraction_target(root, c.relative_name("nested/one.exe"))
            c.copy_bounded(io.BytesIO(b"abc"), root, target, 3)
            self.assertEqual(target.read_bytes(), b"abc")
            with self.assertRaises(FileExistsError):
                c.copy_bounded(io.BytesIO(b"abc"), root, target, 3)
            with self.assertRaises(ValueError):
                c.nonnegative_size(-1)
            with self.assertRaises(ValueError):
                c.nonnegative_size(True)

    def test_real_reparse_parent_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = c.create_root(Path(tmp) / "extract")
            outside = Path(tmp) / "outside"
            outside.mkdir()
            link = root / "linked"
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True)
            else:
                link.symlink_to(outside, target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError, "linked"):
                    c.extraction_target(root, c.relative_name("linked/one.exe"))
                self.assertFalse((outside / "one.exe").exists())
            finally:
                os.rmdir(link) if os.name == "nt" else link.unlink()

    def test_runtime_zip_alias_and_bad_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, names in enumerate((("corpus/CON.exe",), ("corpus/a", "corpus/A"), ("corpus/.. /escape",))):
                archive_path = root / f"{index}.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for name in names:
                        archive.writestr(name, b"fixture")
                with self.assertRaises(ValueError):
                    c.extract_runtime(archive_path, root / f"extract-{index}")


if __name__ == "__main__":
    unittest.main()
