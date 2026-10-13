import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.prepare_public_snapshot import (
    SnapshotError,
    prepare_public_snapshot,
    validate_repo_path,
)


class PublicSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Snapshot Test")
        self._git("config", "user.email", "snapshot@example.invalid")
        self._write(".gitignore", b"private/\n")
        self._write("README.md", b"local current worktree only\n")
        self._write("LICENSE", b"project license bytes\n")
        self._write("THIRD_PARTY_NOTICES.md", b"upstream notice bytes\n")
        self._write("requirements.lock.txt", b"example-package==1.2.3\n")
        self._write("Start-PhoneLab.ps1", b"Write-Output 'portable'\n")
        self._write("phonelab/app.py", b"SOURCE = b'unchanged'\n")
        self._write("firmware/main.ino", b"void setup() {}\n")
        self._write("firmware/build/firmware.bin", b"\x00binary excluded by path\n")
        self._write("upstream/module.py", b"# retained upstream file\n")
        self._write("scripts/build.ps1", b"Write-Output 'build'\n")
        self._write("tests/test_app.py", b"def test_ok(): pass\n")
        self._write("docs/PUBLIC_README.zh-CN.md",
                    b"# Public\n[License](../LICENSE)\n[Notices](../THIRD_PARTY_NOTICES.md)\n")
        self._write("docs/PUBLIC_PROGRESS.md", b"# Progress\nPublic-only milestone.\n")
        self._write("docs/HANDOFF.md", b"Do not export this operational file.\n")
        self._write("AGENTS.md", b"Do not export project instructions.\n")
        self._write("private/target.json", b"not read by the exporter\n")
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture source")
        self.ref = self._git("rev-parse", "HEAD").strip()
        self.destination = self.root / "public-snapshot"
        self.manifest = self.root / "private" / "snapshot-manifest.json"

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.source, text=True,
                                       stderr=subprocess.PIPE)

    def _write(self, relative, data):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_exports_allowlist_commit_bytes_and_sanitized_public_docs(self):
        self._write("phonelab/app.py", b"uncommitted secret must not export\n")
        record = prepare_public_snapshot(self.source, self.destination, self.ref, self.manifest)
        self.assertEqual((self.destination / "phonelab/app.py").read_bytes(), b"SOURCE = b'unchanged'\n")
        self.assertEqual((self.destination / "LICENSE").read_bytes(), b"project license bytes\n")
        readme = (self.destination / "README.zh-CN.md").read_bytes()
        self.assertIn(b"](LICENSE)", readme)
        self.assertIn(b"](THIRD_PARTY_NOTICES.md)", readme)
        self.assertEqual((self.destination / "README.md").read_bytes(), readme)
        self.assertEqual((self.destination / "docs/PROGRESS.md").read_bytes(),
                         b"# Progress\nPublic-only milestone.\n")
        self.assertFalse((self.destination / "docs/HANDOFF.md").exists())
        self.assertFalse((self.destination / "AGENTS.md").exists())
        self.assertFalse((self.destination / "private/target.json").exists())
        self.assertFalse((self.destination / "firmware/build/firmware.bin").exists())
        self.assertEqual(record["source_commit"], self.ref)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertIn("docs/HANDOFF.md", [x["path"] for x in manifest["exclusions"]])
        self.assertNotIn(str(self.source), self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"]["phonelab/app.py"]["source_sha256"],
                         __import__("hashlib").sha256(b"SOURCE = b'unchanged'\n").hexdigest())

    def test_nonempty_destination_is_refused_without_modification(self):
        self.destination.mkdir()
        sentinel = self.destination / "preserve.txt"
        sentinel.write_text("leave intact", encoding="utf-8")
        with self.assertRaises(SnapshotError):
            prepare_public_snapshot(self.source, self.destination, self.ref, self.manifest)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "leave intact")

    def test_invalid_ref_and_manifest_inside_destination_are_refused(self):
        with self.assertRaises(SnapshotError):
            prepare_public_snapshot(self.source, self.destination, "missing-ref", self.manifest)
        with self.assertRaises(SnapshotError):
            prepare_public_snapshot(self.source, self.destination, self.ref,
                                    self.destination / "manifest.json")
        self.assertFalse(self.destination.exists())

    def test_unexpected_binary_blob_fails_closed(self):
        self._write("scripts/unexpected.dat", b"\xffunclassified binary")
        self._git("add", "--all")
        self._git("commit", "-qm", "unexpected fixture blob")
        ref = self._git("rev-parse", "HEAD").strip()
        with self.assertRaises(SnapshotError):
            prepare_public_snapshot(self.source, self.destination, ref, self.manifest)
        self.assertFalse(self.destination.exists())

    def test_repo_path_validation_rejects_traversal_and_absolute_names(self):
        for value in ("../outside", "/absolute", "C:/outside", "phonelab/../../outside", "phonelab\\..\\outside"):
            with self.subTest(value=value), self.assertRaises(SnapshotError):
                validate_repo_path(value)


if __name__ == "__main__":
    unittest.main()
