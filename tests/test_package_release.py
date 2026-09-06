"""Distribution integrity: private files stay out and archive bytes are reproducible."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import package_release


class PackageReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        for name in package_release.REQUIRED:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture\n")
        (self.root / "SKILL.md").write_text('---\nname: game-sensitivity-coach\ndescription: Fixture\nmetadata:\n  version: "1.0.0"\n---\n', encoding="utf-8")

    def test_manifest_hashes_and_private_exclusion_and_reproducible_archive(self):
        for name in ("sessions/private.mp4", "profiles/person.json", "scripts/token.txt", "scripts/__pycache__/private.pyc", "assets/private.png", "examples/account.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"DO NOT DISTRIBUTE")
        out = self.root.parent / "release.zip"
        first = package_release.build(self.root, out)
        with zipfile.ZipFile(str(out)) as archive:
            self.assertIsNone(archive.testzip())
            prefix = package_release.NAME + "/"
            manifest = json.loads(archive.read(prefix + "RELEASE-MANIFEST.json"))
            self.assertEqual(set(archive.namelist()), {prefix + item["path"] for item in manifest["files"]} | {prefix + "RELEASE-MANIFEST.json"})
            for item in manifest["files"]:
                data = archive.read(prefix + item["path"])
                self.assertNotIn(b"DO NOT DISTRIBUTE", data)
                self.assertEqual(item["bytes"], len(data))
                self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
        second = package_release.build(self.root, self.root.parent / "another.zip")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertIn(first["sha256"], Path(first["sha256_file"]).read_text())
        with self.assertRaisesRegex(ValueError, "already exists"):
            package_release.build(self.root, out)

    def test_incomplete_source_or_output_in_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            package_release.build(self.root, self.root / "release.zip")
        (self.root / "README.md").unlink()
        with self.assertRaisesRegex(ValueError, "Missing"):
            package_release.build(self.root, self.root.parent / "release.zip")


if __name__ == "__main__":
    unittest.main()
