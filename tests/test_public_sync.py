import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.sync_public_snapshot import PublicSyncError, sync_public_snapshot


class PublicSnapshotSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.mirror = self.root / "public-mirror"
        self.remote = self.root / "public-remote.git"
        self.manifest = self.root / "private" / "manifest.json"
        self.review = self.root / "private" / "review.json"
        self._git("init", "--bare", str(self.remote), cwd=self.root)
        self._git("init", "-b", "main", str(self.mirror), cwd=self.root)
        self._git("config", "user.name", "Fixture Owner", cwd=self.mirror)
        self._git("config", "user.email", "fixture@example.invalid", cwd=self.mirror)
        (self.mirror / "phonelab").mkdir()
        (self.mirror / "phonelab" / "app.py").write_text("old\n", encoding="utf-8")
        (self.mirror / "remove-me.txt").write_text("tracked old file\n", encoding="utf-8")
        self._git("add", "--all", cwd=self.mirror)
        self._git("commit", "-qm", "public initial snapshot", cwd=self.mirror)
        self.initial_head = self._git("rev-parse", "HEAD", cwd=self.mirror).strip()
        self._git("remote", "add", "origin", str(self.remote), cwd=self.mirror)
        self._git("push", "-u", "origin", "main", cwd=self.mirror)
        self.public_files = {
            "phonelab/app.py": b"new public code\n",
            "LICENSE": b"public license\n",
            "THIRD_PARTY_NOTICES.md": b"third-party notices\n",
            "README.zh-CN.md": b"# public readme\n",
            "README.md": b"# public readme\n",
            "docs/PROGRESS.md": b"public progress\n",
        }
        self._write_stage(self.public_files)

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args, cwd):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                       stderr=subprocess.PIPE)

    def _write_stage(self, files):
        if self.staging.exists():
            shutil.rmtree(self.staging)
        self.staging.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            path = self.staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        records = {}
        mapped = set()
        for name, content in files.items():
            if name in {"README.zh-CN.md", "README.md"}:
                source = "docs/PUBLIC_README.zh-CN.md"
                targets = ["README.zh-CN.md", "README.md"]
            elif name == "docs/PROGRESS.md":
                source = "docs/PUBLIC_PROGRESS.md"
                targets = ["docs/PROGRESS.md"]
            else:
                source = name
                targets = [name]
            if source in mapped:
                continue
            mapped.add(source)
            outputs = [{"path": target, "sha256": hashlib.sha256(files[target]).hexdigest()}
                       for target in targets]
            records[source] = {"source_sha256": hashlib.sha256(content).hexdigest(),
                               "outputs": outputs}
        manifest = {"schema": 1, "source_commit": "a" * 40, "files": records, "exclusions": []}
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.review.write_text(json.dumps({"approved_for_publication": True,
                                           "source_commit": manifest["source_commit"]}), encoding="utf-8")

    def _run(self, *, apply=False, push=False, remote=None, manifest=None, review=None, staging=None):
        return sync_public_snapshot(
            staging or self.staging, manifest or self.manifest, self.mirror,
            review or self.review, remote or str(self.remote), apply=apply, push=push,
            message="sync reviewed public snapshot",
        )

    def test_preview_is_read_only_and_reports_changes(self):
        before = self._git("rev-parse", "HEAD", cwd=self.mirror).strip()
        result = self._run()
        self.assertEqual(result["state"], "PREVIEW_ONLY")
        self.assertEqual(self._git("rev-parse", "HEAD", cwd=self.mirror).strip(), before)
        self.assertEqual((self.mirror / "phonelab" / "app.py").read_text(encoding="utf-8"), "old\n")
        self.assertTrue((self.mirror / "remove-me.txt").exists())
        self.assertEqual(self._git("status", "--porcelain", "--untracked-files=all", cwd=self.mirror), "")

    def test_apply_updates_snapshot_removes_only_tracked_file_and_uses_public_head_parent(self):
        result = self._run(apply=True)
        self.assertEqual(result["state"], "COMMITTED_LOCAL")
        self.assertEqual((self.mirror / "phonelab" / "app.py").read_bytes(), b"new public code\n")
        self.assertEqual((self.mirror / "LICENSE").read_bytes(), b"public license\n")
        self.assertEqual((self.mirror / "docs/PROGRESS.md").read_bytes(), b"public progress\n")
        self.assertFalse((self.mirror / "remove-me.txt").exists())
        head = self._git("rev-parse", "HEAD", cwd=self.mirror).strip()
        parent = self._git("rev-parse", "HEAD^", cwd=self.mirror).strip()
        self.assertEqual(parent, self.initial_head)
        self.assertEqual(self._git("status", "--porcelain", "--untracked-files=all", cwd=self.mirror), "")
        before = self._git("rev-parse", "HEAD", cwd=self.mirror).strip()
        self.assertEqual(self._run(apply=True)["state"], "NO_CHANGES")
        self.assertEqual(self._git("rev-parse", "HEAD", cwd=self.mirror).strip(), before)

    def test_explicit_push_to_local_fixture_remote_reads_back_exact_head(self):
        result = self._run(apply=True, push=True)
        self.assertEqual(result["state"], "PUSHED_AND_VERIFIED")
        remote_head = self._git("ls-remote", str(self.remote), "refs/heads/main", cwd=self.root).split()[0]
        self.assertEqual(remote_head, result["commit"])
        self.assertEqual(self._git("rev-parse", "HEAD^", cwd=self.mirror).strip(), self.initial_head)

    def _bilingual_manifest(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        zh = manifest["files"]["docs/PUBLIC_README.zh-CN.md"]
        english = b"# English overview\n"
        (self.staging / "README.md").write_bytes(english)
        zh["outputs"] = [row for row in zh["outputs"] if row["path"] == "README.zh-CN.md"]
        digest = hashlib.sha256(english).hexdigest()
        manifest["files"]["docs/PUBLIC_README.md"] = {
            "source_sha256": digest, "outputs": [{"path": "README.md", "sha256": digest}]}
        return manifest

    def test_distinct_language_mapping_and_named_banner_are_accepted(self):
        manifest = self._bilingual_manifest()
        name = "docs/assets/phonelab-banner.svg"
        banner = b'<svg xmlns="http://www.w3.org/2000/svg"/>\n'
        path = self.staging / name
        path.parent.mkdir(parents=True)
        path.write_bytes(banner)
        digest = hashlib.sha256(banner).hexdigest()
        manifest["files"][name] = {"source_sha256": digest, "outputs": [{"path": name, "sha256": digest}]}
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self._run(apply=True)["state"], "COMMITTED_LOCAL")
        self.assertNotEqual((self.mirror / "README.md").read_bytes(), (self.mirror / "README.zh-CN.md").read_bytes())

    def test_bilingual_mapping_cannot_alias_or_omit_language_output(self):
        manifest = self._bilingual_manifest()
        manifest["files"]["docs/PUBLIC_README.md"]["outputs"][0]["path"] = "README.zh-CN.md"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(PublicSyncError):
            self._run()
        manifest = self._bilingual_manifest()
        manifest["files"]["docs/PUBLIC_README.zh-CN.md"]["outputs"] = []
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(PublicSyncError):
            self._run()

    def test_hash_mismatch_unapproved_review_wrong_remote_and_dirty_tree_block(self):
        path = self.staging / "phonelab" / "app.py"
        path.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(PublicSyncError):
            self._run()
        path.write_bytes(b"new public code\n")
        self.review.write_text(json.dumps({"approved_for_publication": False,
                                           "source_commit": "a" * 40}), encoding="utf-8")
        with self.assertRaises(PublicSyncError):
            self._run()
        self.review.write_text(json.dumps({"approved_for_publication": True,
                                           "source_commit": "b" * 40}), encoding="utf-8")
        with self.assertRaises(PublicSyncError):
            self._run()
        self.review.write_text(json.dumps({"approved_for_publication": True,
                                           "source_commit": "a" * 40}), encoding="utf-8")
        with self.assertRaises(PublicSyncError):
            self._run(remote=str(self.root / "wrong.git"))
        (self.mirror / "untracked.txt").write_text("preserve", encoding="utf-8")
        with self.assertRaises(PublicSyncError):
            self._run()
        self.assertTrue((self.mirror / "untracked.txt").exists())

    def test_workflow_addition_and_unmanifested_staging_file_are_rejected(self):
        workflow = dict(self.public_files)
        workflow[".github/workflows/publish.yml"] = b"name: publish\n"
        self._write_stage(workflow)
        with self.assertRaises(PublicSyncError):
            self._run(apply=True)
        self._write_stage(self.public_files)
        (self.staging / "extra.txt").write_text("extra", encoding="utf-8")
        with self.assertRaises(PublicSyncError):
            self._run()

    def test_non_main_branch_and_custom_hooks_block_apply(self):
        self._git("checkout", "-qb", "other", cwd=self.mirror)
        with self.assertRaises(PublicSyncError):
            self._run(apply=True)
        self._git("checkout", "-q", "main", cwd=self.mirror)
        hook = self.mirror / ".git" / "hooks" / "pre-commit"
        hook.write_text("echo custom\n", encoding="utf-8")
        with self.assertRaises(PublicSyncError):
            self._run(apply=True)
        self.assertEqual(self._git("rev-parse", "HEAD", cwd=self.mirror).strip(), self.initial_head)

    def test_ambient_git_redirection_variables_are_ignored(self):
        previous = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = str(self.remote)
        try:
            self.assertEqual(self._run()["state"], "PREVIEW_ONLY")
        finally:
            if previous is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = previous

    def test_symlink_input_root_is_rejected_when_supported(self):
        alias = self.root / "staging-link"
        try:
            alias.symlink_to(self.staging, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation is unavailable")
        with self.assertRaises(PublicSyncError):
            self._run(staging=alias)


if __name__ == "__main__":
    unittest.main()
