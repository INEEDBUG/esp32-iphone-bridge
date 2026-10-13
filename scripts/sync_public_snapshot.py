"""Preview or explicitly synchronize an approved local public snapshot.

This tool does not review privacy/licensing, access credentials, create remotes, or
import source history. Preview is read-only. ``--apply`` updates only manifest paths;
``--push`` is a separate explicit non-force push to the verified existing origin.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
from typing import Iterable


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
ROOT_SOURCE_FILES = {
    ".gitignore", "README.zh-CN.md", "README.md", "Start-PhoneLab.ps1", "LICENSE",
    "THIRD_PARTY_NOTICES.md",
}
PUBLIC_DIRS = {"phonelab", "firmware", "upstream", "scripts", "tests"}
PRIVATE_OR_GENERATED_PARTS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", "build", "dist",
    "cache", "caches", "private", "secrets", "credentials", "pairing-cache", "evidence", "logs",
    "screenshots", "frames", "media", "artifacts",
}
PRIVATE_OR_BINARY_SUFFIXES = {
    ".bin", ".elf", ".map", ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".obj", ".png",
    ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".7z", ".pdf", ".plist", ".pem", ".key",
    ".ppk", ".p12", ".pfx", ".cer", ".crt", ".der", ".mobileprovision", ".db", ".sqlite",
    ".sqlite3", ".log", ".pyc", ".class", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov",
    ".wav", ".mp3", ".pyd", ".whl",
}


class PublicSyncError(RuntimeError):
    """The reviewed staging tree or existing public mirror failed safety checks."""


def _git_env() -> dict[str, str]:
    # Ambient Git variables can redirect the repository, index, config, or hooks.
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")}


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PublicSyncError("manifest contains an invalid relative path")
    posix = PurePosixPath(value)
    if (posix.is_absolute() or PureWindowsPath(value).drive or
            any(part in ("", ".", "..") for part in posix.parts)):
        raise PublicSyncError("manifest path escapes the staging or mirror root")
    return posix


def _git(root: Path, *args: str, binary: bool = False) -> bytes | str:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], check=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=45, env=_git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicSyncError("Git command failed safely") from exc
    if result.returncode != 0:
        raise PublicSyncError("Git validation or operation failed")
    return result.stdout if binary else result.stdout.decode("utf-8", errors="strict")


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicSyncError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise PublicSyncError(f"{label} has an invalid structure")
    return value


def _manifest_files(manifest: dict) -> dict[str, str]:
    if manifest.get("schema") != 1 or not isinstance(manifest.get("source_commit"), str):
        raise PublicSyncError("snapshot manifest schema or source commit is invalid")
    if not COMMIT_RE.fullmatch(manifest["source_commit"]):
        raise PublicSyncError("snapshot manifest source commit is invalid")
    rows = manifest.get("files")
    if not isinstance(rows, dict) or not rows:
        raise PublicSyncError("snapshot manifest has no exported files")
    expected: dict[str, str] = {}
    normalized: set[str] = set()
    for source_path, record in rows.items():
        source_rel = _safe_relative(source_path)
        source_parts = [part.casefold() for part in source_rel.parts]
        source_name = source_rel.name.casefold()
        source_top = source_rel.parts[0]
        source_allowed = (
            len(source_rel.parts) == 1 and
            (source_path in ROOT_SOURCE_FILES or
             (source_name.startswith("requirements") and source_name.endswith(".txt")))
        ) or source_top in PUBLIC_DIRS or source_path in {
            "docs/PUBLIC_README.zh-CN.md", "docs/PUBLIC_PROGRESS.md"}
        if (not source_allowed or any(part in PRIVATE_OR_GENERATED_PARTS for part in source_parts) or
                source_name == ".env" or source_name.startswith(".env.") or
                source_name.endswith(".local.json") or source_rel.suffix.casefold() in PRIVATE_OR_BINARY_SUFFIXES):
            raise PublicSyncError("manifest contains a path outside the public snapshot policy")
        if source_path == "docs/PUBLIC_README.zh-CN.md":
            required_outputs = {"README.zh-CN.md", "README.md"}
        elif source_path == "docs/PUBLIC_PROGRESS.md":
            required_outputs = {"docs/PROGRESS.md"}
        else:
            required_outputs = {source_path}
        if not isinstance(record, dict) or not HASH_RE.fullmatch(str(record.get("source_sha256", ""))):
            raise PublicSyncError("snapshot manifest has an invalid source digest")
        outputs = record.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            raise PublicSyncError("snapshot manifest has an invalid output list")
        for output in outputs:
            if not isinstance(output, dict):
                raise PublicSyncError("snapshot manifest has an invalid output row")
            rel = _safe_relative(output.get("path"))
            name = "/".join(rel.parts)
            if name not in required_outputs:
                raise PublicSyncError("manifest output mapping does not match the public snapshot policy")
            key = name.casefold()
            if key in normalized:
                raise PublicSyncError("snapshot manifest contains duplicate output paths")
            normalized.add(key)
            digest = output.get("sha256")
            if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
                raise PublicSyncError("snapshot manifest has an invalid output digest")
            expected[name] = digest
        if {row.get("path") for row in outputs if isinstance(row, dict)} != required_outputs:
            raise PublicSyncError("manifest output mapping is incomplete")
    # File/parent collisions are not portable and can overwrite a different manifest path.
    lowered = {path.casefold() for path in expected}
    for path in lowered:
        parts = path.split("/")
        if any("/".join(parts[:index]) in lowered for index in range(1, len(parts))):
            raise PublicSyncError("snapshot manifest contains file and directory path collisions")
    required_public = {"LICENSE", "THIRD_PARTY_NOTICES.md", "README.zh-CN.md", "README.md", "docs/PROGRESS.md"}
    if not required_public.issubset(expected):
        raise PublicSyncError("manifest is missing required public documentation or license files")
    return expected


def _staging_files(staging: Path, expected: dict[str, str]) -> None:
    if not staging.is_dir() or staging.is_symlink():
        raise PublicSyncError("staging snapshot must be a regular directory")
    actual: set[str] = set()
    for item in staging.rglob("*"):
        if item.is_symlink():
            raise PublicSyncError("staging snapshot must not contain symlinks")
        if item.is_file():
            rel = item.relative_to(staging).as_posix()
            _safe_relative(rel)
            actual.add(rel)
        elif not item.is_dir():
            raise PublicSyncError("staging snapshot contains an unsupported filesystem entry")
    if actual != set(expected):
        raise PublicSyncError("staging files do not exactly match the approved manifest")
    for rel, expected_hash in expected.items():
        path = staging.joinpath(*_safe_relative(rel).parts)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise PublicSyncError("staging file digest does not match the manifest")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _worktree_path(root: Path, rel: str) -> Path:
    current = root
    for part in _safe_relative(rel).parts:
        current = current / part
        if current.is_symlink():
            raise PublicSyncError("a touched mirror path is a symlink")
        if not _is_within(current.resolve(strict=False), root):
            raise PublicSyncError("a touched mirror path escapes the public mirror")
    return current


def _mirror_state(mirror: Path, expected_remote: str) -> tuple[str, set[str]]:
    if mirror.is_symlink() or not mirror.is_dir():
        raise PublicSyncError("mirror must be an existing regular directory")
    root_value = str(_git(mirror, "rev-parse", "--show-toplevel")).strip()
    root = Path(root_value).resolve(strict=True)
    if root != mirror.resolve(strict=True):
        raise PublicSyncError("mirror path is not the exact Git worktree root")
    branch = str(_git(mirror, "symbolic-ref", "--quiet", "--short", "HEAD")).strip()
    if branch != "main":
        raise PublicSyncError("public mirror must be on main")
    remotes = set(str(_git(mirror, "remote")).split())
    if remotes != {"origin"}:
        raise PublicSyncError("public mirror must have only its reviewed origin remote")
    fetch_url = str(_git(mirror, "remote", "get-url", "origin")).strip()
    push_url = str(_git(mirror, "remote", "get-url", "--push", "origin")).strip()
    if not expected_remote or fetch_url != expected_remote or push_url != expected_remote:
        raise PublicSyncError("origin does not match the expected public remote")
    status = _git(mirror, "status", "--porcelain=v1", "--untracked-files=all", binary=True)
    if status:
        raise PublicSyncError("public mirror must have a clean worktree and index")
    tracked_raw = _git(mirror, "ls-files", "-z", binary=True)
    tracked: set[str] = set()
    for raw in tracked_raw.split(b"\0"):
        if raw:
            try:
                path = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise PublicSyncError("mirror has an unsupported tracked path") from exc
            _safe_relative(path)
            tracked.add(path)
    return str(_git(mirror, "rev-parse", "HEAD")).strip(), tracked


def _verify_remote_at_base(mirror: Path, base_head: str) -> None:
    lines = str(_git(mirror, "ls-remote", "--heads", "origin", "refs/heads/main")).splitlines()
    if len(lines) != 1:
        raise PublicSyncError("existing remote main branch is missing or ambiguous")
    parts = lines[0].split("\t")
    if len(parts) != 2 or parts[0] != base_head or parts[1] != "refs/heads/main":
        raise PublicSyncError("mirror HEAD does not match the existing remote main branch")


def _assert_hooks_inactive(mirror: Path) -> None:
    configured = subprocess.run(["git", "-C", str(mirror), "config", "--get", "core.hooksPath"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                                 timeout=20, env=_git_env())
    if configured.returncode == 0 and configured.stdout.strip():
        raise PublicSyncError("custom Git hooksPath is configured")
    if configured.returncode not in (0, 1):
        raise PublicSyncError("Git hook configuration could not be checked")
    hook_dir_text = str(_git(mirror, "rev-parse", "--git-path", "hooks")).strip()
    hook_dir = Path(hook_dir_text)
    if not hook_dir.is_absolute():
        hook_dir = mirror / hook_dir
    if hook_dir.is_symlink():
        raise PublicSyncError("Git hooks directory is a symlink")
    if hook_dir.exists():
        for hook in hook_dir.iterdir():
            if hook.name.endswith(".sample"):
                continue
            if hook.exists() or hook.is_symlink():
                raise PublicSyncError("active custom Git hook blocks sync")


def _load_approval(manifest_path: Path, review_path: Path) -> tuple[dict, dict, dict[str, str]]:
    manifest = _read_json(manifest_path, "snapshot manifest")
    review = _read_json(review_path, "publication review")
    expected = _manifest_files(manifest)
    if review.get("approved_for_publication") is not True:
        raise PublicSyncError("LEAD publication review is not approved")
    if review.get("source_commit") != manifest.get("source_commit"):
        raise PublicSyncError("reviewed source commit does not match the snapshot manifest")
    return manifest, review, expected


def sync_public_snapshot(staging: Path | str, manifest_path: Path | str,
                         mirror: Path | str, review_path: Path | str,
                         expected_remote: str, *, apply: bool = False,
                         push: bool = False, message: str = "") -> dict:
    raw_inputs = [Path(value).expanduser().absolute()
                  for value in (staging, manifest_path, mirror, review_path)]
    if any(path.is_symlink() for path in raw_inputs):
        raise PublicSyncError("sync inputs must not be symbolic links")
    staging, manifest_path, mirror, review_path = (
        path.resolve(strict=True) for path in raw_inputs)
    if _is_within(staging, mirror) or _is_within(mirror, staging):
        raise PublicSyncError("staging and public mirror must be separate trees")
    if _is_within(manifest_path, mirror) or _is_within(review_path, mirror):
        raise PublicSyncError("review inputs must remain outside the public mirror")
    if push and not apply:
        raise PublicSyncError("--push requires an explicit --apply")
    if apply and (not message.strip() or any(ord(ch) < 32 for ch in message)):
        raise PublicSyncError("a single-line commit message is required for apply")
    manifest, _review, expected = _load_approval(manifest_path, review_path)
    _staging_files(staging, expected)
    _assert_hooks_inactive(mirror)
    base_head, tracked = _mirror_state(mirror, expected_remote)
    copy_paths: list[str] = []
    delete_paths: list[str] = []
    for rel, digest in expected.items():
        target = _worktree_path(mirror, rel)
        if target.exists():
            if not target.is_file():
                raise PublicSyncError("a manifest destination is not a regular file")
            if rel not in tracked:
                raise PublicSyncError("an ignored or untracked mirror asset occupies a manifest path")
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                copy_paths.append(rel)
        else:
            copy_paths.append(rel)
    for rel in tracked - set(expected):
        if rel.casefold().startswith(".github/workflows/"):
            raise PublicSyncError("existing workflow changes require separate review")
        target = _worktree_path(mirror, rel)
        if target.exists():
            if not target.is_file():
                raise PublicSyncError("tracked deletion target is not a regular file")
            delete_paths.append(rel)
    changed = sorted(set(copy_paths) | set(delete_paths))
    if not changed:
        if push:
            raise PublicSyncError("no snapshot change exists to push")
        return {"state": "NO_CHANGES", "changed_files": 0, "deleted_files": 0}
    if not apply:
        return {"state": "PREVIEW_ONLY", "changed_files": len(copy_paths),
                "deleted_files": len(delete_paths)}

    if push:
        _verify_remote_at_base(mirror, base_head)
    for rel in copy_paths:
        if rel.casefold().startswith(".github/workflows/"):
            raise PublicSyncError("new or changed workflow files require separate review")

    for rel in copy_paths:
        source = staging.joinpath(*_safe_relative(rel).parts)
        target = _worktree_path(mirror, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() and rel in tracked:
            # A tracked-but-missing file will be recreated from the verified snapshot.
            pass
        elif target.exists() and rel not in tracked:
            raise PublicSyncError("refusing to overwrite an untracked path")
        target.write_bytes(source.read_bytes())
    for rel in delete_paths:
        target = _worktree_path(mirror, rel)
        if target.is_symlink() or not _is_within(target.resolve(strict=True), mirror):
            raise PublicSyncError("tracked deletion target escaped the mirror")
        target.unlink()

    path_args = [*changed]
    _git(mirror, "--literal-pathspecs", "add", "--force", "--all", "--", *path_args)
    staged_raw = _git(mirror, "diff", "--cached", "--name-only", "-z", binary=True)
    try:
        staged = {part.decode("utf-8", errors="strict") for part in staged_raw.split(b"\0") if part}
    except UnicodeDecodeError as exc:
        raise PublicSyncError("staged path list is not supported") from exc
    if staged != set(changed):
        raise PublicSyncError("staged paths differ from the approved snapshot plan")
    _git(mirror, "-c", "user.name=CodexAgent", "-c", "user.email=codex@localhost",
         "commit", "-m", message)
    head = str(_git(mirror, "rev-parse", "HEAD")).strip()
    parent = str(_git(mirror, "rev-parse", "HEAD^" )).strip()
    if parent != base_head:
        raise PublicSyncError("sync commit parent differs from the existing public mirror HEAD")
    if _git(mirror, "status", "--porcelain=v1", "--untracked-files=all", binary=True):
        raise PublicSyncError("mirror is not clean after local sync commit")
    if not push:
        return {"state": "COMMITTED_LOCAL", "commit": head,
                "changed_files": len(copy_paths), "deleted_files": len(delete_paths)}
    _git(mirror, "push", "--no-follow-tags", "origin", "main")
    remote_lines = str(_git(mirror, "ls-remote", "--heads", "origin", "refs/heads/main")).splitlines()
    if len(remote_lines) != 1:
        raise PublicSyncError("remote branch readback was missing or ambiguous")
    parts = remote_lines[0].split("\t")
    if len(parts) != 2 or parts[0] != head or parts[1] != "refs/heads/main":
        raise PublicSyncError("remote branch readback did not match the local commit")
    return {"state": "PUSHED_AND_VERIFIED", "commit": head,
            "changed_files": len(copy_paths), "deleted_files": len(delete_paths)}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--mirror", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--expected-remote", required=True)
    parser.add_argument("--message", default="")
    parser.add_argument("--apply", action="store_true", help="copy, stage and commit the approved snapshot")
    parser.add_argument("--push", action="store_true", help="also perform a non-force push and branch readback")
    args = parser.parse_args(argv)
    try:
        result = sync_public_snapshot(args.staging, args.manifest, args.mirror,
                                      args.review, args.expected_remote,
                                      apply=args.apply, push=args.push, message=args.message)
    except PublicSyncError as exc:
        print(f"PUBLIC_SYNC_REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
